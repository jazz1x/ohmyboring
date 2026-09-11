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

use drudge::frontmatter::{Claim, FrontMatter};
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

    let mut emb_p = [0.0_f32; 1024];
    emb_p[0] = 1.0;
    let mut emb_c = [0.0_f32; 1024];
    emb_c[1] = 1.0;

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

    let query = [0.5_f32; 1024]; // near both
    let subjects = |rows: Vec<drudge::store::AnchoredClaim>| -> Vec<String> {
        rows.into_iter().map(|c| c.claim.subject).collect()
    };

    // No exclusion → both visible.
    let all = subjects(
        store
            .current_claims(&query, 20, &[], None, None, None)
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
            .current_claims(&query, 20, &["company".to_string()], None, None, None)
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
        .claim_is_unchanged(&subject, "axis", "v1", &path, "fact", "certain")
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
            .claim_is_unchanged(&subject, "axis", "v1", &path, "fact", "certain")
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
                .claim_is_unchanged(&subject, "axis", value, src, kind, conf)
                .await
                .expect("probe"),
            "{why} must not read as unchanged"
        );
    }

    // A value that was superseded and then came back must NOT read as unchanged. Without the
    // `superseded_at IS NULL` filter the probe finds the sealed row, skips the insert, and the
    // current value stays at v2 — so `claims` answers with something the note no longer says.
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
            .claim_is_unchanged(&subject, "axis", "v1", &path, "fact", "certain")
            .await
            .expect("probe"),
        "a sealed value returning is new information, not an unchanged claim"
    );
    assert!(
        store
            .claim_is_unchanged(&subject, "axis", "v2", &path, "fact", "certain")
            .await
            .expect("probe"),
        "the current value must still read as unchanged"
    );

    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("cleanup claim");
}

/// D2 step 1: a claim inherits the code anchor its note body cites, and the row is marked
/// `era = 'anchored'`; a note with no citation is 'unanchored'; a row written before the era
/// column existed keeps the 'pre-anchor' default. Mirrors `FrontmatterGraphExtractor`'s claim
/// path (the suite has no LLM for the embedding, so the resolver output is attached directly).
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

    // Deltas, not absolutes: the suite shares one DB and other tests may leave rows behind.
    let before = store.claim_era_counts().await.expect("era counts before");

    // The note body cites a file at a line; the resolver turns that into the stored anchor.
    let body = "the regression lives in a/b.rs:10 since the refactor";
    let note_anchors = drudge::anchor::from_note_body(project, body);
    let anchor = drudge::anchor::anchor_for_claim(&note_anchors, &subject, "regression")
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

    // No citation in the body → anchor NULL, era 'unanchored'.
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

    // A row written the pre-anchor way (no anchor/era in the INSERT) keeps the era default.
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

    // The D2 adoption counter sees each new era: one row anchored, one unanchored, one legacy.
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

/// D2 step 1: the `anchor_path` filter on `current_claims` (and the `claims` MCP tool) keeps
/// only rows whose anchor starts with `<project>:<anchor_path>`.
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
        )
        .await
        .expect("upsert anchored claim");

    let hits = store
        .current_claims(&emb, 10, &[], Some(project), None, Some("a/b.rs"))
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
        .current_claims(&emb, 10, &[], Some(project), None, Some("no/such.rs"))
        .await
        .expect("claims filtered by missing anchor_path");
    assert!(
        !misses.iter().any(|c| c.subject == subject),
        "a non-matching anchor_path must drop the claim"
    );
    // Prefix semantics (contract): only the path STARTING with anchor_path matches — a bare
    // suffix of the path (`b.rs` inside `a/b.rs`) is not `<project>:b.rs…` and must not match.
    let suffix = store
        .current_claims(&emb, 10, &[], Some(project), None, Some("b.rs"))
        .await
        .expect("claims filtered by path suffix");
    assert!(
        !suffix.iter().any(|c| c.subject == subject),
        "a path suffix is not an anchor prefix and must not match"
    );
    let prefix = store
        .current_claims(&emb, 10, &[], Some(project), None, Some("a/"))
        .await
        .expect("claims filtered by path prefix");
    assert!(
        prefix.iter().any(|c| c.subject == subject),
        "a path prefix must match"
    );
    let unfiltered = store
        .current_claims(&emb, 10, &[], Some(project), None, None)
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
