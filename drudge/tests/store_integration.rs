//! Rust integration tests for the Storage Layer contract.
//!
//! These tests exercise the live PostgreSQL backend, NOT the HTTP/MCP surface
//! (that belongs in `scripts/e2e.sh` and `data/eval/run_eval.py`). They need a
//! Postgres instance reachable via `BORING_TEST_DATABASE_URL`. If the variable is
//! unset, the tests are skipped with a clear message.
//!
//! Run via (serially — they share one DB and `compact()` does a global `REINDEX CONCURRENTLY`,
//! which conflicts with other tests' open connections under the default parallel runner):
//!   `BORING_TEST_DATABASE_URL=postgresql://boring:boring@localhost:5432/boring_test \`
//!   `  cargo test -p drudge --test store_integration -- --test-threads=1`
#![allow(clippy::expect_used, clippy::unwrap_used)] // tests may fail fast on setup errors

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::frontmatter::{Author, Claim, FrontMatter};
use drudge::store::{DistKind, Doc, EventLogFilter, LoggedHit, Store};
use serde_json::json;
use tokio_postgres::{Client, NoTls};

fn test_dsn() -> Option<String> {
    std::env::var("BORING_TEST_DATABASE_URL").ok()
}

async fn connect(dsn: &str) -> Client {
    let (client, conn) = tokio_postgres::connect(dsn, NoTls)
        .await
        .expect("connect to Postgres");
    tokio::spawn(conn);
    client
}

fn unique_path(prefix: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("/vault/wiki/{prefix}-{ts}.md")
}

/// `current_claims` reads top-k through HNSW (ef_search 40): a fixture far from the query is
/// found or missed depending on what earlier suites left in the index. Query along its own direction.
fn unique_direction() -> [f32; 1024] {
    static SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let nanos = u64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
    )
    .unwrap();
    let seq = SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let mut state = (nanos ^ (seq << 48)) | 1;
    std::array::from_fn(|_| {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        if state & 1 == 0 { 1.0 } else { -1.0 }
    })
}

fn dummy_frontmatter(path: &str) -> FrontMatter {
    FrontMatter {
        origin: "personal".to_string(),
        project: "test".to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("test note".to_string()),
        tags: vec!["test".to_string()],
        ..Default::default()
    }
}

async fn count_claims(db: &Client, path: &str) -> i64 {
    db.query_one(
        "SELECT count(*) FROM claim WHERE source_path = $1;",
        &[&path],
    )
    .await
    .expect("count claims")
    .get(0)
}

/// Ensure VACUUM/REINDEX CONCURRENTLY run outside a transaction block.
#[tokio::test]
async fn compact_succeeds_in_autocommit_mode() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let summary = store.compact().await.expect("compact should not fail");
    assert!(summary.total_ms > 0, "compact should report elapsed time");
}

/// Workflow events are stored in Postgres as OpenTelemetry-shaped rows while keeping legacy
/// filter keys (`component`, `event`, `status`, `run_id`) queryable.
#[tokio::test]
async fn event_log_round_trips_otel_projection() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let run_id = format!(
        "event-roundtrip-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let fake_key = ["sk", "-ant", "-abcdefghij1234567890XYZ"].join("");

    store
        .log_event(&json!({
            "ts": "2026-07-01T00:00:00Z",
            "component": "guard",
            "event": "structural_guard",
            "status": "failed",
            "run_id": run_id,
            "credential": fake_key,
            "workflow": "memory_ingest",
            "workflow_node": "remember",
            "workflow_outcome": "failed",
            "otel": {
                "time_unix_nano": 1_782_864_000_000_000_000_i64,
                "severity_text": "ERROR",
                "severity_number": 17,
                "event_name": "structural_guard"
            }
        }))
        .await
        .expect("log event");

    let rows = store
        .recent_events(EventLogFilter {
            limit: 10,
            component: Some("guard"),
            event_name: Some("structural_guard"),
            status: Some("failed"),
            run_id: Some(&run_id),
            workflow: Some("memory_ingest"),
            since_hours: None,
        })
        .await
        .expect("recent events");
    assert_eq!(rows.len(), 1);
    let row = &rows[0];
    assert_eq!(row.severity_text, "ERROR");
    assert_eq!(row.severity_number, 17);
    assert_eq!(row.workflow_node.as_deref(), Some("remember"));
    assert_eq!(row.time_unix_nano, Some(1_782_864_000_000_000_000));
    let attrs = row.attributes.to_string();
    assert!(
        !attrs.contains("sk-ant-"),
        "event attributes must be redacted"
    );
    assert!(
        attrs.contains("REDACTED"),
        "redacted marker should remain visible"
    );

    let db = connect(&dsn).await;
    db.execute("DELETE FROM event_log WHERE run_id = $1;", &[&run_id])
        .await
        .expect("cleanup event");
}

/// Ensure current_claims honors exclude_origins (a claim's origin comes from its parent document via
/// the JOIN), so a claim can't bypass the same origin boundary the recalled chunks in an answer respect.
#[tokio::test]
async fn current_claims_honors_exclude_origins() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    // Two docs with claims, distinct origins. Unique subjects so we can identify them in results.
    let p_path = unique_path("claim-origin-personal");
    let c_path = unique_path("claim-origin-company");
    let p_subj = format!("subj-personal-{}", p_path.len());
    let c_subj = format!("subj-company-{}", c_path.len());

    let mut p_front = dummy_frontmatter(&p_path);
    p_front.origin = "personal".to_string();
    let mut c_front = dummy_frontmatter(&c_path);
    c_front.origin = "company".to_string();

    let emb_p = unique_direction();
    let emb_c = unique_direction();

    for (front, subj, emb) in [(&p_front, &p_subj, &emb_p), (&c_front, &c_subj, &emb_c)] {
        store
            .upsert_document(front, "sha-claim-origin", SystemTime::now())
            .await
            .expect("upsert document");
        store
            .upsert_claim(
                subj,
                "is",
                "x",
                &front.source_path,
                SystemTime::now(),
                emb,
                "fact",
                "certain",
            )
            .await
            .expect("upsert claim");
    }

    let query: [f32; 1024] = std::array::from_fn(|i| emb_p[i] + emb_c[i]);
    let subjects = |rows: Vec<drudge::store::AnchoredClaim>| -> Vec<String> {
        rows.into_iter().map(|c| c.claim.subject).collect()
    };

    // No exclusion → both visible.
    let all = subjects(
        store
            .current_claims(&query, 20, &[], None, None, None, false)
            .await
            .expect("claims all"),
    );
    assert!(
        all.contains(&p_subj) && all.contains(&c_subj),
        "both origins visible with no exclusion"
    );

    // Exclude company → company claim must be filtered out, personal kept.
    let filtered = subjects(
        store
            .current_claims(
                &query,
                20,
                &["company".to_string()],
                None,
                None,
                None,
                false,
            )
            .await
            .expect("claims filtered"),
    );
    assert!(
        filtered.contains(&p_subj),
        "personal claim must survive the company exclusion"
    );
    assert!(
        !filtered.contains(&c_subj),
        "company claim must be excluded"
    );

    store
        .delete_document(&p_path)
        .await
        .expect("cleanup personal");
    store
        .delete_document(&c_path)
        .await
        .expect("cleanup company");
}

/// `about` meant two relations at once, and only one of them is semantic. A claim belonging to a
/// project and a document mentioning a concept were stored under the same kind, so
/// `semantic_stats.about` -- which sits beside `tools`, `concepts` and `uses` -- counted both and
/// read as roughly two and a half times the number of concept mentions the corpus actually has.
/// The relabel has to move exactly the claim rows and leave the concept rows where they are.
#[tokio::test]
async fn the_relabel_moves_claim_edges_and_leaves_concept_edges() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let db = connect(&dsn).await;
    let tag = unique_path("about-split").replace('/', "-");
    let claim_src = format!("claim:{tag}");
    let doc_src = format!("doc:{tag}");
    let project_dst = format!("project:{tag}");
    let concept_dst = format!("concept:{tag}");

    for (src, dst) in [(&claim_src, &project_dst), (&doc_src, &concept_dst)] {
        db.execute(
            "INSERT INTO edge (src, dst, kind) VALUES ($1, $2, 'about')
             ON CONFLICT DO NOTHING;",
            &[src, dst],
        )
        .await
        .expect("seed edge");
    }

    db.execute(
        "UPDATE edge SET kind = 'claim_of_project'
          WHERE kind = 'about' AND src LIKE 'claim:%' AND dst LIKE 'project:%';",
        &[],
    )
    .await
    .expect("relabel");

    let kind_of = |src: String, dst: String| {
        let db = &db;
        async move {
            db.query_one(
                "SELECT kind FROM edge WHERE src = $1 AND dst = $2;",
                &[&src, &dst],
            )
            .await
            .expect("edge still there")
            .get::<_, String>(0)
        }
    };
    assert_eq!(
        kind_of(claim_src.clone(), project_dst.clone()).await,
        "claim_of_project"
    );
    assert_eq!(
        kind_of(doc_src.clone(), concept_dst.clone()).await,
        "about",
        "a document mentioning a concept is the relation `about` is left meaning"
    );

    // Running it again moves nothing: the predicate no longer matches.
    let moved = db
        .execute(
            "UPDATE edge SET kind = 'claim_of_project'
              WHERE kind = 'about' AND src LIKE $1 AND dst LIKE 'project:%';",
            &[&format!("claim:{tag}")],
        )
        .await
        .expect("second run");
    assert_eq!(moved, 0, "the relabel must be idempotent");

    for (src, dst) in [(&claim_src, &project_dst), (&doc_src, &concept_dst)] {
        db.execute("DELETE FROM edge WHERE src = $1 AND dst = $2;", &[src, dst])
            .await
            .expect("cleanup");
    }
}

/// The briefing decides whether a heading names a project by asking this list, so what it leaves
/// out is as load-bearing as what it returns. A name that reaches it blank or NULL is not a
/// project anyone can look up, and a name returned twice would make the caller's membership set
/// no different -- but the duplicate is a signal the DISTINCT was dropped, so pin both.
#[tokio::test]
async fn project_names_are_distinct_and_skip_the_unnamed() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let named = unique_path("project-names-a");
    let twin = unique_path("project-names-b");
    let blank = unique_path("project-names-blank");
    let slug = format!("proj-{}", named.replace('/', "-"));

    for (path, project) in [
        (&named, slug.as_str()),
        (&twin, slug.as_str()),
        (&blank, ""),
    ] {
        let mut front = dummy_frontmatter(path);
        front.project = project.to_owned();
        store
            .upsert_document(&front, "sha-project-names", SystemTime::now())
            .await
            .expect("upsert document");
    }

    let listed = store.project_names().await.expect("project names");
    assert_eq!(
        listed.iter().filter(|n| *n == &slug).count(),
        1,
        "two documents sharing a project must yield the name once, not twice: {listed:?}"
    );
    assert!(
        !listed.iter().any(String::is_empty),
        "a document with no project must not put an empty name in the list: {listed:?}"
    );

    for path in [&named, &twin, &blank] {
        store.delete_document(path).await.expect("cleanup");
    }
}

/// Ensure delete_document removes not only document/edge rows but also claims,
/// because claim has no FK to document./// Ensure delete_document removes not only document/edge rows but also claims,
/// because claim has no FK to document.
#[tokio::test]
async fn delete_document_removes_claims() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let db = connect(&dsn).await;
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let path = unique_path("delete-claim-test");
    let mut front = dummy_frontmatter(&path);
    front.claims.push(Claim {
        subject: "test-subject".to_string(),
        predicate: "has".to_string(),
        value: "value".to_string(),
        kind: "fact".to_string(),
        confidence: "certain".to_string(),
        said_by: None,
    });

    store
        .upsert_document(&front, "sha1", SystemTime::now())
        .await
        .expect("upsert document");
    store
        .upsert_claim(
            &front.claims[0].subject,
            &front.claims[0].predicate,
            &front.claims[0].value,
            &path,
            SystemTime::now(),
            &[0.0_f32; 1024],
            &front.claims[0].kind,
            &front.claims[0].confidence,
        )
        .await
        .expect("upsert claim");

    assert_eq!(
        count_claims(&db, &path).await,
        1,
        "claim should exist before delete"
    );

    store.delete_document(&path).await.expect("delete document");

    assert_eq!(
        count_claims(&db, &path).await,
        0,
        "claim should be removed with document"
    );
}

/// nearest_document returns the closest document only when within the distance threshold.
#[tokio::test]
async fn nearest_document_respects_threshold() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let a_path = unique_path("nearest-a");
    let b_path = unique_path("nearest-b");
    let a_front = dummy_frontmatter(&a_path);
    let b_front = dummy_frontmatter(&b_path);
    store
        .upsert_document(&a_front, "sha-a", SystemTime::now())
        .await
        .expect("upsert a");
    store
        .upsert_document(&b_front, "sha-b", SystemTime::now())
        .await
        .expect("upsert b");

    let mut emb_a = [0.0_f32; 1024];
    emb_a[0] = 1.0;
    let mut emb_b = [0.0_f32; 1024];
    emb_b[1] = 1.0;

    store
        .upsert_chunk(&Doc {
            id: format!("{a_path}#0"),
            content: "A note".to_string(),
            embedding: emb_a.to_vec(),
            front: a_front.clone(),
            chunk_idx: 0,
        })
        .await
        .expect("chunk a");
    store
        .upsert_chunk(&Doc {
            id: format!("{b_path}#0"),
            content: "B note".to_string(),
            embedding: emb_b.to_vec(),
            front: b_front.clone(),
            chunk_idx: 0,
        })
        .await
        .expect("chunk b");

    // Query close to A → should return A.
    let mut query_near_a = [0.0_f32; 1024];
    query_near_a[0] = 0.9;
    query_near_a[1] = 0.1;
    let near = store
        .nearest_document(&query_near_a, 0.2)
        .await
        .expect("nearest")
        .map(|(p, _)| p);
    assert_eq!(near, Some(a_path.clone()), "query near A should return A");

    // Distant query with tight threshold → none.
    let far = store
        .nearest_document(&[0.5_f32; 1024], 0.01)
        .await
        .expect("nearest far");
    assert!(far.is_none(), "distant query below threshold returns none");

    store.delete_document(&a_path).await.expect("cleanup a");
    store.delete_document(&b_path).await.expect("cleanup b");
}

/// Claims can carry kind/confidence and be filtered by kind.
#[tokio::test]
async fn claim_kind_and_confidence_round_trip() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("claim-kind");
    let mut front = dummy_frontmatter(&path);
    front.project = "omb".to_owned();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert doc");

    let emb = [0.1_f32; 1024];
    store
        .upsert_claim(
            "omb",
            "release-version",
            "0.2.0",
            &path,
            SystemTime::now(),
            &emb,
            "decision",
            "certain",
        )
        .await
        .expect("upsert decision claim");
    store
        .upsert_claim(
            "omb",
            "auth-flow",
            "unverified",
            &path,
            SystemTime::now(),
            &emb,
            "risk",
            "likely",
        )
        .await
        .expect("upsert risk claim");

    let decisions = store
        .recent_claims(10, Some("omb"), Some(&["decision".to_owned()]), &[])
        .await
        .expect("recent decisions");
    assert_eq!(decisions.len(), 1);
    assert_eq!(decisions[0].kind(), "decision");
    assert_eq!(decisions[0].confidence(), "certain");

    let risks = store
        .recent_claims(
            10,
            Some("omb"),
            Some(&[
                "risk".to_owned(),
                "assumption".to_owned(),
                "blocked".to_owned(),
            ]),
            &[],
        )
        .await
        .expect("recent risks");
    assert_eq!(risks.len(), 1);
    assert_eq!(risks[0].kind(), "risk");

    store.delete_document(&path).await.expect("cleanup");
}

/// `next` claims are stored and filterable alongside blockers.
#[tokio::test]
async fn next_claim_is_recallable() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("claim-next");
    let mut front = dummy_frontmatter(&path);
    front.project = "omb".to_owned();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert doc");

    let emb = [0.1_f32; 1024];
    store
        .upsert_claim(
            "omb",
            "follow-up",
            "add next_actions endpoint",
            &path,
            SystemTime::now(),
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("upsert next claim");

    let nexts = store
        .recent_claims(
            10,
            Some("omb"),
            Some(&["next".to_owned(), "blocked".to_owned()]),
            &[],
        )
        .await
        .expect("recent next actions");
    assert_eq!(nexts.len(), 1);
    assert_eq!(nexts[0].kind(), "next");
    assert_eq!(nexts[0].predicate, "follow-up");

    store.delete_document(&path).await.expect("cleanup");
}

/// Register queries carry the full match count alongside the limited rows, so the cut is stated.
#[tokio::test]
async fn recent_register_rows_report_total_matching_beyond_the_limit() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("register-recent");
    let mut front = dummy_frontmatter(&path);
    front.project = "register-test".to_owned();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert doc");

    let emb = [0.1_f32; 1024];
    for i in 0u32..3 {
        store
            .upsert_claim(
                "omb",
                &format!("decision-{i}"),
                "0.2.0",
                &path,
                SystemTime::now() + Duration::from_secs(u64::from(i)),
                &emb,
                "decision",
                "certain",
            )
            .await
            .expect("upsert decision claim");
    }

    let res = store
        .recent_register_rows(
            2,
            Some("register-test"),
            Some(&["decision".to_owned()]),
            &[],
        )
        .await
        .expect("recent register rows");
    assert_eq!(res.rows.len(), 2);
    assert_eq!(res.total_matching, 3);
    assert_eq!(res.rows[0].node_id, "claim:omb:decision-2");
    assert_eq!(res.rows[0].project, "register-test");

    store.delete_document(&path).await.expect("cleanup");
}

/// A row that says something outranks a newer row that only labels something.
///
/// The session-start card showed four risks in a row reading `ohmyboring incident: <fragment>`
/// (measured 2026-09-20: 647 of 766 current risk claims carry the predicate `incident`, and 472
/// carry a value under 25 characters). Recency alone put those first because they were the
/// newest, so the most privileged slot in the product — what the agent reads before anything
/// else — was spent on labels.
#[tokio::test]
async fn recent_register_rows_put_the_informative_row_first() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    // A project name shared with another run is a shared fixture: the first failing run leaves its
    // rows behind (the cleanup sits after the assert) and the next run reads them as its own.
    let path = unique_path("register-informative");
    let project = path
        .rsplit('/')
        .next()
        .unwrap_or("informative-test")
        .to_owned();
    let mut front = dummy_frontmatter(&path);
    front.project = project.clone();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert doc");

    let emb = [0.1_f32; 1024];
    let now = SystemTime::now();
    // Oldest, but it is a sentence and its predicate names something.
    store
        .upsert_claim(
            "pool sizing",
            "chosen-bound",
            "max 8 connections with a 30s idle timeout, measured against the 502 spike",
            &path,
            now,
            &emb,
            "risk",
            "certain",
        )
        .await
        .expect("informative claim");
    // Newer, but the predicate restates the kind.
    store
        .upsert_claim(
            "ohmyboring",
            "incident",
            "the retry loop handed back a socket the pool had already closed",
            &path,
            now + Duration::from_secs(10),
            &emb,
            "risk",
            "certain",
        )
        .await
        .expect("tautological-predicate claim");
    // Newest of all, and both a label and a fragment. A different subject, because a claim is
    // keyed by (subject, predicate) — reusing the pair would supersede the row above instead of
    // adding a third one.
    store
        .upsert_claim(
            "ohmyboring web",
            "incident",
            "mismatch",
            &path,
            now + Duration::from_secs(20),
            &emb,
            "risk",
            "certain",
        )
        .await
        .expect("fragment claim");

    let res = store
        .recent_register_rows(3, Some(&project), Some(&["risk".to_owned()]), &[])
        .await
        .expect("recent register rows");

    let order: Vec<&str> = res.rows.iter().map(|r| r.predicate.as_str()).collect();
    assert_eq!(
        order,
        vec!["chosen-bound", "incident", "incident"],
        "a naming predicate outranks a restating one"
    );
    assert_eq!(
        res.rows[2].value, "mismatch",
        "the row that is both a label and a fragment goes last, and is still returned"
    );
    assert_eq!(
        res.total_matching, 3,
        "demotion is ordering, never filtering"
    );

    store.delete_document(&path).await.expect("cleanup");
}

/// The session-start card orders like the registers do.
///
/// `recent_claims` is what `ask::context_card` calls; `recent_register_rows` is what the MCP
/// registers call. The demotion rule shipped in the second one while the card — the surface that
/// showed four consecutive `ohmyboring incident: <fragment>` rows and the reason the rule exists —
/// kept its recency-only order. This test reads the card's own path.
#[tokio::test]
async fn recent_claims_put_the_informative_row_first() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("card-informative");
    let project = path.rsplit('/').next().unwrap_or("card-test").to_owned();
    let mut front = dummy_frontmatter(&path);
    front.project = project.clone();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert doc");

    let emb = [0.1_f32; 1024];
    let now = SystemTime::now();
    store
        .upsert_claim(
            "pool sizing",
            "chosen-bound",
            "max 8 connections with a 30s idle timeout, after the 502 spike",
            &path,
            now,
            &emb,
            "risk",
            "certain",
        )
        .await
        .expect("informative claim");
    store
        .upsert_claim(
            "ohmyboring",
            "incident",
            "the retry loop handed back a socket the pool had already closed",
            &path,
            now + Duration::from_secs(10),
            &emb,
            "risk",
            "certain",
        )
        .await
        .expect("tautological claim");

    let rows = store
        .recent_claims(2, Some(&project), Some(&["risk".to_owned()]), &[])
        .await
        .expect("recent claims");

    assert_eq!(
        rows.iter()
            .map(|c| c.predicate.as_str())
            .collect::<Vec<_>>(),
        vec!["chosen-bound", "incident"],
        "the card's own query has to demote the label, not just the registers' query"
    );

    store.delete_document(&path).await.expect("cleanup");
}

/// The corpus counts the claims that only label something, so the weakness is visible and not
/// merely sorted to the bottom of a register.
///
/// Sorting them last fixes what a reader sees now; it says nothing about a corpus drifting that
/// way, and on 2026-09-20 it had — 647 of 769 risk claims restated their kind in the predicate.
#[tokio::test]
async fn claim_era_counts_count_the_rows_that_only_label() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("label-only-count");
    let mut front = dummy_frontmatter(&path);
    front.project = path.rsplit('/').next().unwrap_or("label-test").to_owned();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert doc");

    let before = store.claim_era_counts().await.expect("era counts before");
    let emb = [0.1_f32; 1024];
    let now = SystemTime::now();
    // Says something: a predicate that names, a value that is a sentence.
    store
        .upsert_claim(
            "pool sizing",
            "chosen-bound",
            "max 8 connections with a 30s idle timeout, after the 502 spike",
            &path,
            now,
            &emb,
            "decision",
            "certain",
        )
        .await
        .expect("informative claim");
    // Labels something: the predicate restates the kind.
    store
        .upsert_claim(
            "ohmyboring",
            "incident",
            "the retry loop handed back a socket the pool had already closed",
            &path,
            now,
            &emb,
            "risk",
            "certain",
        )
        .await
        .expect("tautological claim");
    // Labels something: the value is a tag.
    store
        .upsert_claim(
            "ohmyboring web",
            "observed-effect",
            "mismatch",
            &path,
            now,
            &emb,
            "risk",
            "certain",
        )
        .await
        .expect("fragment claim");

    let after = store.claim_era_counts().await.expect("era counts after");
    assert_eq!(
        after.label_only - before.label_only,
        2,
        "the tautological predicate and the fragment value each count; the sentence does not"
    );

    store.delete_document(&path).await.expect("cleanup");
}

/// Two notes that settled the same question find each other, even sharing no concept.
///
/// The `claims` edge was written 6,921 times and walked by nothing: related-note retrieval
/// followed `about` edges only. Measured 2026-09-20 on the live corpus — 11,123 document pairs
/// share a claim node and 10,886 of them (98%) share no two concepts, so the strongest link in
/// the graph was invisible to the surface that asks "what does this continue".
#[tokio::test]
async fn related_follows_a_shared_claim_with_no_shared_concept() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let subject = unique_subject("shared-ground");
    let older_path = format!("/vault/wiki/{subject}-older.md");
    let newer_path = format!("/vault/wiki/{subject}-newer.md");
    let emb = [0.1_f32; 1024];

    // The older note settles a question. No tags, no concepts — only the claim.
    let older = claim_note(&older_path, &subject);
    store
        .upsert_document(
            &older,
            "sha-older",
            SystemTime::now() - Duration::from_mins(10),
        )
        .await
        .expect("older doc");
    store
        .upsert_chunk(&Doc {
            id: format!("{older_path}#0"),
            content: "the pool bound was settled here".to_string(),
            embedding: emb.to_vec(),
            front: older.clone(),
            chunk_idx: 0,
        })
        .await
        .expect("older chunk");
    store
        .upsert_claim_node(
            &older_path,
            "",
            &older.claims[0].subject,
            &older.claims[0].predicate,
            &older.claims[0],
        )
        .await
        .expect("older claim node");

    // The newer note answers the same question again — same subject and predicate.
    let newer = claim_note(&newer_path, &subject);
    store
        .upsert_document(&newer, "sha-newer", SystemTime::now())
        .await
        .expect("newer doc");
    store
        .upsert_chunk(&Doc {
            id: format!("{newer_path}#0"),
            content: "the pool bound came up again".to_string(),
            embedding: emb.to_vec(),
            front: newer.clone(),
            chunk_idx: 0,
        })
        .await
        .expect("newer chunk");
    store
        .upsert_claim_node(
            &newer_path,
            "",
            &newer.claims[0].subject,
            &newer.claims[0].predicate,
            &newer.claims[0],
        )
        .await
        .expect("newer claim node");

    let related = store
        .related_by_shared_ground(&newer_path, 5)
        .await
        .expect("related by shared ground");

    assert!(
        related.iter().any(|d| d.source_path == older_path),
        "the older note answering the same question has to be reachable; got {:?}",
        related
            .iter()
            .map(|d| d.source_path.as_str())
            .collect::<Vec<_>>()
    );

    store
        .delete_document(&older_path)
        .await
        .expect("cleanup older");
    store
        .delete_document(&newer_path)
        .await
        .expect("cleanup newer");
}

/// The stalled register window matches `stalled_claims` and also reports the full match count.
#[tokio::test]
async fn stalled_register_rows_report_total_matching_within_the_window() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let ten_days = Duration::from_hours(10 * 24);
    let emb = [0.1_f32; 1024];
    let project = unique_path("register-stalled-p");
    let mut paths = vec![];
    for i in 0u32..3 {
        let path = unique_path(&format!("register-stalled-{i}"));
        let mut front = dummy_frontmatter(&path);
        front.project.clone_from(&project);
        store
            .upsert_document(&front, "sha", SystemTime::now())
            .await
            .expect("upsert doc");
        store
            .upsert_claim(
                "omb",
                &format!("next-{i}"),
                "follow up",
                &path,
                SystemTime::now() - ten_days + Duration::from_secs(u64::from(i)),
                &emb,
                "next",
                "likely",
            )
            .await
            .expect("upsert next claim");
        paths.push(path);
    }

    let res = store
        .stalled_register_rows(2, Some(&project), Some(&["next".to_owned()]), &[], 7)
        .await
        .expect("stalled register rows");
    assert_eq!(res.rows.len(), 2);
    assert_eq!(res.total_matching, 3);
    assert_eq!(
        res.rows
            .iter()
            .map(|r| r.predicate.as_str())
            .collect::<Vec<_>>(),
        vec!["next-0", "next-1"],
        "a stalled register leads with what has been frozen longest, not with what stalled most recently"
    );

    for path in paths {
        store.delete_document(&path).await.expect("cleanup");
    }
}

/// The register tools answer from rows: structured items carry the claim node id, the answer
/// states the cut, and an empty claim set keeps the none-recorded message.
#[tokio::test]
async fn decision_register_answers_from_rows() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("register-fn");
    let mut front = dummy_frontmatter(&path);
    front.project = "register-test".to_owned();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert doc");

    let emb = [0.1_f32; 1024];
    for (subject, predicate) in [("omb", "release-version"), ("omb", "auth-flow")] {
        store
            .upsert_claim(
                subject,
                predicate,
                "0.2.0",
                &path,
                SystemTime::now(),
                &emb,
                "decision",
                "certain",
            )
            .await
            .expect("upsert decision claim");
    }

    let out = drudge::ask::decision_register(&store, Some("register-test"), &[], 50)
        .await
        .expect("decision register");
    assert!(
        out.answer
            .starts_with("Showing 2 of 2 matching claims (limit_applied=false)."),
        "was {:?}",
        out.answer
    );
    assert_eq!(out.items.len(), 2);
    for item in &out.items {
        assert_eq!(
            item.node_id,
            format!("claim:{}:{}", item.subject, item.predicate)
        );
    }
    assert_eq!(out.sources, vec!["omb".to_owned()]);

    let narrowed = drudge::ask::decision_register(&store, Some("register-test"), &[], 1)
        .await
        .expect("decision register with limit 1");
    assert_eq!(narrowed.items.len(), 1);
    assert!(narrowed.limit_applied);
    assert_eq!(narrowed.total_matching, 2);

    let empty = drudge::ask::decision_register(&store, Some("no-such-project"), &[], 50)
        .await
        .expect("decision register on empty project");
    assert_eq!(empty.answer, "No decisions recorded yet.");
    assert!(empty.items.is_empty());
    assert!(!empty.limit_applied);
    assert_eq!(empty.total_matching, 0);

    store.delete_document(&path).await.expect("cleanup");
}

/// The registers take an origin filter. They are also the only surface that used to bind it and
/// throw it away, so the filter is pinned at the register rather than only at the store.
#[tokio::test]
async fn decision_register_honours_the_origin_filter() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let emb = [0.1_f32; 1024];
    let mut paths = vec![];
    for origin in ["personal", "company"] {
        let path = unique_path(&format!("register-origin-{origin}"));
        let mut front = dummy_frontmatter(&path);
        front.project = "origin-test".to_owned();
        front.origin = origin.to_owned();
        store
            .upsert_document(&front, "sha", SystemTime::now())
            .await
            .expect("upsert doc");
        store
            .upsert_claim(
                origin,
                "decision",
                "shipped",
                &path,
                SystemTime::now(),
                &emb,
                "decision",
                "certain",
            )
            .await
            .expect("upsert decision claim");
        paths.push(path);
    }

    let unfiltered = drudge::ask::decision_register(&store, Some("origin-test"), &[], 50)
        .await
        .expect("decision register");
    assert_eq!(unfiltered.items.len(), 2);

    let filtered =
        drudge::ask::decision_register(&store, Some("origin-test"), &["company".to_owned()], 50)
            .await
            .expect("decision register with origin filter");
    assert_eq!(
        filtered
            .items
            .iter()
            .map(|i| i.subject.as_str())
            .collect::<Vec<_>>(),
        vec!["personal"],
        "a company-origin claim must not reach a register asked to exclude it"
    );
    assert_eq!(filtered.total_matching, 1);

    for path in paths {
        store.delete_document(&path).await.expect("cleanup");
    }
}

/// Regression test for the 2026-07-25 outage: a single `tokio_postgres::Client` wedged permanently
/// once its underlying connection died, and every subsequent write failed silently for 5.5 days.
/// `Store` now holds a `deadpool_postgres::Pool` (`RecyclingMethod::Verified`), which must
/// transparently reconnect — both reads and writes must keep working after the connection is killed.
///
/// Scope guard on the kill: only backends for `current_database()`, excluding the admin connection
/// issuing the kill (`pid <> pg_backend_pid()`) and restricted to `backend_type = 'client backend'` —
/// this must never terminate another database's connections or non-client backends on a shared
/// Postgres instance.
#[tokio::test]
async fn pool_recovers_after_connection_kill() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    // Baseline: pool is functional before the kill.
    store
        .liveness_probe()
        .await
        .expect("baseline liveness probe");
    let path = unique_path("pool-kill");
    let front = dummy_frontmatter(&path);
    store
        .upsert_document(&front, "sha-before-kill", SystemTime::now())
        .await
        .expect("baseline write should succeed");

    // Terminate every backend the pool holds for this database, from a separate admin connection.
    let admin = connect(&dsn).await;
    let killed = admin
        .execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
             WHERE datname = current_database() AND pid <> pg_backend_pid() \
             AND backend_type = 'client backend';",
            &[],
        )
        .await
        .expect("terminate backends");
    // Assert on the kill's own return value, not just a pre-kill row count — asserting only the
    // latter and discarding this value let a 0-connections-actually-killed run pass as green before.
    assert!(
        killed > 0,
        "kill must actually terminate at least one backend, or this test is vacuous"
    );

    // Both a write and a read must succeed post-kill — the pool must transparently reconnect.
    store
        .upsert_document(&front, "sha-after-kill", SystemTime::now())
        .await
        .expect("write after connection kill should succeed (pool must reconnect)");
    let sha = store
        .get_doc_sha(&path)
        .await
        .expect("read after connection kill should succeed (pool must reconnect)");
    assert_eq!(sha.as_deref(), Some("sha-after-kill"));

    store.delete_document(&path).await.expect("cleanup");
}

/// Control group for `pool_recovers_after_connection_kill`: a bare, un-pooled `tokio_postgres::Client`
/// (the pre-fix design) does NOT recover from its connection being terminated. This is the shape of
/// the code that caused the 5.5-day outage — nothing in it re-establishes the connection.
#[tokio::test]
async fn bare_client_does_not_recover_after_connection_kill() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let target = connect(&dsn).await;
    let admin = connect(&dsn).await;

    let pid: i32 = target
        .query_one("SELECT pg_backend_pid();", &[])
        .await
        .expect("get target pid")
        .get(0);

    let killed = admin
        .execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
             WHERE pid = $1 AND backend_type = 'client backend';",
            &[&pid],
        )
        .await
        .expect("terminate target backend");
    assert_eq!(
        killed, 1,
        "kill must terminate exactly the target connection"
    );

    let result = target.query_one("SELECT 1;", &[]).await;
    assert!(
        result.is_err(),
        "a bare tokio_postgres::Client must NOT recover from a terminated connection — this is \
         the regression the deadpool-postgres pool (Store::pool) fixes"
    );
}

/// A stalled report is about work that stalled recently enough to still be the same work, and one
/// note should not be able to fill the list on its own.
#[tokio::test]
async fn stalled_claims_stop_at_the_horizon_and_take_one_row_per_note() {
    // Two defects that made the same twelve items the briefing's "stalled" list every morning.
    //
    // No lower bound: oldest-first with an open floor pinned claims from 2026-06-30 to the top
    // slots for 65 days. 497 of 548 current `next` claims were over a week old, so the bucket
    // never emptied and never rotated. Past the horizon an item is not stalled, it is abandoned.
    //
    // No per-note limit: one note that emitted several next-steps took several of the twelve
    // slots. `wiki-0231` held two, `fds-16220` and `fds 16220` — one piece of work under two
    // spellings of the same axis.
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let project = format!("horizon-{stamp}");
    let emb = [0.1_f32; 1024];
    let ago = |hours: u64| {
        SystemTime::now()
            .checked_sub(Duration::from_hours(hours))
            .expect("valid timestamp")
    };

    // One note, two next-steps, both inside the window.
    let busy = unique_path("claim-horizon-busy");
    let mut front = dummy_frontmatter(&busy);
    front.project = project.clone();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert busy doc");
    for (pred, value, hours) in [
        ("first-step", "the older of the two", 24 * 20),
        ("second-step", "same note, same axis restated", 24 * 15),
    ] {
        store
            .upsert_claim(
                &project,
                pred,
                value,
                &busy,
                ago(hours),
                &emb,
                "next",
                "certain",
            )
            .await
            .expect("upsert busy claim");
    }

    // A second note, well past the horizon.
    let ancient = unique_path("claim-horizon-ancient");
    let mut front = dummy_frontmatter(&ancient);
    front.project = project.clone();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert ancient doc");
    store
        .upsert_claim(
            &project,
            "abandoned",
            "older than the horizon",
            &ancient,
            ago(24 * 90),
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("upsert ancient claim");

    let stalled = store
        .stalled_claims(10, Some(&project), Some(&["next".to_owned()]), &[], 7)
        .await
        .expect("stalled claims");

    assert_eq!(
        stalled.len(),
        1,
        "one row per note, and nothing past the horizon: {stalled:?}"
    );
    assert_eq!(
        stalled[0].predicate, "first-step",
        "the oldest claim inside the window represents its note"
    );

    store.delete_document(&busy).await.expect("cleanup busy");
    store
        .delete_document(&ancient)
        .await
        .expect("cleanup ancient");
}

#[tokio::test]
/// Stalled backlog should respect the requested action kinds; old decisions stay in the decision register.
async fn stalled_claims_honor_requested_kinds() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("claim-stalled");
    let project = format!(
        "stalled-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let mut front = dummy_frontmatter(&path);
    front.project = project.clone();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert doc");

    let older = SystemTime::now()
        .checked_sub(Duration::from_hours(192))
        .expect("valid older timestamp");
    let emb = [0.1_f32; 1024];
    store
        .upsert_claim(
            &project,
            "follow-up",
            "ship release checklist",
            &path,
            older,
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("upsert next claim");
    store
        .upsert_claim(
            &project,
            "release-decision",
            "keep stable wiki ids",
            &path,
            older,
            &emb,
            "decision",
            "certain",
        )
        .await
        .expect("upsert decision claim");

    let stalled = store
        .stalled_claims(
            10,
            Some(&project),
            Some(&["next".to_owned(), "blocked".to_owned()]),
            &[],
            7,
        )
        .await
        .expect("stalled claims");
    assert_eq!(stalled.len(), 1);
    assert_eq!(stalled[0].kind(), "next");
    assert_eq!(stalled[0].predicate, "follow-up");

    store.delete_document(&path).await.expect("cleanup");
}

/// query_log must keep "distance 0.0" and "no distance" distinct. 0.0 is a valid cosine distance
/// (identical vector), while absence means the hit came from a source with no comparable signal.
#[tokio::test]
async fn query_log_preserves_zero_distance_and_absence_distinctly() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let endpoint = format!(
        "dist-roundtrip-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let p1 = unique_path("dist-zero");
    let p2 = unique_path("dist-absent");

    store
        .log_query(
            &endpoint,
            "test query",
            &[
                LoggedHit::with_distance(&p1, 0.0, DistKind::VectorCosine),
                LoggedHit::without_distance(&p2),
            ],
            &[],
            "snippet",
            None,
        )
        .await
        .expect("log query");

    let rows = store.recent_queries(10).await.expect("recent queries");
    let row = rows
        .iter()
        .find(|r| r.endpoint == endpoint)
        .expect("row present");

    assert_eq!(row.hit_paths, vec![p1.clone(), p2.clone()]);
    assert_eq!(row.hit_dists.len(), 2);
    assert_eq!(row.hit_dists[0], Some(0.0));
    assert!(row.hit_dists[1].is_none(), "absent distance must be None");
    assert_eq!(row.hit_dist_kinds.len(), 2);
    assert_eq!(row.hit_dist_kinds[0].as_deref(), Some("vector_cosine"));
    assert!(row.hit_dist_kinds[1].is_none(), "absent kind must be None");

    let db = connect(&dsn).await;
    db.execute("DELETE FROM query_log WHERE endpoint = $1;", &[&endpoint])
        .await
        .expect("cleanup query_log");
}

/// Seed one logged query with two hits and return its id. Shared by the label tests.
async fn seed_labelled_query(store: &Store, endpoint: &str) -> i32 {
    store
        .log_query(
            endpoint,
            "labelled query",
            &[
                LoggedHit::with_distance(unique_path("label-hit-a"), 0.31, DistKind::VectorCosine),
                LoggedHit::with_distance(unique_path("label-hit-b"), 0.52, DistKind::VectorCosine),
            ],
            &[],
            "snippet",
            None,
        )
        .await
        .expect("log query");
    store
        .recent_queries(10)
        .await
        .expect("recent queries")
        .into_iter()
        .find(|r| r.endpoint == endpoint)
        .expect("row present")
        .id
}

fn unique_endpoint(prefix: &str) -> String {
    format!(
        "{prefix}-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    )
}

/// Both judges' verdicts on one hit live side by side, and re-labelling replaces only the row of
/// the judge doing it. A human audit that overwrote the llm row would erase the disagreement the
/// audit exists to measure.
#[tokio::test]
async fn recall_labels_keep_both_judges_independent() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let endpoint = unique_endpoint("label-independent");
    let qid = seed_labelled_query(&store, &endpoint).await;

    store
        .record_recall_label(qid, 0, "llm", "relevant", "gemma4:12b", "")
        .await
        .expect("llm label");
    store
        .record_recall_label(qid, 0, "human", "irrelevant", "human", "off topic")
        .await
        .expect("human label");

    let mine = |labels: Vec<drudge::store::RecallLabelRow>| {
        labels
            .into_iter()
            .filter(|l| l.query_log_id == qid)
            .collect::<Vec<_>>()
    };
    let rows = mine(store.recent_recall_labels(200).await.expect("labels"));
    assert_eq!(
        rows.len(),
        2,
        "a human verdict must not overwrite the llm's"
    );
    let llm = rows
        .iter()
        .find(|l| l.judge == "llm")
        .expect("llm verdict present");
    assert_eq!(llm.verdict, "relevant");
    assert_eq!(llm.model, "gemma4:12b");

    store
        .record_recall_label(qid, 0, "llm", "irrelevant", "gemma4:12b", "second pass")
        .await
        .expect("relabel");
    let rows = mine(store.recent_recall_labels(200).await.expect("labels again"));
    assert_eq!(rows.len(), 2, "re-labelling must update, not append");
    assert_eq!(
        rows.iter()
            .find(|l| l.judge == "llm")
            .expect("llm verdict")
            .verdict,
        "irrelevant"
    );
    assert_eq!(
        rows.iter()
            .find(|l| l.judge == "human")
            .expect("human verdict")
            .verdict,
        "irrelevant",
        "the human row must be untouched by the llm relabel"
    );

    let db = connect(&dsn).await;
    db.execute("DELETE FROM query_log WHERE endpoint = $1;", &[&endpoint])
        .await
        .expect("cleanup query_log");
}

/// An `unsure` verdict is an abstention: it leaves the agreement denominator entirely. Counting it
/// either way would move the rate that decides whether the llm judge is usable at all. Labels also
/// cascade with the query they describe — a label pointing at a pruned query is unauditable.
#[tokio::test]
async fn recall_label_agreement_excludes_unsure_and_cascades() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let endpoint = unique_endpoint("label-agreement");
    let qid = seed_labelled_query(&store, &endpoint).await;
    let (_, agreed_before, compared_before) = store
        .recall_label_stats()
        .await
        .expect("baseline stats before labelling");

    // hit 0: both judges decided. hit 1: the person abstained.
    for (hit, judge, verdict) in [
        (0, "llm", "relevant"),
        (0, "human", "relevant"),
        (1, "llm", "relevant"),
        (1, "human", "unsure"),
    ] {
        store
            .record_recall_label(qid, hit, judge, verdict, "test", "")
            .await
            .expect("label");
    }

    let (judges, agreed_after, compared_after) = store.recall_label_stats().await.expect("stats");
    assert!(
        judges.iter().any(|j| j.judge == "llm") && judges.iter().any(|j| j.judge == "human"),
        "both judges must appear in the stats"
    );
    // Deltas against the pre-seed baseline, so this asserts on the store's own aggregation rather
    // than on a copy of its SQL (a copy would pass no matter what the shipped query did). Two hits
    // were labelled by both judges; only hit 0 is decided on both sides.
    assert_eq!(
        compared_after - compared_before,
        1,
        "the unsure pair must not enter the agreement denominator"
    );
    assert_eq!(
        agreed_after - agreed_before,
        1,
        "hit 0 matched on both sides and must count as agreement"
    );

    let db = connect(&dsn).await;
    db.execute("DELETE FROM query_log WHERE endpoint = $1;", &[&endpoint])
        .await
        .expect("cleanup query_log");
    let leftover: i64 = db
        .query_one(
            "SELECT count(*) FROM recall_label WHERE query_log_id = $1;",
            &[&qid],
        )
        .await
        .expect("count leftovers")
        .get(0);
    assert_eq!(
        leftover, 0,
        "labels must cascade with the query they describe"
    );
}

/// Legacy query_log rows (paths only) must read back with empty distance arrays, never zeros.
#[tokio::test]
async fn query_log_legacy_rows_read_empty_distances() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let endpoint = format!(
        "dist-legacy-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let p1 = unique_path("dist-legacy-a");
    let p2 = unique_path("dist-legacy-b");

    let db = connect(&dsn).await;
    db.execute(
        "INSERT INTO query_log (endpoint, query, hit_paths, sources, answer_snippet, latency_ms)
         VALUES ($1, $2, $3, $4, $5, $6);",
        &[
            &endpoint,
            &"legacy query",
            &vec![p1.clone(), p2.clone()],
            &Vec::<String>::new(),
            &"snippet",
            &None::<i32>,
        ],
    )
    .await
    .expect("insert legacy row");

    let rows = store.recent_queries(10).await.expect("recent queries");
    let row = rows
        .iter()
        .find(|r| r.endpoint == endpoint)
        .expect("row present");

    assert_eq!(row.hit_paths, vec![p1, p2]);
    assert!(
        row.hit_dists.is_empty(),
        "legacy row must read back empty hit_dists, not zeros"
    );
    assert!(
        row.hit_dist_kinds.is_empty(),
        "legacy row must read back empty hit_dist_kinds"
    );

    db.execute("DELETE FROM query_log WHERE endpoint = $1;", &[&endpoint])
        .await
        .expect("cleanup query_log");
}

/// A re-asserted claim that says exactly the same thing is not a new version.
///
/// Re-ingesting a note re-asserts every claim in it, and `valid_from` is the note's mtime, so
/// editing one line used to write a fresh row — and a fresh 1024-dim embedding — for every
/// unrelated claim in that note. Measured before this guard: 36,421 of 55,498 rows (66%) were
/// byte-identical re-writes, and `claim` was 393 MB of a 744 MB database.
#[tokio::test]
async fn an_identical_claim_is_not_a_new_version() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let subject = format!("unchanged-probe-{}", std::process::id());
    let path = format!("/vault/wiki/{subject}.md");

    let unchanged = store
        .claim_is_unchanged(&subject, "axis", "v1", &path, "fact", "certain", None)
        .await
        .expect("probe");
    assert!(
        !unchanged,
        "nothing stored yet, so nothing can be unchanged"
    );

    store
        .upsert_claim(
            &subject,
            "axis",
            "v1",
            &path,
            SystemTime::now(),
            &[0.0_f32; 1024],
            "fact",
            "certain",
        )
        .await
        .expect("first insert");

    assert!(
        store
            .claim_is_unchanged(&subject, "axis", "v1", &path, "fact", "certain", None)
            .await
            .expect("probe"),
        "the same tuple must read as unchanged"
    );

    // Each of these is new information, so none may read as unchanged: a different value
    // supersedes, a different note is new provenance, and kind/confidence are part of the claim.
    for (value, src, kind, conf, why) in [
        ("v2", path.as_str(), "fact", "certain", "a changed value"),
        (
            "v1",
            "/vault/wiki/other.md",
            "fact",
            "certain",
            "a different note",
        ),
        ("v1", path.as_str(), "decision", "certain", "a changed kind"),
        (
            "v1",
            path.as_str(),
            "fact",
            "likely",
            "a changed confidence",
        ),
    ] {
        assert!(
            !store
                .claim_is_unchanged(&subject, "axis", value, src, kind, conf, None)
                .await
                .expect("probe"),
            "{why} must not read as unchanged"
        );
    }

    // A value that was superseded and then came back must NOT read as unchanged. The probe
    // asks only about this note's own latest row — v2 — so the returning v1 reads as changed,
    // is inserted, and the slot follows the note on disk. Matching the older sealed row here
    // would leave the store answering v2 while the note says v1.
    store
        .upsert_claim(
            &subject,
            "axis",
            "v2",
            &path,
            SystemTime::now(),
            &[0.0_f32; 1024],
            "fact",
            "certain",
        )
        .await
        .expect("supersede with v2");
    assert!(
        !store
            .claim_is_unchanged(&subject, "axis", "v1", &path, "fact", "certain", None)
            .await
            .expect("probe"),
        "a sealed value returning is new information, not an unchanged claim"
    );
    assert!(
        store
            .claim_is_unchanged(&subject, "axis", "v2", &path, "fact", "certain", None)
            .await
            .expect("probe"),
        "the current value must still read as unchanged"
    );

    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("cleanup claim");
}

#[tokio::test]
async fn a_claim_whose_anchor_would_change_is_not_unchanged() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let subject = format!("anchor-shift-probe-{}", std::process::id());
    let path = format!("/vault/wiki/{subject}.md");
    let stored_anchor = "test:a/b.rs:L10";

    store
        .upsert_claim_with_anchor(
            &subject,
            "axis",
            "v1",
            &path,
            SystemTime::now(),
            &[0.0_f32; 1024],
            "fact",
            "certain",
            Some(stored_anchor),
            None,
            None,
        )
        .await
        .expect("insert anchored claim");

    assert!(
        !store
            .claim_is_unchanged(
                &subject,
                "axis",
                "v1",
                &path,
                "fact",
                "certain",
                Some("test:c/d.rs:L20"),
            )
            .await
            .expect("probe with a different anchor"),
        "a different anchor must not read as unchanged"
    );

    assert!(
        store
            .claim_is_unchanged(
                &subject,
                "axis",
                "v1",
                &path,
                "fact",
                "certain",
                Some(stored_anchor)
            )
            .await
            .expect("probe with the same anchor"),
        "the same anchor must still read as unchanged"
    );

    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("cleanup claim");
}

#[tokio::test]
async fn a_claim_that_loses_its_anchor_is_not_unchanged() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let subject = format!("anchor-loss-probe-{}", std::process::id());
    let path = format!("/vault/wiki/{subject}.md");

    store
        .upsert_claim_with_anchor(
            &subject,
            "axis",
            "v1",
            &path,
            SystemTime::now(),
            &[0.0_f32; 1024],
            "fact",
            "certain",
            Some("test:a/b.rs:L10"),
            None,
            None,
        )
        .await
        .expect("insert anchored claim");

    assert!(
        !store
            .claim_is_unchanged(&subject, "axis", "v1", &path, "fact", "certain", None)
            .await
            .expect("probe with no anchor"),
        "losing the anchor must read as changed"
    );

    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("cleanup claim");
}

/// One slot, two notes: A asserts V, B asserts W and takes the slot, so A's row is sealed.
/// Re-syncing A re-asserts V — the exact tuple A already recorded. The probe used to scope to
/// the current row, miss the sealed one, and answer "changed": ingest then inserted a fresh
/// V with a fresh mtime, which took the slot and sealed B; the next sync of B did the same
/// back. Every sync of either note wrote a row forever — measured on the live store: 56,966
/// rows collapsing to 11,251 distinct tuples. A note that has already recorded this exact
/// claim has nothing new to say, whether or not its row currently wins the slot.
#[tokio::test]
async fn a_resynced_note_does_not_reinsert_a_claim_it_recorded() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;

    let subject = format!(
        "resync-probe-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let predicate = "axis".to_string();
    let path_a = unique_path("resync-a");
    let path_b = unique_path("resync-b");
    // Whole seconds: timestamptz round-trips at microsecond precision and the assertion
    // below compares A's valid_from for equality.
    let t_a = UNIX_EPOCH
        + Duration::from_secs(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs(),
        );
    let t_b = t_a + Duration::from_mins(1);
    let emb = [0.0_f32; 1024];

    // 1. Note A records V for the slot.
    store
        .upsert_claim(
            &subject, &predicate, "v", &path_a, t_a, &emb, "fact", "certain",
        )
        .await
        .expect("ingest V from A");
    // 2. Note B records W for the same slot; B's later valid_from takes it, sealing A's row.
    store
        .upsert_claim(
            &subject, &predicate, "w", &path_b, t_b, &emb, "fact", "certain",
        )
        .await
        .expect("ingest W from B");

    // 3. A re-sync, as ingest.rs runs it: probe before writing. A's sealed row matches the
    //    exact tuple, so there is nothing new to record.
    assert!(
        store
            .claim_is_unchanged(&subject, &predicate, "v", &path_a, "fact", "certain", None)
            .await
            .expect("probe"),
        "A already recorded exactly this claim; sealed or not, re-asserting it is nothing new"
    );

    // 4. Exactly two rows for the slot — the re-sync added none — and A's row keeps the
    //    valid_from it was first recorded with.
    let rows: i64 = db
        .query_one(
            "SELECT count(*) FROM claim WHERE subject = $1 AND predicate = $2;",
            &[&subject, &predicate],
        )
        .await
        .expect("count slot rows")
        .get(0);
    assert_eq!(
        rows, 2,
        "re-syncing A must not insert a third row for the slot"
    );
    let valid_from: SystemTime = db
        .query_one(
            "SELECT valid_from FROM claim
             WHERE subject = $1 AND predicate = $2 AND value = 'v' AND source_path = $3;",
            &[&subject, &predicate, &path_a],
        )
        .await
        .expect("A's row still there")
        .get(0);
    assert_eq!(
        valid_from, t_a,
        "A's row keeps the first time V was true, not the re-sync's mtime"
    );

    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("cleanup claim");
}

/// The note on disk is the source of truth, so a note edited back to an old value must update
/// the slot. The probe asks what the note's own latest row says: after v1 -> v2 the latest
/// row is v2, the returning v1 reads as changed, and the insert makes v1 current again.
/// Scoping the probe to "has this note ever recorded this" misses that and the slot keeps
/// answering v2 while the note says v1.
#[tokio::test]
async fn a_note_oscillating_back_updates_the_slot() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;

    let subject = format!(
        "osc-probe-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let predicate = "axis".to_string();
    let path = unique_path("osc-a");
    let t1 = UNIX_EPOCH
        + Duration::from_secs(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs(),
        );
    let t2 = t1 + Duration::from_mins(1);
    let t3 = t2 + Duration::from_mins(1);
    let emb = [0.0_f32; 1024];

    store
        .upsert_claim(
            &subject, &predicate, "v1", &path, t1, &emb, "fact", "certain",
        )
        .await
        .expect("ingest v1");
    store
        .upsert_claim(
            &subject, &predicate, "v2", &path, t2, &emb, "fact", "certain",
        )
        .await
        .expect("ingest v2");

    // The note is edited back to v1, as ingest.rs runs it: probe before writing. The note's
    // own latest row says v2, so the returning v1 is new information and must be inserted.
    assert!(
        !store
            .claim_is_unchanged(&subject, &predicate, "v1", &path, "fact", "certain", None)
            .await
            .expect("probe"),
        "the note's own latest row says v2; the returning v1 must read as changed"
    );
    store
        .upsert_claim(
            &subject, &predicate, "v1", &path, t3, &emb, "fact", "certain",
        )
        .await
        .expect("re-ingest v1");

    let current: String = db
        .query_one(
            "SELECT value FROM claim WHERE subject = $1 AND predicate = $2
             AND superseded_at IS NULL;",
            &[&subject, &predicate],
        )
        .await
        .expect("current row")
        .get(0);
    assert_eq!(current, "v1", "the slot must follow the note on disk");

    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("cleanup claim");
}

/// A slot holds one fact but many items. Note A records `next` X and note B records `next` Y
/// for the same (subject, predicate): both are open work the owner recorded, so both rows must
/// stay current. The old (subject, predicate) seal scope let B's later row evict A's — on the
/// live store that hid 555 distinct next-steps from `next_actions`.
#[tokio::test]
async fn two_notes_next_steps_both_stay_current() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;

    let subject = format!(
        "items-probe-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let predicate = "next-step".to_string();
    let path_a = unique_path("items-a");
    let path_b = unique_path("items-b");
    let t_a = UNIX_EPOCH
        + Duration::from_secs(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs(),
        );
    let t_b = t_a + Duration::from_mins(1);
    let emb = [0.0_f32; 1024];

    store
        .upsert_claim(
            &subject, &predicate, "x", &path_a, t_a, &emb, "next", "certain",
        )
        .await
        .expect("ingest X from A");
    store
        .upsert_claim(
            &subject, &predicate, "y", &path_b, t_b, &emb, "next", "certain",
        )
        .await
        .expect("ingest Y from B");

    let current: i64 = db
        .query_one(
            "SELECT count(*) FROM claim WHERE subject = $1 AND predicate = $2
             AND superseded_at IS NULL;",
            &[&subject, &predicate],
        )
        .await
        .expect("count current rows")
        .get(0);
    assert_eq!(
        current, 2,
        "two notes' next-steps are items, not values of one slot; both stay current"
    );
    let a_sealed: Option<SystemTime> = db
        .query_one(
            "SELECT superseded_at FROM claim WHERE subject = $1 AND predicate = $2
             AND source_path = $3;",
            &[&subject, &predicate, &path_a],
        )
        .await
        .expect("A's row")
        .get(0);
    assert_eq!(
        a_sealed, None,
        "B's later row must not seal A's — the pre-fix scope stamped A with B's valid_from"
    );

    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("cleanup claim");
}

/// A note that edits its own next-step from X to Y no longer asserts X, so A's X must be
/// sealed — but only within A's own rows. Another note's row for the same slot is never
/// touched, no matter how many times A rewrites its own.
#[tokio::test]
async fn a_note_replacing_its_own_next_step_seals_the_old_one() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;

    let subject = format!(
        "own-item-probe-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let predicate = "next-step".to_string();
    let path_a = unique_path("own-item-a");
    let path_b = unique_path("own-item-b");
    let t1 = UNIX_EPOCH
        + Duration::from_secs(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs(),
        );
    let t2 = t1 + Duration::from_mins(1);
    let t3 = t2 + Duration::from_mins(1);
    let emb = [0.0_f32; 1024];

    store
        .upsert_claim(
            &subject, &predicate, "x", &path_a, t1, &emb, "next", "certain",
        )
        .await
        .expect("ingest X from A");
    store
        .upsert_claim(
            &subject, &predicate, "w", &path_b, t2, &emb, "next", "certain",
        )
        .await
        .expect("ingest W from B");
    store
        .upsert_claim(
            &subject, &predicate, "y", &path_a, t3, &emb, "next", "certain",
        )
        .await
        .expect("A replaces its own next-step with Y");

    let x_sealed: SystemTime = db
        .query_one(
            "SELECT superseded_at FROM claim WHERE subject = $1 AND predicate = $2
             AND source_path = $3 AND value = 'x';",
            &[&subject, &predicate, &path_a],
        )
        .await
        .expect("A's old row")
        .get(0);
    assert_eq!(
        x_sealed, t3,
        "A's own later row seals X — the note no longer asserts it"
    );
    let current: i64 = db
        .query_one(
            "SELECT count(*) FROM claim WHERE subject = $1 AND predicate = $2
             AND superseded_at IS NULL;",
            &[&subject, &predicate],
        )
        .await
        .expect("count current rows")
        .get(0);
    assert_eq!(current, 2, "A's Y and B's W are both current");
    let b_sealed: Option<SystemTime> = db
        .query_one(
            "SELECT superseded_at FROM claim WHERE subject = $1 AND predicate = $2
             AND source_path = $3;",
            &[&subject, &predicate, &path_b],
        )
        .await
        .expect("B's row")
        .get(0);
    assert_eq!(
        b_sealed, None,
        "A rewriting its own next-step must never touch B's row"
    );

    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("cleanup claim");
}

/// Control: facts still collapse. Note A asserts `fact` X and note B asserts `fact` Y for the
/// same slot — exactly one current row, B's, as before. Without this the fix would be
/// indistinguishable from removing sealing altogether.
#[tokio::test]
async fn two_notes_facts_still_collapse_to_one() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;

    let subject = format!(
        "fact-slot-probe-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let predicate = "axis".to_string();
    let path_a = unique_path("fact-a");
    let path_b = unique_path("fact-b");
    let t_a = UNIX_EPOCH
        + Duration::from_secs(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs(),
        );
    let t_b = t_a + Duration::from_mins(1);
    let emb = [0.0_f32; 1024];

    store
        .upsert_claim(
            &subject, &predicate, "x", &path_a, t_a, &emb, "fact", "certain",
        )
        .await
        .expect("ingest X from A");
    store
        .upsert_claim(
            &subject, &predicate, "y", &path_b, t_b, &emb, "fact", "certain",
        )
        .await
        .expect("ingest Y from B");

    let current: i64 = db
        .query_one(
            "SELECT count(*) FROM claim WHERE subject = $1 AND predicate = $2
             AND superseded_at IS NULL;",
            &[&subject, &predicate],
        )
        .await
        .expect("count current rows")
        .get(0);
    assert_eq!(
        current, 1,
        "a fact is a slot: one current value per (subject, predicate)"
    );
    let value: String = db
        .query_one(
            "SELECT value FROM claim WHERE subject = $1 AND predicate = $2
             AND superseded_at IS NULL;",
            &[&subject, &predicate],
        )
        .await
        .expect("the current row")
        .get(0);
    let path: String = db
        .query_one(
            "SELECT source_path FROM claim WHERE subject = $1 AND predicate = $2
             AND superseded_at IS NULL;",
            &[&subject, &predicate],
        )
        .await
        .expect("the current row")
        .get(0);
    assert_eq!(
        (value.as_str(), path.as_str()),
        ("y", path_b.as_str()),
        "B's later row holds the slot, sealing A's across notes"
    );

    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("cleanup claim");
}

async fn claim_mirror_node_count(db: &Client, id: &str) -> i64 {
    db.query_one("SELECT count(*) FROM node WHERE id = $1;", &[&id])
        .await
        .expect("count node")
        .get(0)
}

async fn edges_touching(db: &Client, ids: &[String]) -> i64 {
    db.query_one(
        "SELECT count(*) FROM edge WHERE src = ANY($1) OR dst = ANY($1);",
        &[&ids.to_vec()],
    )
    .await
    .expect("count edges")
    .get(0)
}

async fn write_claim_mirror(store: &Store, key: &str, kind: &str) -> (String, String, String) {
    let path = unique_path(key);
    store
        .upsert_document(&dummy_frontmatter(&path), "sha-gc", SystemTime::now())
        .await
        .expect("upsert document");
    let claim = Claim {
        subject: format!("{key}-subject"),
        predicate: "axis".to_string(),
        value: "v".to_string(),
        kind: kind.to_string(),
        confidence: "certain".to_string(),
        said_by: None,
    };
    store
        .upsert_claim(
            &claim.subject,
            &claim.predicate,
            &claim.value,
            &path,
            SystemTime::now(),
            &[0.0_f32; 1024],
            &claim.kind,
            &claim.confidence,
        )
        .await
        .expect("upsert claim");
    store
        .upsert_claim_node(&path, "test", &claim.subject, &claim.predicate, &claim)
        .await
        .expect("upsert claim node");
    (path, claim.subject, claim.predicate)
}

fn gc_key(prefix: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}-{ts}")
}

#[tokio::test]
async fn gc_removes_a_claim_node_whose_row_is_gone() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let db = connect(&dsn).await;
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let (path, subject, predicate) =
        write_claim_mirror(&store, &gc_key("gc-gone"), "decision").await;
    let (anchor_path, anchor_subject, _) =
        write_claim_mirror(&store, &gc_key("gc-anchor"), "decision").await;
    let claim_id = format!("claim:{subject}:{predicate}");
    let typed_id = format!("decision:{subject}:{predicate}");

    assert_eq!(claim_mirror_node_count(&db, &claim_id).await, 1);
    assert_eq!(claim_mirror_node_count(&db, &typed_id).await, 1);
    assert!(
        edges_touching(&db, &[claim_id.clone(), typed_id.clone()]).await >= 2,
        "claims and is_a edges keep the mirror reachable, not alive"
    );

    db.execute(
        "DELETE FROM claim WHERE subject = $1 AND predicate = $2;",
        &[&subject, &predicate],
    )
    .await
    .expect("delete claim row");

    let gc = store.gc_orphans().await.expect("gc orphans");
    assert!(
        gc.claim_nodes >= 2,
        "the sweep counts the claim and typed mirrors it removes"
    );
    assert_eq!(claim_mirror_node_count(&db, &claim_id).await, 0);
    assert_eq!(claim_mirror_node_count(&db, &typed_id).await, 0);
    assert_eq!(
        edges_touching(&db, &[claim_id.clone(), typed_id.clone()]).await,
        0,
        "edges leave with the nodes they join"
    );
    assert_eq!(
        claim_mirror_node_count(&db, &format!("claim:{anchor_subject}:axis")).await,
        1,
        "a live claim of the same kind anchors the vocabulary and survives"
    );

    db.execute("DELETE FROM claim WHERE subject = $1;", &[&anchor_subject])
        .await
        .expect("cleanup anchor claim");
    store.gc_orphans().await.expect("sweep the anchor mirror");
    store
        .delete_document(&anchor_path)
        .await
        .expect("cleanup anchor document");
    store
        .delete_document(&path)
        .await
        .expect("cleanup document");
}

#[tokio::test]
async fn gc_keeps_a_claim_node_that_still_has_a_row() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let db = connect(&dsn).await;
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let (path, subject, predicate) =
        write_claim_mirror(&store, &gc_key("gc-live"), "decision").await;
    let claim_id = format!("claim:{subject}:{predicate}");
    let typed_id = format!("decision:{subject}:{predicate}");

    store.gc_orphans().await.expect("gc orphans");

    assert_eq!(claim_mirror_node_count(&db, &claim_id).await, 1);
    assert_eq!(claim_mirror_node_count(&db, &typed_id).await, 1);
    assert_eq!(
        edges_touching(&db, &[claim_id.clone(), typed_id.clone()]).await,
        3,
        "claims, is_a and claim_of_project edges all survive"
    );

    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("cleanup claim");
    store.gc_orphans().await.expect("sweep the orphaned mirror");
    store
        .delete_document(&path)
        .await
        .expect("cleanup document");
}

#[tokio::test]
async fn gc_keeps_a_claim_node_whose_only_row_is_sealed() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let db = connect(&dsn).await;
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let (path, subject, predicate) = write_claim_mirror(&store, &gc_key("gc-sealed"), "risk").await;
    let claim_id = format!("claim:{subject}:{predicate}");
    let typed_id = format!("risk:{subject}:{predicate}");

    db.execute(
        "UPDATE claim SET superseded_at = now() WHERE subject = $1;",
        &[&subject],
    )
    .await
    .expect("seal the only version");

    let sealed: i64 = db
        .query_one(
            "SELECT count(*) FROM claim WHERE subject = $1 AND predicate = $2
             AND superseded_at IS NOT NULL;",
            &[&subject, &predicate],
        )
        .await
        .expect("count sealed")
        .get(0);
    assert_eq!(sealed, 1, "the only version is sealed");
    let current: i64 = db
        .query_one(
            "SELECT count(*) FROM claim WHERE subject = $1 AND predicate = $2
             AND superseded_at IS NULL;",
            &[&subject, &predicate],
        )
        .await
        .expect("count current")
        .get(0);
    assert_eq!(current, 0, "no current row remains, history does");

    store.gc_orphans().await.expect("gc orphans");

    assert_eq!(
        claim_mirror_node_count(&db, &claim_id).await,
        1,
        "a sealed row is still a row"
    );
    assert_eq!(claim_mirror_node_count(&db, &typed_id).await, 1);
    assert_eq!(
        edges_touching(&db, &[claim_id.clone(), typed_id.clone()]).await,
        3
    );

    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("cleanup claim");
    store.gc_orphans().await.expect("sweep the orphaned mirror");
    store
        .delete_document(&path)
        .await
        .expect("cleanup document");
}

#[tokio::test]
async fn claim_inherits_note_anchor_and_era_is_marked() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let path = unique_path("claim-anchor");
    let subject = format!("anchor-probe-{}", std::process::id());
    let project = "test";

    let mut emb = [0.0_f32; 1024];
    emb[0] = 1.0;

    let front = dummy_frontmatter(&path);
    store
        .upsert_document(&front, "sha-anchor", SystemTime::now())
        .await
        .expect("upsert document");

    let before = store.claim_era_counts().await.expect("era counts before");

    let body = "the regression lives in a/b.rs:10 since the refactor";
    let note_anchors = drudge::anchor::from_note_body(project, body);
    let anchor = drudge::anchor::anchor_for_claim(&note_anchors, &subject, "regression in b.rs")
        .map(|a| a.to_db_string());
    store
        .upsert_claim_with_anchor(
            &subject,
            "location",
            "a/b.rs",
            &path,
            SystemTime::now(),
            &emb,
            "fact",
            "certain",
            anchor.as_deref(),
            None,
            None,
        )
        .await
        .expect("upsert anchored claim");

    let row = db
        .query_one(
            "SELECT anchor, era FROM claim WHERE subject = $1 AND predicate = 'location';",
            &[&subject],
        )
        .await
        .expect("read claim row");
    let stored_anchor: Option<String> = row.get(0);
    let era: String = row.get(1);
    assert_eq!(stored_anchor.as_deref(), Some("test:a/b.rs:L10"));
    assert_eq!(era, "anchored");

    let bare = format!("{subject}-bare");
    store
        .upsert_claim(
            &bare,
            "location",
            "nowhere",
            &path,
            SystemTime::now(),
            &emb,
            "fact",
            "certain",
        )
        .await
        .expect("upsert unanchored claim");
    let row = db
        .query_one(
            "SELECT anchor, era FROM claim WHERE subject = $1;",
            &[&bare],
        )
        .await
        .expect("read bare row");
    assert_eq!(row.get::<_, Option<String>>(0), None);
    assert_eq!(row.get::<_, String>(1), "unanchored");

    let legacy = format!("{subject}-legacy");
    db.execute(
        "INSERT INTO claim (subject, predicate, value, source_path, valid_from, kind, confidence)
         VALUES ($1, 'location', 'old world', $2, now(), 'fact', 'certain');",
        &[&legacy, &path],
    )
    .await
    .expect("insert legacy claim");
    let era: String = db
        .query_one("SELECT era FROM claim WHERE subject = $1;", &[&legacy])
        .await
        .expect("read legacy row")
        .get(0);
    assert_eq!(era, "pre-anchor");

    let after = store.claim_era_counts().await.expect("era counts after");
    assert_eq!(after.anchored, before.anchored + 1);
    assert_eq!(after.unanchored, before.unanchored + 1);
    assert_eq!(after.pre_anchor, before.pre_anchor + 1);

    db.execute(
        "DELETE FROM claim WHERE subject LIKE $1;",
        &[&format!("{subject}%")],
    )
    .await
    .expect("cleanup claims");
    store
        .delete_document(&path)
        .await
        .expect("cleanup document");
}

#[tokio::test]
async fn current_claims_filters_by_anchor_path() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let path = unique_path("claim-anchor-filter");
    let subject = format!("anchor-filter-{}", std::process::id());
    let project = "test";

    let mut emb = [0.0_f32; 1024];
    emb[0] = 1.0;

    let front = dummy_frontmatter(&path);
    store
        .upsert_document(&front, "sha-anchor-filter", SystemTime::now())
        .await
        .expect("upsert document");
    store
        .upsert_claim_with_anchor(
            &subject,
            "location",
            "a/b.rs",
            &path,
            SystemTime::now(),
            &emb,
            "fact",
            "certain",
            Some("test:a/b.rs:L10"),
            None,
            None,
        )
        .await
        .expect("upsert anchored claim");

    let hits = store
        .current_claims(&emb, 10, &[], Some(project), None, Some("a/b.rs"), false)
        .await
        .expect("claims filtered by anchor_path");
    assert!(
        hits.iter().any(|c| c.subject == subject),
        "the anchored claim must match anchor_path a/b.rs"
    );
    assert_eq!(
        hits.iter().find(|c| c.subject == subject).unwrap().era,
        "anchored"
    );
    let misses = store
        .current_claims(
            &emb,
            10,
            &[],
            Some(project),
            None,
            Some("no/such.rs"),
            false,
        )
        .await
        .expect("claims filtered by missing anchor_path");
    assert!(
        !misses.iter().any(|c| c.subject == subject),
        "a non-matching anchor_path must drop the claim"
    );
    let suffix = store
        .current_claims(&emb, 10, &[], Some(project), None, Some("b.rs"), false)
        .await
        .expect("claims filtered by path suffix");
    assert!(
        !suffix.iter().any(|c| c.subject == subject),
        "a path suffix is not an anchor prefix and must not match"
    );
    let prefix = store
        .current_claims(&emb, 10, &[], Some(project), None, Some("a/"), false)
        .await
        .expect("claims filtered by path prefix");
    assert!(
        prefix.iter().any(|c| c.subject == subject),
        "a path prefix must match"
    );
    let unfiltered = store
        .current_claims(&emb, 10, &[], Some(project), None, None, false)
        .await
        .expect("claims unfiltered");
    assert!(
        unfiltered.iter().any(|c| c.subject == subject),
        "the filter absent must keep default behaviour"
    );

    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("cleanup claim");
    store
        .delete_document(&path)
        .await
        .expect("cleanup document");
}

use std::fs;

use drudge::config::{CodeIndexSource, CodeLanguage};
use tempfile::tempdir;

fn unique_source_id(prefix: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}-{ts}")
}

struct HashedFixture {
    root: tempfile::TempDir,
    source: CodeIndexSource,
    anchor: drudge::anchor::Anchor,
    hashed: drudge::anchor_hash::AnchorHash,
    note_path: String,
    subject: String,
    emb: [f32; 1024],
}

async fn seed_hashed_claim(store: &Store) -> HashedFixture {
    let root = tempdir().expect("temp source root");
    fs::write(
        root.path().join("x.rs"),
        "// fixture header\n\nfn known() {\n    let value = 1;\n}\n",
    )
    .expect("write fixture");
    let source_id = unique_source_id("anchor-hash");
    let source = CodeIndexSource::new(
        &source_id,
        "Anchor Hash Fixture",
        root.path().to_path_buf(),
        CodeLanguage::Rust,
        true,
    )
    .expect("valid source");
    let anchor = drudge::anchor::Anchor {
        project: source_id,
        path: root.path().join("x.rs").to_string_lossy().into_owned(),
        span: Some(drudge::anchor::LineSpan { start: 3, end: 5 }),
    };
    let hashed = drudge::anchor_hash::hash_for_anchor(&anchor, std::slice::from_ref(&source))
        .expect("a registered repo resolves the covering symbol");
    assert_eq!(hashed.symbol, "known");

    let note_path = unique_path("claim-anchor-hash");
    let subject = format!("anchor-hash-{}", unique_source_id("subj"));
    let mut front = dummy_frontmatter(&note_path);
    front.project = anchor.project.clone();
    let emb = unique_direction();
    store
        .upsert_document(&front, "sha-anchor-hash", SystemTime::now())
        .await
        .expect("upsert document");
    store
        .upsert_claim_with_anchor(
            &subject,
            "location",
            "x.rs",
            &note_path,
            SystemTime::now(),
            &emb,
            "fact",
            "certain",
            Some(&anchor.to_db_string()),
            Some(&hashed.hash),
            Some(&hashed.symbol),
        )
        .await
        .expect("upsert hashed claim");
    HashedFixture {
        root,
        source,
        anchor,
        hashed,
        note_path,
        subject,
        emb,
    }
}

async fn cleanup_hashed_claim(store: &Store, db: &Client, fixture: &HashedFixture) {
    db.execute("DELETE FROM claim WHERE subject = $1;", &[&fixture.subject])
        .await
        .expect("cleanup claim");
    store
        .delete_document(&fixture.note_path)
        .await
        .expect("cleanup document");
}

#[tokio::test]
async fn anchor_hash_is_set_at_ingest_only_for_registered_repos() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let fixture = seed_hashed_claim(&store).await;
    let sources = [fixture.source.clone()];

    let stored_hash: Option<String> = db
        .query_one(
            "SELECT anchor_hash FROM claim WHERE subject = $1;",
            &[&fixture.subject],
        )
        .await
        .expect("read hash")
        .get(0);
    assert_eq!(
        stored_hash.as_deref(),
        Some(fixture.hashed.hash.as_str()),
        "anchor_hash is stored at ingest"
    );

    let foreign = drudge::anchor::Anchor {
        project: "not-registered".to_owned(),
        ..fixture.anchor.clone()
    };
    assert!(
        drudge::anchor_hash::hash_for_anchor(&foreign, &sources).is_none(),
        "an unregistered project must not get the hash layer"
    );
    let uncovered = drudge::anchor::Anchor {
        span: Some(drudge::anchor::LineSpan { start: 1, end: 2 }),
        ..fixture.anchor.clone()
    };
    assert!(
        drudge::anchor_hash::hash_for_anchor(&uncovered, &sources).is_none(),
        "a span no symbol covers must not get a hash"
    );

    cleanup_hashed_claim(&store, &db, &fixture).await;
}

#[tokio::test]
async fn anchor_hash_marks_edited_bodies_stale_and_current_claims_hides_them() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let fixture = seed_hashed_claim(&store).await;
    let sources = [fixture.source.clone()];

    let quiet = drudge::anchor_hash::check_stale_anchors(&store, &sources)
        .await
        .expect("quiet sweep");
    assert_eq!(quiet.repinned, 0, "an untouched body must not re-pin");
    let untouched: Option<SystemTime> = db
        .query_one(
            "SELECT stale_at FROM claim WHERE subject = $1;",
            &[&fixture.subject],
        )
        .await
        .expect("read stale_at")
        .get(0);
    assert!(untouched.is_none(), "an untouched body must not go stale");

    fs::write(
        fixture.root.path().join("x.rs"),
        "// fixture header\n\nfn known() {\n    let value = 2;\n}\n",
    )
    .expect("edit body");
    let sweep = drudge::anchor_hash::check_stale_anchors(&store, &sources)
        .await
        .expect("sweep after edit");
    assert!(sweep.stale >= 1, "the edited body must be counted stale");
    let row = db
        .query_one(
            "SELECT stale_at, stale_reason FROM claim WHERE subject = $1;",
            &[&fixture.subject],
        )
        .await
        .expect("read stale row");
    let stale_at: Option<SystemTime> = row.get(0);
    let reason: Option<String> = row.get(1);
    assert!(stale_at.is_some(), "stale_at is stamped");
    assert_eq!(reason.as_deref(), Some("symbol body changed"));

    let query = fixture.emb;
    let hidden = store
        .current_claims(&query, 50, &[], None, None, None, false)
        .await
        .expect("claims default");
    assert!(
        !hidden.iter().any(|c| c.subject == fixture.subject),
        "a stale claim must be hidden by default"
    );
    let shown = store
        .current_claims(&query, 50, &[], None, None, None, true)
        .await
        .expect("claims include_stale");
    let stale_row = shown
        .iter()
        .find(|c| c.subject == fixture.subject)
        .expect("include_stale must surface the stale claim");
    assert!(stale_row.stale_at.is_some());
    assert_eq!(
        stale_row.stale_reason.as_deref(),
        Some("symbol body changed")
    );

    cleanup_hashed_claim(&store, &db, &fixture).await;
}

#[tokio::test]
async fn anchor_hash_repins_a_moved_symbol_instead_of_stale() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let fixture = seed_hashed_claim(&store).await;
    let sources = [fixture.source.clone()];

    fs::write(
        fixture.root.path().join("x.rs"),
        "// fixture header\n\nfn known() {\n    let value = 2;\n}\n",
    )
    .expect("edit body");
    drudge::anchor_hash::check_stale_anchors(&store, &sources)
        .await
        .expect("sweep after edit");

    fs::write(
        fixture.root.path().join("x.rs"),
        "// fixture header\n\n\n\nfn known() {\n    let value = 1;\n}\n",
    )
    .expect("move the function down");
    db.execute(
        "UPDATE claim SET stale_at = NULL, stale_reason = NULL WHERE subject = $1;",
        &[&fixture.subject],
    )
    .await
    .expect("clear stale marker");
    let sweep = drudge::anchor_hash::check_stale_anchors(&store, &sources)
        .await
        .expect("sweep after move");
    assert!(sweep.repinned >= 1, "the moved body must be re-pinned");

    let row = db
        .query_one(
            "SELECT anchor, stale_at, anchor_hash FROM claim WHERE subject = $1;",
            &[&fixture.subject],
        )
        .await
        .expect("read re-pinned row");
    let repinned_anchor: String = row.get(0);
    let stale_at: Option<SystemTime> = row.get(1);
    let hash_after: String = row.get(2);
    assert_eq!(
        repinned_anchor,
        format!("{}:{}:L5-L7", fixture.anchor.project, fixture.anchor.path),
        "the anchor re-pins to the shifted span"
    );
    assert!(stale_at.is_none(), "a move is not counted stale");
    assert_eq!(
        hash_after, fixture.hashed.hash,
        "the re-pinned anchor keeps its hash — same body, moved"
    );

    cleanup_hashed_claim(&store, &db, &fixture).await;
}

async fn stale_subjects_now(db: &Client, subjects: &[String]) -> Vec<String> {
    let rows = db
        .query(
            "SELECT subject FROM claim WHERE subject = ANY($1) AND stale_at IS NOT NULL;",
            &[&subjects],
        )
        .await
        .expect("read stale subjects");
    rows.iter()
        .map(|row| row.get::<_, String>(0))
        .collect::<Vec<_>>()
}

#[tokio::test]
async fn anchor_sweep_is_bounded_and_continues_by_valid_from() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;

    let root = tempdir().expect("temp source root");
    let source_id = unique_source_id("anchor-sweep");
    let source = CodeIndexSource::new(
        &source_id,
        "Anchor Sweep Fixture",
        root.path().to_path_buf(),
        CodeLanguage::Rust,
        true,
    )
    .expect("valid source");
    let sources = [source];
    let base = SystemTime::now()
        .checked_sub(Duration::from_hours(1))
        .expect("base time");
    let subjects: Vec<String> = (0..3)
        .map(|i| format!("anchor-sweep-{}-{i}", unique_source_id("subj")))
        .collect();
    let note_paths: Vec<String> = (0..3)
        .map(|i| unique_path(&format!("claim-anchor-sweep-{i}")))
        .collect();
    for (i, subject) in subjects.iter().enumerate() {
        let file = root.path().join(format!("x{i}.rs"));
        fs::write(&file, "fn known() {}\n").expect("write fixture");
        let anchor = drudge::anchor::Anchor {
            project: source_id.clone(),
            path: file.to_string_lossy().into_owned(),
            span: None,
        };
        let hashed = drudge::anchor_hash::hash_for_anchor(&anchor, &sources)
            .expect("span-less anchor hashes the file bytes");
        let mut front = dummy_frontmatter(&note_paths[i]);
        front.project = source_id.clone();
        let mut emb = [0.0_f32; 1024];
        emb[0] = 1.0;
        store
            .upsert_document(&front, &format!("sha-anchor-sweep-{i}"), SystemTime::now())
            .await
            .expect("upsert document");
        store
            .upsert_claim_with_anchor(
                subject,
                "location",
                "x.rs",
                &note_paths[i],
                base + Duration::from_secs(i as u64),
                &emb,
                "fact",
                "certain",
                Some(&anchor.to_db_string()),
                Some(&hashed.hash),
                Some(&hashed.symbol),
            )
            .await
            .expect("upsert hashed claim");
    }

    fs::write(root.path().join("x0.rs"), "fn known() { let a = 1; }\n").expect("edit file 0");
    fs::write(root.path().join("x2.rs"), "fn known() { let a = 1; }\n").expect("edit file 2");

    let first = drudge::anchor_hash::check_stale_anchors_limited(&store, &sources, 2)
        .await
        .expect("first sweep");
    assert_eq!(first.checked, 2, "the first sweep stops at its batch");
    assert_eq!(
        stale_subjects_now(&db, &subjects).await,
        vec![subjects[0].clone()],
        "the two oldest were checked; the third has not had its turn"
    );

    let second = drudge::anchor_hash::check_stale_anchors_limited(&store, &sources, 2)
        .await
        .expect("second sweep");
    assert_eq!(second.checked, 2);
    let mut stale = stale_subjects_now(&db, &subjects).await;
    stale.sort();
    let mut expected = vec![subjects[0].clone(), subjects[2].clone()];
    expected.sort();
    assert_eq!(stale, expected, "the second sweep reaches the tail");

    for (i, subject) in subjects.iter().enumerate() {
        db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
            .await
            .expect("cleanup claim");
        store
            .delete_document(&note_paths[i])
            .await
            .expect("cleanup document");
    }
}

async fn repinned_subjects_now(db: &Client, subjects: &[String]) -> Vec<String> {
    let rows = db
        .query(
            "SELECT subject FROM claim
              WHERE subject = ANY($1) AND anchor LIKE '%:L5-L7';",
            &[&subjects],
        )
        .await
        .expect("read repinned subjects");
    let mut out: Vec<String> = rows.iter().map(|row| row.get::<_, String>(0)).collect();
    out.sort();
    out
}

async fn seed_shared_valid_from_group(
    store: &Store,
    n: usize,
) -> (
    tempfile::TempDir,
    Vec<CodeIndexSource>,
    Vec<String>,
    Vec<String>,
) {
    let root = tempdir().expect("temp source root");
    let source_id = unique_source_id("anchor-sweep-group");
    let source = CodeIndexSource::new(
        &source_id,
        "Anchor Sweep Group Fixture",
        root.path().to_path_buf(),
        CodeLanguage::Rust,
        true,
    )
    .expect("valid source");
    let sources = vec![source];
    let shared_from = SystemTime::now()
        .checked_sub(Duration::from_hours(1))
        .expect("shared valid_from");
    let subjects: Vec<String> = (0..n)
        .map(|i| format!("anchor-sweep-group-{}-{i:02}", unique_source_id("subj")))
        .collect();
    let note_paths: Vec<String> = (0..n)
        .map(|i| unique_path(&format!("claim-anchor-sweep-group-{i}")))
        .collect();
    for (i, subject) in subjects.iter().enumerate() {
        let file = root.path().join(format!("x{i}.rs"));
        fs::write(
            &file,
            "// fixture header\n\nfn known() {\n    let value = 1;\n}\n",
        )
        .expect("write fixture");
        let anchor = drudge::anchor::Anchor {
            project: source_id.clone(),
            path: file.to_string_lossy().into_owned(),
            span: Some(drudge::anchor::LineSpan { start: 3, end: 5 }),
        };
        let hashed = drudge::anchor_hash::hash_for_anchor(&anchor, &sources)
            .expect("a registered repo resolves the covering symbol");
        let mut front = dummy_frontmatter(&note_paths[i]);
        front.project = source_id.clone();
        let mut emb = [0.0_f32; 1024];
        emb[0] = 1.0;
        store
            .upsert_document(
                &front,
                &format!("sha-anchor-sweep-group-{i}"),
                SystemTime::now(),
            )
            .await
            .expect("upsert document");
        store
            .upsert_claim_with_anchor(
                subject,
                "location",
                "x.rs",
                &note_paths[i],
                shared_from,
                &emb,
                "fact",
                "certain",
                Some(&anchor.to_db_string()),
                Some(&hashed.hash),
                Some(&hashed.symbol),
            )
            .await
            .expect("upsert hashed claim");
    }
    (root, sources, subjects, note_paths)
}

#[tokio::test]
async fn anchor_sweep_resumes_inside_a_shared_valid_from_group() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let (root, sources, subjects, note_paths) = seed_shared_valid_from_group(&store, 4).await;

    for i in 0..4 {
        fs::write(
            root.path().join(format!("x{i}.rs")),
            "// fixture header\n\n\n\nfn known() {\n    let value = 1;\n}\n",
        )
        .expect("move the function down");
    }

    let first = drudge::anchor_hash::check_stale_anchors_limited(&store, &sources, 2)
        .await
        .expect("first sweep");
    assert_eq!(first.checked, 2, "the first sweep stops at its batch");
    assert_eq!(first.repinned, 2, "both checked claims re-pinned");
    assert_eq!(first.stale, 0, "a move is never counted stale");
    let mut first_two = subjects[0..2].to_vec();
    first_two.sort();
    assert_eq!(
        repinned_subjects_now(&db, &subjects).await,
        first_two,
        "the first sweep took exactly the first two of the shared group"
    );

    let second = drudge::anchor_hash::check_stale_anchors_limited(&store, &sources, 2)
        .await
        .expect("second sweep");
    assert_eq!(second.checked, 2);
    assert_eq!(
        second.repinned, 2,
        "the second sweep took the remaining two"
    );
    let mut all_four = subjects.clone();
    all_four.sort();
    assert_eq!(
        repinned_subjects_now(&db, &subjects).await,
        all_four,
        "every claim was checked exactly once across the two sweeps — none skipped"
    );

    for (i, subject) in subjects.iter().enumerate() {
        db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
            .await
            .expect("cleanup claim");
        store
            .delete_document(&note_paths[i])
            .await
            .expect("cleanup document");
    }
}

// ── claim graph edges: an unchanged claim still belongs to the graph ─────────

use std::sync::atomic::{AtomicUsize, Ordering};

use drudge::audit;
use drudge::config::BoringConfig;
use drudge::ingest::{
    DefaultChunker, Embed, FileOutcome, FrontmatterGraphExtractor, GraphExtractor, Stats,
    ingest_file_with,
};

struct CountingEmbed {
    calls: AtomicUsize,
    panic_on_call: bool,
}

impl CountingEmbed {
    fn lenient() -> Self {
        Self {
            panic_on_call: false,
            ..Self::default()
        }
    }
    fn strict() -> Self {
        Self {
            panic_on_call: true,
            ..Self::default()
        }
    }
    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

impl Default for CountingEmbed {
    fn default() -> Self {
        Self {
            calls: AtomicUsize::new(0),
            panic_on_call: false,
        }
    }
}

impl Embed for CountingEmbed {
    fn embed(
        &self,
        text: &str,
    ) -> impl std::future::Future<Output = anyhow::Result<Vec<f32>>> + Send {
        let _ = text;
        assert!(
            !self.panic_on_call,
            "embed called on a path that must stay un-embedded"
        );
        self.calls.fetch_add(1, Ordering::SeqCst);
        std::future::ready(Ok(vec![0.0_f32; 1024]))
    }
}

fn claim_note(path: &str, subject: &str) -> FrontMatter {
    FrontMatter {
        origin: "personal".to_string(),
        project: String::new(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("claim edge test".to_string()),
        tags: Vec::new(),
        claims: vec![Claim {
            subject: subject.to_string(),
            predicate: "axis".to_string(),
            value: "v1".to_string(),
            kind: "fact".to_string(),
            confidence: "certain".to_string(),
            said_by: None,
        }],
        ..Default::default()
    }
}

async fn extract_note_graph(store: &Store, llm: &impl Embed, front: &FrontMatter) -> Stats {
    let cfg = BoringConfig::default();
    let mut stats = Stats::default();
    FrontmatterGraphExtractor::new()
        .extract(store, llm, &cfg, front, "body", &mut stats)
        .await
        .expect("graph extract");
    stats
}

async fn claims_edge_count(db: &Client, path: &str, subject: &str) -> i64 {
    db.query_one(
        "SELECT count(*) FROM edge WHERE src = $1 AND dst = $2 AND kind = 'claims';",
        &[&format!("doc:{path}"), &format!("claim:{subject}:axis")],
    )
    .await
    .expect("count claims edge")
    .get(0)
}

async fn claim_row_count(db: &Client, path: &str, subject: &str) -> i64 {
    db.query_one(
        "SELECT count(*) FROM claim WHERE subject = $1 AND predicate = 'axis' AND source_path = $2;",
        &[&subject, &path],
    )
    .await
    .expect("count claim rows")
    .get(0)
}

async fn claim_valid_from(db: &Client, path: &str, subject: &str) -> SystemTime {
    db.query_one(
        "SELECT valid_from FROM claim WHERE subject = $1 AND predicate = 'axis' AND source_path = $2
         ORDER BY valid_from DESC LIMIT 1;",
        &[&subject, &path],
    )
    .await
    .expect("read valid_from")
    .get(0)
}

async fn delete_claims_edge(db: &Client, subject: &str) {
    db.execute(
        "DELETE FROM edge WHERE kind = 'claims' AND dst = $1;",
        &[&format!("claim:{subject}:axis")],
    )
    .await
    .expect("delete claims edge");
}

async fn cleanup_claim_edge_fixture(store: &Store, db: &Client, path: &str, subject: &str) {
    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("cleanup claim");
    db.execute(
        "DELETE FROM edge WHERE dst = $1;",
        &[&format!("claim:{subject}:axis")],
    )
    .await
    .expect("cleanup claim edges");
    db.execute(
        "DELETE FROM node WHERE id = $1;",
        &[&format!("claim:{subject}:axis")],
    )
    .await
    .expect("cleanup claim node");
    store.delete_document(path).await.expect("cleanup document");
}

fn unique_subject(tag: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("claim-edge-{tag}-{ts}")
}

/// Re-asserting an unchanged claim keeps its row (`valid_from` stays — the row guard still
/// works) and now also writes the `doc —claims→ claim` edge. The fixture is a note ingested
/// before the claim-graph code existed: the row is present, the edge is not.
#[tokio::test]
async fn unchanged_claim_keeps_row_and_gets_graph_edge() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let subject = unique_subject("row");
    let path = format!("/vault/wiki/{subject}.md");
    let front = claim_note(&path, &subject);
    store
        .upsert_document(&front, &format!("sha-{subject}"), SystemTime::now())
        .await
        .expect("upsert document");
    store
        .upsert_claim(
            &subject,
            "axis",
            "v1",
            &path,
            SystemTime::now(),
            &[0.0_f32; 1024],
            "fact",
            "certain",
        )
        .await
        .expect("seed claim row");
    let valid_from = claim_valid_from(&db, &path, &subject).await;

    let embedder = CountingEmbed::lenient();
    let stats = extract_note_graph(&store, &embedder, &front).await;

    assert_eq!(embedder.calls(), 0, "an unchanged claim must not embed");
    assert_eq!(stats.claims_unchanged, 1, "the row skip is counted");
    assert!(
        stats.edges > 0,
        "the graph write is counted on the unchanged path"
    );
    assert_eq!(
        claim_row_count(&db, &path, &subject).await,
        1,
        "no second row"
    );
    extract_note_graph(&store, &embedder, &front).await;
    assert_eq!(
        claim_valid_from(&db, &path, &subject).await,
        valid_from,
        "valid_from must not move"
    );
    assert_eq!(
        claims_edge_count(&db, &path, &subject).await,
        1,
        "the claims edge exists after the unchanged pass"
    );
    cleanup_claim_edge_fixture(&store, &db, &path, &subject).await;
}

/// The 4,321 case: the row exists and the `claims` edge was lost (deleted directly here, as the
/// old unchanged-claim path dropped it on every re-ingest). The next ingest of the identical
/// note restores the edge and leaves the row — `valid_from` included — untouched.
#[tokio::test]
async fn deleted_claims_edge_is_restored_on_next_ingest() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let subject = unique_subject("restore");
    let path = format!("/vault/wiki/{subject}.md");
    let front = claim_note(&path, &subject);
    store
        .upsert_document(&front, &format!("sha-{subject}"), SystemTime::now())
        .await
        .expect("upsert document");

    let embedder = CountingEmbed::lenient();
    extract_note_graph(&store, &embedder, &front).await;
    assert_eq!(claims_edge_count(&db, &path, &subject).await, 1);
    let valid_from = claim_valid_from(&db, &path, &subject).await;

    delete_claims_edge(&db, &subject).await;
    assert_eq!(claims_edge_count(&db, &path, &subject).await, 0);

    let stats = extract_note_graph(&store, &embedder, &front).await;

    assert_eq!(
        claims_edge_count(&db, &path, &subject).await,
        1,
        "the edge comes back on the next ingest"
    );
    assert_eq!(
        claim_row_count(&db, &path, &subject).await,
        1,
        "the row is not rewritten"
    );
    assert_eq!(
        claim_valid_from(&db, &path, &subject).await,
        valid_from,
        "the row's valid_from is untouched"
    );
    assert_eq!(
        embedder.calls(),
        1,
        "proving the edge must not re-embed the claim"
    );
    assert_eq!(stats.claims_unchanged, 1);
    cleanup_claim_edge_fixture(&store, &db, &path, &subject).await;
}

/// Structural control for the row guard: the embed call sits on the changed branch only, so a
/// stub that panics on any call makes "the unchanged path does not embed" a hard failure.
#[tokio::test]
async fn unchanged_claim_path_never_embeds() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let subject = unique_subject("noembed");
    let path = format!("/vault/wiki/{subject}.md");
    let front = claim_note(&path, &subject);
    store
        .upsert_document(&front, &format!("sha-{subject}"), SystemTime::now())
        .await
        .expect("upsert document");
    store
        .upsert_claim(
            &subject,
            "axis",
            "v1",
            &path,
            SystemTime::now(),
            &[0.0_f32; 1024],
            "fact",
            "certain",
        )
        .await
        .expect("seed claim row");

    let embedder = CountingEmbed::strict();
    extract_note_graph(&store, &embedder, &front).await;

    assert_eq!(embedder.calls(), 0);
    assert_eq!(claims_edge_count(&db, &path, &subject).await, 1);
    cleanup_claim_edge_fixture(&store, &db, &path, &subject).await;
}

/// `corpus_status` reports claim-edge coverage as two integers — claim nodes total and claim
/// nodes with a `claims` edge — and the second moves only when an edge is added, not when a
/// bare node appears.
#[tokio::test]
async fn corpus_status_reports_claim_edge_coverage() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let subject = unique_subject("coverage");
    let path = format!("/vault/wiki/{subject}.md");
    let front = claim_note(&path, &subject);

    let before = audit::stats(&store, false).await.expect("audit stats");

    store
        .upsert_claim_node(
            &path,
            "",
            &front.claims[0].subject,
            &front.claims[0].predicate,
            &front.claims[0],
        )
        .await
        .expect("upsert claim node");
    delete_claims_edge(&db, &subject).await;

    let mid = audit::stats(&store, false).await.expect("audit stats");
    assert_eq!(
        mid.graph_claims,
        before.graph_claims + 1,
        "a claim node moves the total"
    );
    assert_eq!(
        mid.graph_claims_with_doc, before.graph_claims_with_doc,
        "an edgeless claim node is not coverage"
    );

    store
        .upsert_claim_node(
            &path,
            "",
            &front.claims[0].subject,
            &front.claims[0].predicate,
            &front.claims[0],
        )
        .await
        .expect("restore claims edge");
    let after = audit::stats(&store, false).await.expect("audit stats");
    assert_eq!(
        after.graph_claims, mid.graph_claims,
        "adding an edge does not move the total"
    );
    assert_eq!(
        after.graph_claims_with_doc,
        mid.graph_claims_with_doc + 1,
        "adding an edge moves coverage"
    );
    cleanup_claim_edge_fixture(&store, &db, &path, &subject).await;
}

/// The backfill path for the 4,321: re-ingesting a byte-identical note still takes the
/// `Unchanged` verdict (no chunk re-embedding — the panic-on-call stub survives), but the
/// deterministic graph now rebuilds, so a deleted `claims` edge is written back.
#[tokio::test]
async fn unchanged_note_reingest_repairs_claim_edge_without_embedding() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let subject = unique_subject("sync");
    let dir = tempdir().expect("tempdir");
    let note_path = dir.path().join(format!("{subject}.md"));
    fs::write(
        &note_path,
        format!(
            "---\norigin: personal\nproject: \"\"\nkind: note\nclaims:\n  - subject: {subject}\n    predicate: axis\n    value: v1\n    kind: fact\n    confidence: certain\n---\n\nbody\n"
        ),
    )
    .expect("write note");
    let note_path_str = note_path.to_string_lossy().into_owned();
    let cfg = BoringConfig::default();

    let mut stats = Stats::default();
    let embedder = CountingEmbed::lenient();
    let outcome = ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor, _>(
        &store,
        &embedder,
        &cfg,
        &note_path_str,
        &mut stats,
    )
    .await
    .expect("first ingest");
    assert_eq!(outcome, FileOutcome::New);
    assert_eq!(claims_edge_count(&db, &note_path_str, &subject).await, 1);
    let valid_from = claim_valid_from(&db, &note_path_str, &subject).await;

    delete_claims_edge(&db, &subject).await;

    let mut stats = Stats::default();
    let strict = CountingEmbed::strict();
    let outcome = ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor, _>(
        &store,
        &strict,
        &cfg,
        &note_path_str,
        &mut stats,
    )
    .await
    .expect("second ingest");
    assert_eq!(outcome, FileOutcome::Unchanged);
    assert!(stats.edges > 0, "the sync must report the edges it wrote");
    assert_eq!(
        claims_edge_count(&db, &note_path_str, &subject).await,
        1,
        "the byte-identical re-ingest repairs the claims edge"
    );
    assert_eq!(
        claim_valid_from(&db, &note_path_str, &subject).await,
        valid_from,
        "the claim row is untouched"
    );
    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("cleanup claim");
    db.execute(
        "DELETE FROM edge WHERE dst = $1;",
        &[&format!("claim:{subject}:axis")],
    )
    .await
    .expect("cleanup claim edges");
    db.execute(
        "DELETE FROM node WHERE id = $1;",
        &[&format!("claim:{subject}:axis")],
    )
    .await
    .expect("cleanup claim node");
    store
        .delete_document(&note_path_str)
        .await
        .expect("cleanup document");
}

/// The missing-node mechanism: the claim row is keyed by `canon(subject)` while the graph node
/// was assembled from the raw frontmatter spelling, so a claim whose subject is not already
/// canonical was written as a node under a spelling no row uses — and `gc_orphans` deleted that
/// node (with its edges) as a stale mirror at the end of every sync. The live trace: wiki-1718
/// declares `feature/FDS-16749-resetkeys-restore / status`, the row reads
/// `feature/fds-16749-resetkeys-restore`, and no node exists under either spelling after a
/// sync. The node must be written under exactly the key the row uses, and survive the sweep.
/// The already-canonical claim in the same note is the in-test control: it never lost its node.
#[tokio::test]
async fn claim_node_is_keyed_canonically_like_the_row() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let raw_subject = format!("Feature/FDS-16749-ResetKeys-Restore-{ts}");
    let canon_subject = format!("feature/fds-16749-resetkeys-restore-{ts}");
    let plain_subject = format!("plainkey-{ts}");
    let path = format!("/vault/wiki/claim-node-key-{ts}.md");
    let front = FrontMatter {
        origin: "personal".to_string(),
        project: String::new(),
        kind: "note".to_string(),
        source_path: path.clone(),
        title: Some("claim node key test".to_string()),
        tags: Vec::new(),
        claims: vec![
            Claim {
                subject: raw_subject.clone(),
                predicate: "axis".to_string(),
                value: "v1".to_string(),
                kind: "decision".to_string(),
                confidence: "certain".to_string(),
                said_by: None,
            },
            Claim {
                subject: plain_subject.clone(),
                predicate: "axis".to_string(),
                value: "v2".to_string(),
                confidence: "certain".to_string(),
                ..Default::default()
            },
        ],
        ..Default::default()
    };
    store
        .upsert_document(&front, &format!("sha-{ts}"), SystemTime::now())
        .await
        .expect("upsert document");

    let embedder = CountingEmbed::lenient();
    extract_note_graph(&store, &embedder, &front).await;

    assert_eq!(
        claim_row_count(&db, &path, &canon_subject).await,
        1,
        "the row is keyed by the canonical subject"
    );
    let canonical_node = format!("claim:{canon_subject}:axis");
    assert_eq!(
        claim_mirror_node_count(&db, &canonical_node).await,
        1,
        "the node exists under the same key the row uses"
    );
    assert_eq!(
        claim_mirror_node_count(&db, &format!("claim:{raw_subject}:axis")).await,
        0,
        "no node lingers under the raw frontmatter spelling"
    );
    assert_eq!(
        claims_edge_count(&db, &path, &canon_subject).await,
        1,
        "the doc claims edge points at the canonical node"
    );
    assert_eq!(
        claim_mirror_node_count(&db, &format!("decision:{canon_subject}:axis")).await,
        1,
        "the typed decision node shares the canonical key"
    );
    assert_eq!(
        claim_mirror_node_count(&db, &format!("claim:{plain_subject}:axis")).await,
        1,
        "control: the already-canonical claim has its node"
    );
    assert_eq!(
        claims_edge_count(&db, &path, &plain_subject).await,
        1,
        "control: the already-canonical claim keeps its edge"
    );

    store.gc_orphans().await.expect("gc orphans");

    assert_eq!(
        claim_mirror_node_count(&db, &canonical_node).await,
        1,
        "the canonical node survives the sweep that removed the raw-spelled one"
    );
    assert_eq!(
        claims_edge_count(&db, &path, &canon_subject).await,
        1,
        "the claims edge survives gc too"
    );
    assert_eq!(
        claim_mirror_node_count(&db, &format!("decision:{canon_subject}:axis")).await,
        1,
        "the typed node survives gc"
    );

    cleanup_claim_edge_fixture(&store, &db, &path, &canon_subject).await;
    cleanup_claim_edge_fixture(&store, &db, &path, &plain_subject).await;
}

/// The population mechanism, pinned as deliberate behavior so a fix for the canonicalisation
/// loss cannot be read as covering it: when a note stops declaring a claim, the re-ingest
/// clears the doc's semantic edges and rewrites only what the note declares now — the dropped
/// claim loses its `claims` edge while its row and its mirror node persist (re-ingest never
/// seals or deletes rows, and the node still mirrors a live row, so gc leaves it). Those
/// edgeless rows are the other share of the corpus-wide gap; sealing them is a separate
/// decision this change does not make.
#[tokio::test]
async fn undeclared_claim_row_and_node_outlive_their_edge() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let subject_x = unique_subject("outlives-x");
    let subject_y = unique_subject("outlives-y");
    let dir = tempdir().expect("tempdir");
    let note_path = dir.path().join("note.md");
    let note = |claims: &str| {
        format!(
            "---\norigin: personal\nproject: \"\"\nkind: note\nclaims:\n{claims}\n---\n\nbody\n"
        )
    };
    let claim_yaml = |subject: &str| {
        format!(
            "  - subject: {subject}\n    predicate: axis\n    value: v1\n    kind: fact\n    confidence: certain"
        )
    };
    fs::write(&note_path, note(&claim_yaml(&subject_x))).expect("write note v1");
    let note_path_str = note_path.to_string_lossy().into_owned();
    let cfg = BoringConfig::default();

    let mut stats = Stats::default();
    let embedder = CountingEmbed::lenient();
    let outcome = ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor, _>(
        &store,
        &embedder,
        &cfg,
        &note_path_str,
        &mut stats,
    )
    .await
    .expect("first ingest");
    assert_eq!(outcome, FileOutcome::New);
    assert_eq!(claim_row_count(&db, &note_path_str, &subject_x).await, 1);
    assert_eq!(claims_edge_count(&db, &note_path_str, &subject_x).await, 1);

    fs::write(&note_path, note(&claim_yaml(&subject_y))).expect("write note v2");
    let mut stats = Stats::default();
    let outcome = ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor, _>(
        &store,
        &embedder,
        &cfg,
        &note_path_str,
        &mut stats,
    )
    .await
    .expect("second ingest");
    assert_eq!(outcome, FileOutcome::Updated);

    assert_eq!(
        claim_row_count(&db, &note_path_str, &subject_x).await,
        1,
        "the undeclared claim's row persists"
    );
    assert_eq!(
        claim_mirror_node_count(&db, &format!("claim:{subject_x}:axis")).await,
        1,
        "the undeclared claim's node persists — its row still mirrors it"
    );
    assert_eq!(
        claims_edge_count(&db, &note_path_str, &subject_x).await,
        0,
        "the undeclared claim's edge is gone — the note no longer declares it"
    );
    assert_eq!(
        claim_row_count(&db, &note_path_str, &subject_y).await,
        1,
        "the surviving claim has its row"
    );
    assert_eq!(
        claims_edge_count(&db, &note_path_str, &subject_y).await,
        1,
        "the surviving claim has the edge"
    );

    cleanup_claim_edge_fixture(&store, &db, &note_path_str, &subject_x).await;
    cleanup_claim_edge_fixture(&store, &db, &note_path_str, &subject_y).await;
}

// ── sync prune is closed during the migration ─────────────────────────────────

use drudge::ingest::run_with;

async fn note_row_counts(db: &Client, path: &str, subject: &str) -> [i64; 4] {
    let one = |row: tokio_postgres::Row| row.get::<_, i64>(0);
    [
        one(db
            .query_one(
                "SELECT count(*) FROM document WHERE source_path = $1;",
                &[&path],
            )
            .await
            .expect("count documents")),
        one(db
            .query_one(
                "SELECT count(*) FROM chunk WHERE source_path = $1;",
                &[&path],
            )
            .await
            .expect("count chunks")),
        one(db
            .query_one(
                "SELECT count(*) FROM edge WHERE src = $1;",
                &[&format!("doc:{path}")],
            )
            .await
            .expect("count edges")),
        claim_row_count(db, path, subject).await,
    ]
}

async fn prune_skipped_events_naming(store: &Store, path: &str) -> usize {
    store
        .recent_events(EventLogFilter {
            limit: 50,
            component: Some("drudge.ingest"),
            event_name: Some("prune_skipped"),
            status: None,
            run_id: None,
            workflow: None,
            since_hours: None,
        })
        .await
        .expect("read events")
        .into_iter()
        .filter(|e| {
            e.attributes["paths"]
                .as_array()
                .is_some_and(|ps| ps.iter().any(|p| p.as_str() == Some(path)))
        })
        .count()
}

/// Owner decision 2026-09-24: a note that leaves the vault keeps its document·chunk·edge·claim
/// rows and is named in a `prune_skipped` event; putting it back re-ingests onto the same rows.
#[tokio::test]
async fn vanished_note_keeps_rows_and_restored_note_does_not_duplicate() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let subject = unique_subject("prune");
    let vault = tempdir().expect("vault dir");
    let parking = tempdir().expect("parking dir");
    let dirs = vec![vault.path().to_string_lossy().into_owned()];
    let note_path = vault.path().join(format!("{subject}.md"));
    let parked = parking.path().join("parked.md");
    fs::write(
        &note_path,
        format!(
            "---\norigin: personal\nproject: \"\"\nkind: note\nclaims:\n  - subject: {subject}\n    predicate: axis\n    value: v1\n    kind: fact\n    confidence: certain\n---\n\nbody\n"
        ),
    )
    .expect("write note");
    let path = note_path.to_string_lossy().into_owned();
    let cfg = BoringConfig::default();
    let embedder = CountingEmbed::lenient();
    let sync =
        || run_with::<DefaultChunker, FrontmatterGraphExtractor, _>(&store, &embedder, &cfg, &dirs);

    let first = sync().await.expect("first sync");
    assert_eq!(first.kept_vanished, 0, "nothing vanished yet");
    let ingested = note_row_counts(&db, &path, &subject).await;
    assert!(
        ingested.iter().all(|n| *n > 0),
        "fixture ingested: {ingested:?}"
    );
    assert_eq!(prune_skipped_events_naming(&store, &path).await, 0);

    fs::rename(&note_path, &parked).expect("move note out");
    let gone = sync().await.expect("sync with the note gone");
    assert_eq!(
        (gone.kept_vanished, gone.deleted),
        (1, 0),
        "the sync stats count the kept note, not a deletion"
    );
    assert_eq!(
        note_row_counts(&db, &path, &subject).await,
        ingested,
        "the vanished note keeps every row"
    );
    assert_eq!(
        prune_skipped_events_naming(&store, &path).await,
        1,
        "the kept path is named in a prune_skipped event"
    );

    fs::rename(&parked, &note_path).expect("move note back");
    let back = sync().await.expect("sync with the note back");
    assert_eq!(back.kept_vanished, 0, "a restored note is not kept");
    assert_eq!(
        note_row_counts(&db, &path, &subject).await,
        ingested,
        "the restored note re-ingests onto the same rows"
    );
    assert_eq!(
        prune_skipped_events_naming(&store, &path).await,
        1,
        "a present note is not reported again"
    );

    cleanup_claim_edge_fixture(&store, &db, &path, &subject).await;
}

// ── a supersedes edge retires the older note's current claims ─────────────────

async fn sealed_at(db: &Client, subject: &str) -> Option<SystemTime> {
    db.query_one(
        "SELECT superseded_at FROM claim WHERE subject = $1;",
        &[&subject],
    )
    .await
    .expect("read superseded_at")
    .get(0)
}

async fn current_count_at(db: &Client, path: &str) -> i64 {
    db.query_one(
        "SELECT count(*) FROM claim WHERE source_path = $1 AND superseded_at IS NULL;",
        &[&path],
    )
    .await
    .expect("count current claims")
    .get(0)
}

fn whole_second_now() -> SystemTime {
    UNIX_EPOCH
        + Duration::from_secs(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs(),
        )
}

/// Note A holds a `next` item; note B supersedes A → A's item is no longer current and
/// leaves `next_actions`, while B's own items stay current. Live 2026-09-27: the finished
/// `remove code_dsn_from_kb_set …` from wiki-1759 came back every morning because a
/// non-fact claim is sealed only inside its own note and the supersedes edge sealed nothing.
#[tokio::test]
async fn a_superseding_note_retires_the_older_notes_next_items() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;

    let project = unique_subject("sup-project");
    let subject_a = unique_subject("sup-next-a");
    let subject_b = unique_subject("sup-next-b");
    let path_a = unique_path("sup-next-a");
    let path_b = unique_path("sup-next-b");
    let t_a = whole_second_now();
    let t_b = t_a + Duration::from_mins(1);
    let emb = [0.0_f32; 1024];

    let mut front_a = dummy_frontmatter(&path_a);
    front_a.project = project.clone();
    let mut front_b = dummy_frontmatter(&path_b);
    front_b.project = project.clone();
    store
        .upsert_document(&front_a, "sha-sup-a", t_a)
        .await
        .expect("upsert A");
    store
        .upsert_document(&front_b, "sha-sup-b", t_b)
        .await
        .expect("upsert B");
    store
        .upsert_claim(
            &subject_a,
            "next_action",
            "remove code_dsn_from_kb_set from the extraction path",
            &path_a,
            t_a,
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("A's next item");
    store
        .upsert_claim(
            &subject_b,
            "next_action",
            "verify the register drops the retired item",
            &path_b,
            t_b,
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("B's next item");

    store
        .record_supersedes(&[[path_b.clone(), path_a.clone()]], None)
        .await
        .expect("record supersedes");

    assert_eq!(
        sealed_at(&db, &subject_a).await,
        Some(t_b),
        "A's item is stamped with the newer note's latest claim time"
    );
    assert_eq!(
        current_count_at(&db, &path_b).await,
        1,
        "B's own claims stay current"
    );

    let out = drudge::ask::next_action_register(&store, Some(&project), &[], 50)
        .await
        .expect("next actions");
    let subjects: Vec<&str> = out.items.iter().map(|r| r.subject.as_str()).collect();
    assert!(
        !subjects.contains(&subject_a.as_str()),
        "the superseded note's item must not come back in next_actions"
    );
    assert!(
        subjects.contains(&subject_b.as_str()),
        "the superseding note's own item stays"
    );

    store.delete_document(&path_a).await.expect("cleanup A");
    store.delete_document(&path_b).await.expect("cleanup B");
}

/// Repeat calls change nothing: the second `record_supersedes` finds no current row to
/// stamp, so the seal keeps the first stamp instead of re-stamping with now().
#[tokio::test]
async fn record_supersedes_twice_seals_once() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;

    let subject_a = unique_subject("sup-idem-a");
    let subject_b = unique_subject("sup-idem-b");
    let path_a = unique_path("sup-idem-a");
    let path_b = unique_path("sup-idem-b");
    let t_a = whole_second_now();
    let t_b = t_a + Duration::from_mins(1);
    let emb = [0.0_f32; 1024];

    store
        .upsert_document(&dummy_frontmatter(&path_a), "sha-idem-a", t_a)
        .await
        .expect("upsert A");
    store
        .upsert_document(&dummy_frontmatter(&path_b), "sha-idem-b", t_b)
        .await
        .expect("upsert B");
    store
        .upsert_claim(
            &subject_a,
            "next_action",
            "x",
            &path_a,
            t_a,
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("A's item");
    store
        .upsert_claim(
            &subject_b,
            "next_action",
            "y",
            &path_b,
            t_b,
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("B's item");

    for round in 1..=2 {
        store
            .record_supersedes(&[[path_b.clone(), path_a.clone()]], None)
            .await
            .expect("record supersedes");
        assert_eq!(
            sealed_at(&db, &subject_a).await,
            Some(t_b),
            "round {round}: the stamp must not move"
        );
        assert_eq!(
            sealed_at(&db, &subject_b).await,
            None,
            "round {round}: B's own claim stays current"
        );
    }

    store.delete_document(&path_a).await.expect("cleanup A");
    store.delete_document(&path_b).await.expect("cleanup B");
}

/// The store-side owner guard agrees with the door: an owner-written older note keeps its
/// current claims when a non-owner note supersedes it. The edge is written (the door, not
/// the store, refuses the pair) but the seal skips the owner's rows — including on a later
/// compact, which walks every edge on the graph.
#[tokio::test]
async fn owner_note_superseded_by_a_non_owner_keeps_its_current_claims() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;

    let subject_a = unique_subject("sup-owner-a");
    let subject_b = unique_subject("sup-owner-b");
    let path_a = unique_path("sup-owner-a");
    let path_b = unique_path("sup-owner-b");
    let t_a = whole_second_now();
    let t_b = t_a + Duration::from_mins(1);
    let emb = [0.0_f32; 1024];

    let mut front_a = dummy_frontmatter(&path_a);
    front_a.author = Author::Owner;
    store
        .upsert_document(&front_a, "sha-owner-a", t_a)
        .await
        .expect("upsert owner A");
    store
        .upsert_document(&dummy_frontmatter(&path_b), "sha-owner-b", t_b)
        .await
        .expect("upsert non-owner B");
    store
        .upsert_claim(
            &subject_a,
            "next_action",
            "x",
            &path_a,
            t_a,
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("owner A's item");
    store
        .upsert_claim(
            &subject_b,
            "next_action",
            "y",
            &path_b,
            t_b,
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("B's item");

    store
        .record_supersedes(&[[path_b.clone(), path_a.clone()]], None)
        .await
        .expect("record supersedes");
    assert_eq!(
        sealed_at(&db, &subject_a).await,
        None,
        "a non-owner note must not retire what the owner wrote"
    );

    let summary = store.compact().await.expect("compact");
    assert_eq!(
        summary.report.sealed_superseded_claims, 0,
        "the compact sweep must hit the same guard"
    );
    assert_eq!(
        sealed_at(&db, &subject_a).await,
        None,
        "compact over the same edge still leaves the owner's claim current"
    );

    store.delete_document(&path_a).await.expect("cleanup A");
    store.delete_document(&path_b).await.expect("cleanup B");
}

/// Edges written before the seal step existed (the live wiki-1945 → wiki-1759 among them,
/// four current claims at 2026-09-27) retire their older note's claims on the next
/// compact — reported in the summary, idempotent across runs.
#[tokio::test]
async fn compact_seals_claims_under_a_pre_existing_edge_once() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;

    let subject_a = unique_subject("sup-compact-a");
    let subject_b = unique_subject("sup-compact-b");
    let path_a = unique_path("sup-compact-a");
    let path_b = unique_path("sup-compact-b");
    let t_a = whole_second_now();
    let t_b = t_a + Duration::from_mins(1);
    let emb = [0.0_f32; 1024];

    store
        .upsert_document(&dummy_frontmatter(&path_a), "sha-compact-a", t_a)
        .await
        .expect("upsert A");
    store
        .upsert_document(&dummy_frontmatter(&path_b), "sha-compact-b", t_b)
        .await
        .expect("upsert B");
    store
        .upsert_claim(
            &subject_a,
            "next_action",
            "x",
            &path_a,
            t_a,
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("A's item");
    store
        .upsert_claim(
            &subject_b,
            "next_action",
            "y",
            &path_b,
            t_b,
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("B's item");
    // The edge predates the seal: written straight onto the graph, the live-DB shape.
    db.execute(
        "INSERT INTO edge (src, dst, kind, judge) VALUES ($1, $2, 'supersedes', NULL);",
        &[&format!("doc:{path_b}"), &format!("doc:{path_a}")],
    )
    .await
    .expect("insert pre-existing edge");
    assert_eq!(
        sealed_at(&db, &subject_a).await,
        None,
        "the unbackfilled edge leaves A's claim current, as on the live DB"
    );

    let first = store.compact().await.expect("first compact");
    assert!(
        first.report.sealed_superseded_claims >= 1,
        "compact reports the retirement, not a silent side effect"
    );
    assert_eq!(
        sealed_at(&db, &subject_a).await,
        Some(t_b),
        "the pre-existing edge retires A's claim with B's latest claim time"
    );

    let second = store.compact().await.expect("second compact");
    assert_eq!(
        second.report.sealed_superseded_claims, 0,
        "a repeat compact seals nothing — the stamp does not move"
    );
    assert_eq!(
        sealed_at(&db, &subject_a).await,
        Some(t_b),
        "the stamp is unchanged after the second compact"
    );

    store.delete_document(&path_a).await.expect("cleanup A");
    store.delete_document(&path_b).await.expect("cleanup B");
}
