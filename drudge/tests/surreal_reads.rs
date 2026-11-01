//! C-SURALL2 Z2③: on `backend=surreal` the engine's reads come out of Surreal. Each test fills a
//! throwaway pg and a throwaway Surreal with the same rows, asks the pg-backed store and the
//! Surreal-backed store the same questions, and prints `Z2R <read>: same n/N`. Then it removes the
//! rows from pg (or writes them to Surreal only): a read that still goes to pg answers differently
//! there, which is what makes each test fail on the code before this change.
//!
//! Needs a throwaway pgvector and a throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test surreal_reads -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::too_many_lines)]

use std::fs;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::code_index::{CodeIndexStore, sync_repository};
use drudge::config::{CodeIndexSource, CodeLanguage, SurrealConfig};
use drudge::frontmatter::FrontMatter;
use drudge::store::{
    AnchorCheckWatermark, Doc, EventLogFilter, LoggedHit, Store, SurrealClient, VectorRoute,
};
use drudge::vector::VectorIndex;
use serde_json::json;
use tempfile::tempdir;
use tokio_postgres::NoTls;

const DIM: usize = 1024;

/// (name, exclude_origins, project, kinds, anchor prefix, include_stale) of one `current_claims`.
type Ask<'a> = (
    &'a str,
    &'a [String],
    Option<&'a str>,
    Option<&'a [String]>,
    Option<&'a str>,
    bool,
);

fn env(name: &str) -> String {
    std::env::var(name).expect(name)
}

fn nanos() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos()
}

fn client_at(namespace: &str) -> SurrealClient {
    let config: SurrealConfig = serde_json::from_value(
        json!({"url": env("BORING_TEST_SURREAL_URL"), "namespace": namespace, "database": namespace}),
    )
    .unwrap();
    SurrealClient::new(config.settings(&|n| std::env::var(n).ok()).unwrap()).unwrap()
}

async fn pg_client() -> tokio_postgres::Client {
    let (db, conn) = tokio_postgres::connect(&env("BORING_TEST_DATABASE_URL"), NoTls)
        .await
        .expect("connect");
    tokio::spawn(conn);
    db
}

/// Prints `Z2R <read>: same n/N` and fails on any difference.
fn same<T: std::fmt::Debug>(read: &str, pg: &[T], surreal: &[T]) {
    let (p, s): (Vec<String>, Vec<String>) = (
        pg.iter().map(|x| format!("{x:?}")).collect(),
        surreal.iter().map(|x| format!("{x:?}")).collect(),
    );
    let equal = p.iter().zip(&s).filter(|(a, b)| a == b).count();
    println!("Z2R {read}: same {equal}/{}", p.len());
    assert_eq!(p, s, "{read}: pg answer vs Surreal answer");
}

// ── log tables ───────────────────────────────────────────────────────────────────────────────

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn the_four_log_reads_answer_from_surreal_and_equal_pg() {
    let dsn = env("BORING_TEST_DATABASE_URL");
    let pg = Store::open(&dsn, DIM).await.expect("open store");
    let db = pg_client().await;
    db.batch_execute(
        "TRUNCATE event_log, recall_label, sync_state, query_log RESTART IDENTITY CASCADE;",
    )
    .await
    .unwrap();
    let client = client_at(&format!("z2r_logs_{}", nanos()));

    for (i, (component, status, run, workflow)) in [
        ("hook", "ok", Some("run-a"), Some("recall")),
        ("hook", "failed", Some("run-a"), None),
        ("door", "ok", None, Some("recall")),
        ("cron", "ok", Some("run-b"), None),
        ("door", "failed", Some("run-b"), Some("sync")),
    ]
    .into_iter()
    .enumerate()
    {
        pg.log_event(&json!({
            "component": component, "event": format!("ev{}", i % 2), "status": status,
            "run_id": run, "workflow": workflow, "note": "한글 \"인용\" \\ 끝"
        }))
        .await
        .unwrap();
    }
    db.execute(
        "UPDATE event_log SET observed_at = now() - interval '100 hours' WHERE component = 'cron'",
        &[],
    )
    .await
    .unwrap();
    for q in ["첫 질문", "second \"quoted\" question", "third"] {
        pg.log_query(
            "/ask",
            q,
            &[LoggedHit {
                path: format!("/vault/{q}.md"),
                dist: Some(0.123_456_79),
                dist_kind: Some(drudge::store::DistKind::VectorCosine),
            }],
            &["src".to_string()],
            "snippet",
            Some(42),
        )
        .await
        .unwrap();
    }
    let ids: Vec<i32> = pg
        .recent_queries(10)
        .await
        .unwrap()
        .iter()
        .map(|q| q.id)
        .collect();
    for (id, judge, verdict) in [
        (ids[0], "llm", "relevant"),
        (ids[0], "human", "relevant"),
        (ids[1], "llm", "irrelevant"),
        (ids[1], "human", "relevant"),
        (ids[2], "llm", "unsure"),
        (ids[2], "human", "irrelevant"),
    ] {
        pg.record_recall_label(id, 0, judge, verdict, "m", "n")
            .await
            .unwrap();
    }
    let mark = AnchorCheckWatermark {
        valid_from: UNIX_EPOCH + Duration::from_micros(1_761_900_000_123_456),
        subject: "주제".to_string(),
        predicate: "pred".to_string(),
    };
    pg.set_anchor_check_watermark(&mark).await.unwrap();

    client.copy_logs(&pg).await.expect("copy pass");
    let routed = Store::open(&dsn, DIM)
        .await
        .unwrap()
        .with_vector_route(VectorRoute::Surreal(client));

    let filters = |f: fn(&mut EventLogFilter<'static>)| {
        let mut e = EventLogFilter {
            limit: 50,
            component: None,
            event_name: None,
            status: None,
            run_id: None,
            workflow: None,
            since_hours: None,
        };
        f(&mut e);
        e
    };
    let variants: Vec<(&str, EventLogFilter<'static>)> = vec![
        ("recent_events all", filters(|_| {})),
        ("recent_events limit 2", filters(|e| e.limit = 2)),
        (
            "recent_events component",
            filters(|e| e.component = Some("hook")),
        ),
        (
            "recent_events event_name",
            filters(|e| e.event_name = Some("ev1")),
        ),
        (
            "recent_events status",
            filters(|e| e.status = Some("failed")),
        ),
        (
            "recent_events run_id",
            filters(|e| e.run_id = Some("run-b")),
        ),
        (
            "recent_events workflow",
            filters(|e| e.workflow = Some("recall")),
        ),
        (
            "recent_events since 48h",
            filters(|e| e.since_hours = Some(48)),
        ),
    ];
    let mut expected_events = Vec::new();
    for (name, f) in &variants {
        let a = pg.recent_events(clone_filter(f)).await.unwrap();
        let b = routed.recent_events(clone_filter(f)).await.unwrap();
        same(name, &a, &b);
        expected_events.push((name, a.len()));
    }
    same(
        "recent_queries",
        &pg.recent_queries(10).await.unwrap(),
        &routed.recent_queries(10).await.unwrap(),
    );
    same(
        "recent_queries limit 2",
        &pg.recent_queries(2).await.unwrap(),
        &routed.recent_queries(2).await.unwrap(),
    );
    let mut labels_pg = pg.recent_recall_labels(10).await.unwrap();
    let mut labels_sr = routed.recent_recall_labels(10).await.unwrap();
    labels_pg.sort_by_key(|l| (l.query_log_id, l.judge.clone()));
    labels_sr.sort_by_key(|l| (l.query_log_id, l.judge.clone()));
    same("recent_recall_labels", &labels_pg, &labels_sr);
    let stats_pg = pg.recall_label_stats().await.unwrap();
    let stats_sr = routed.recall_label_stats().await.unwrap();
    same("recall_label_stats", &[&stats_pg], &[&stats_sr]);
    assert_eq!((stats_sr.1, stats_sr.2), (1, 2), "agreed/compared");
    same(
        "anchor_check_watermark",
        &[pg.anchor_check_watermark().await.unwrap()],
        &[routed.anchor_check_watermark().await.unwrap()],
    );

    // Control: pg loses every row. A read that still asks pg now answers empty.
    db.batch_execute(
        "TRUNCATE event_log, recall_label, sync_state, query_log RESTART IDENTITY CASCADE;",
    )
    .await
    .unwrap();
    for ((name, f), (_, had)) in variants.iter().zip(&expected_events) {
        let got = routed.recent_events(clone_filter(f)).await.unwrap();
        assert_eq!(got.len(), *had, "{name} after pg was emptied");
    }
    assert_eq!(routed.recent_queries(10).await.unwrap().len(), 3);
    assert_eq!(routed.recent_recall_labels(10).await.unwrap().len(), 6);
    assert_eq!(routed.recall_label_stats().await.unwrap().2, 2);
    assert!(routed.anchor_check_watermark().await.unwrap().is_some());
    println!("Z2R log reads after pg was emptied: still answered from Surreal");
}

fn clone_filter<'a>(f: &EventLogFilter<'a>) -> EventLogFilter<'a> {
    EventLogFilter {
        limit: f.limit,
        component: f.component,
        event_name: f.event_name,
        status: f.status,
        run_id: f.run_id,
        workflow: f.workflow,
        since_hours: f.since_hours,
    }
}

// ── code_index ───────────────────────────────────────────────────────────────────────────────

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn the_code_reads_answer_from_surreal_and_equal_pg() {
    let dsn = env("BORING_TEST_DATABASE_URL");
    let mut pg = CodeIndexStore::connect(&dsn).unwrap();
    pg.initialize().await.expect("initialize");
    let db = pg_client().await;
    db.batch_execute(
        "TRUNCATE code_index.relation, code_index.symbol, code_index.file, code_index.repository CASCADE;",
    )
    .await
    .unwrap();
    let client = client_at(&format!("z2r_code_{}", nanos()));

    // Two repositories with different symbols: rows tied on every sort key have no defined order.
    let first = tempdir().unwrap();
    fs::write(
        first.path().join("alpha.rs"),
        "pub fn parse_token() { helper(); }\nfn helper() {}\nstruct TokenStream;\nimpl TokenStream { fn next(&self) { helper(); } }\n",
    )
    .unwrap();
    fs::write(
        first.path().join("beta_token.rs"),
        "pub fn Parse_Other() {}\npub fn other_helper() { parse_token(); }\n",
    )
    .unwrap();
    let second = tempdir().unwrap();
    fs::write(
        second.path().join("gamma.rs"),
        "pub fn parseGamma() { helper_two(); }\nfn helper_two() {}\nstruct Token2;\n",
    )
    .unwrap();
    for (id, dir) in [("z2r-a", &first), ("z2r-b", &second)] {
        let source = CodeIndexSource::new(
            id,
            format!("Test {id}"),
            dir.path().to_path_buf(),
            CodeLanguage::Rust,
            true,
        )
        .unwrap();
        sync_repository(&mut pg, &source).await.expect("sync");
        client.copy_code_repo(&pg, id).await.expect("copy");
    }
    let routed = CodeIndexStore::connect(&dsn)
        .unwrap()
        .with_surreal(Some(client));

    let status_pg = pg.status(None).await.unwrap();
    let status_sr = routed.status(None).await.unwrap();
    same("code status all", &status_pg, &status_sr);
    same(
        "code status one repository",
        &pg.status(Some("z2r-b")).await.unwrap(),
        &routed.status(Some("z2r-b")).await.unwrap(),
    );
    same(
        "code status unknown repository",
        &pg.status(Some("nope")).await.unwrap(),
        &routed.status(Some("nope")).await.unwrap(),
    );
    let mut first_hits = Vec::new();
    for q in [
        "parse",
        "TOKEN",
        "helper",
        "beta_token",
        "next",
        "zzz-none",
        "",
    ] {
        let a = pg.search(q, None).await.unwrap();
        let b = routed.search(q, None).await.unwrap();
        same(&format!("code search {q:?}"), &a, &b);
        first_hits.push((q, a));
    }
    same(
        "code search in one repository",
        &pg.search("helper", Some("z2r-a")).await.unwrap(),
        &routed.search("helper", Some("z2r-a")).await.unwrap(),
    );
    let mut symbols = 0;
    for hit in pg.search("", None).await.unwrap() {
        let a = pg.symbol(&hit.id).await.unwrap();
        let b = routed.symbol(&hit.id).await.unwrap();
        same("code symbol", &[a], &[b]);
        symbols += 1;
    }
    assert!(symbols > 0);
    same(
        "code symbol missing id",
        &[pg.symbol("no-such-id").await.unwrap()],
        &[routed.symbol("no-such-id").await.unwrap()],
    );

    // Control: pg loses the index. A read that still asks pg now answers empty.
    db.batch_execute(
        "TRUNCATE code_index.relation, code_index.symbol, code_index.file, code_index.repository CASCADE;",
    )
    .await
    .unwrap();
    assert_eq!(routed.status(None).await.unwrap().len(), status_pg.len());
    for (q, hits) in first_hits {
        assert_eq!(
            routed.search(q, None).await.unwrap().len(),
            hits.len(),
            "search {q:?} after pg was emptied"
        );
    }
    println!("Z2R code reads after pg was emptied: still answered from Surreal");
}

// ── chunks and claims ────────────────────────────────────────────────────────────────────────

fn tilted(tilt_at: usize, cos: f32, salt: usize) -> Vec<f32> {
    let mut v = vec![0.0; DIM];
    v[0] = cos;
    v[tilt_at] = (1.0 - cos * cos).sqrt();
    v[10 + salt % 1000] = 0.001;
    v
}

fn query_vec() -> Vec<f32> {
    let mut q = vec![0.0_f32; DIM];
    q[0] = 1.0;
    q
}

fn front(path: &str, project: &str, origin: &str) -> FrontMatter {
    FrontMatter {
        origin: origin.to_string(),
        project: project.to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("z2r fixture".to_string()),
        ..Default::default()
    }
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn chunk_and_claim_reads_equal_pg_over_the_same_rows() {
    let dsn = env("BORING_TEST_DATABASE_URL");
    let pg = Store::open(&dsn, DIM).await.expect("open store");
    let db = pg_client().await;
    db.batch_execute("DELETE FROM claim; DELETE FROM document;")
        .await
        .unwrap();
    let client = client_at(&format!("z2r_hits_{}", nanos()));
    let n = nanos();
    let project = format!("z2r-{n}");
    let now = SystemTime::now();

    let fixtures = [
        (0.99_f32, "personal", "risk", 1_u64),
        (0.90, "personal", "decision", 1),
        (0.85, "company", "risk", 1),
        (0.80, "personal", "risk", 100),
    ];
    for (i, (cos, origin, kind, age_h)) in fixtures.iter().enumerate() {
        let path = format!("/vault/wiki/z2r-{n}-{i}.md");
        let fm = front(&path, &project, origin);
        let emb = tilted(1 + i % 3, *cos, usize::try_from(n % 1_000_003).unwrap());
        pg.upsert_document(&fm, "sha", now - Duration::from_hours(*age_h))
            .await
            .unwrap();
        pg.upsert_chunk(&Doc {
            id: format!("{path}#0"),
            content: format!("alpha bravo chunk {i} 한글"),
            embedding: emb.clone(),
            front: fm,
            chunk_idx: 0,
        })
        .await
        .unwrap();
        pg.upsert_claim_with_anchor(
            &format!("subject-{n}-{i}"),
            "observed",
            &format!("a long enough value for claim number {i}"),
            &path,
            now - Duration::from_hours(24),
            &emb,
            kind,
            "certain",
            Some(&format!("code:src/z2r/{i}.rs:10")),
            None,
            None,
        )
        .await
        .unwrap();
    }
    db.execute(
        "UPDATE claim SET stale_at = now(), stale_reason = 'gone' WHERE subject = $1",
        &[&format!("subject-{n}-3")],
    )
    .await
    .unwrap();
    let tables = client.fill_from(&pg).await.expect("fill");
    assert!(tables.iter().all(drudge::store::FillTable::same), "fill");
    let routed = Store::open(&dsn, DIM)
        .await
        .unwrap()
        .with_vector_route(VectorRoute::Surreal(client));

    let q = query_vec();
    let company = vec!["company".to_string()];
    let risk = vec!["risk".to_string()];
    let none: Vec<String> = Vec::new();
    let asks: Vec<Ask> = vec![
        ("current_claims plain", &none, None, None, None, false),
        (
            "current_claims project",
            &none,
            Some(&project),
            None,
            None,
            false,
        ),
        (
            "current_claims exclude origin",
            &company,
            Some(&project),
            None,
            None,
            false,
        ),
        (
            "current_claims kinds",
            &none,
            Some(&project),
            Some(&risk),
            None,
            false,
        ),
        (
            "current_claims anchor prefix",
            &none,
            Some(&project),
            None,
            Some("src/z2r/1"),
            false,
        ),
        (
            "current_claims include_stale",
            &none,
            Some(&project),
            None,
            None,
            true,
        ),
        (
            "current_claims other project",
            &none,
            Some("elsewhere"),
            None,
            None,
            false,
        ),
    ];
    for (name, exclude, proj, kinds, anchor, stale) in asks {
        let a = pg
            .current_claims(&q, 50, exclude, proj, kinds, anchor, stale)
            .await
            .unwrap();
        let b = routed
            .current_claims(&q, 50, exclude, proj, kinds, anchor, stale)
            .await
            .unwrap();
        assert!(
            name.contains("other") || !a.is_empty(),
            "{name}: oracle empty"
        );
        same(name, &a, &b);
    }
    let ids = |hits: Vec<drudge::store::Hit>| -> Vec<(String, String, String, String)> {
        hits.into_iter()
            .map(|h| (h.id, h.content, h.origin, h.source_path))
            .collect()
    };
    same(
        "vector_search",
        &ids(pg.vector_search(&q, 50).await.unwrap()),
        &ids(routed.vector_search(&q, 50).await.unwrap()),
    );
    for (name, proj, hours) in [
        (
            "vector_search_filtered project",
            Some(project.as_str()),
            None,
        ),
        (
            "vector_search_filtered window 48h",
            Some(project.as_str()),
            Some(48),
        ),
        ("vector_search_filtered window only", None, Some(48)),
    ] {
        let a = ids(pg
            .vector_search_filtered(&q, 50, proj, hours)
            .await
            .unwrap());
        let b = ids(routed
            .vector_search_filtered(&q, 50, proj, hours)
            .await
            .unwrap());
        assert!(!a.is_empty(), "{name}: oracle empty");
        same(name, &a, &b);
    }
    for (name, proj, hours) in [
        ("text_search_filtered", None, None),
        (
            "text_search_filtered project+window",
            Some(project.as_str()),
            Some(48),
        ),
    ] {
        let a = ids(pg
            .text_search_filtered("alpha", 50, proj, hours)
            .await
            .unwrap());
        let b = ids(routed
            .text_search_filtered("alpha", 50, proj, hours)
            .await
            .unwrap());
        assert!(!a.is_empty(), "{name}: oracle empty");
        same(name, &a, &b);
    }
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_claim_written_only_into_surreal_is_read_back_without_pg() {
    let dsn = env("BORING_TEST_DATABASE_URL");
    let pg = Store::open(&dsn, DIM).await.expect("open store");
    let db = pg_client().await;
    db.batch_execute("DELETE FROM claim; DELETE FROM document;")
        .await
        .unwrap();
    let client = client_at(&format!("z2r_only_{}", nanos()));
    client.fill_from(&pg).await.expect("fill of an empty pg");
    let routed = Store::open(&dsn, DIM)
        .await
        .unwrap()
        .with_vector_route(VectorRoute::Surreal(client));

    let n = nanos();
    let path = format!("/vault/wiki/z2r-only-{n}.md");
    let fm = front(&path, "z2r-only", "personal");
    let emb = tilted(1, 0.95, 7);
    routed
        .upsert_document(&fm, "sha", SystemTime::now())
        .await
        .unwrap();
    routed
        .upsert_chunk(&Doc {
            id: format!("{path}#0"),
            content: "only in surreal".to_string(),
            embedding: emb.clone(),
            front: fm,
            chunk_idx: 0,
        })
        .await
        .unwrap();
    routed
        .upsert_claim_with_anchor(
            &format!("only-{n}"),
            "observed",
            "a long enough value to count as a statement",
            &path,
            SystemTime::now(),
            &emb,
            "risk",
            "certain",
            None,
            None,
            None,
        )
        .await
        .unwrap();

    let in_pg: i64 = db
        .query_one(
            "SELECT count(*) FROM claim WHERE subject = $1",
            &[&format!("only-{n}")],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(in_pg, 0, "Z1: the claim was written to Surreal only");
    let claims = routed
        .current_claims(&query_vec(), 10, &[], Some("z2r-only"), None, None, false)
        .await
        .expect("current_claims must read the claim Surreal holds");
    println!("Z2R claim only in Surreal: read back {}/1", claims.len());
    assert_eq!(claims.len(), 1);
    assert_eq!(claims[0].claim.subject, format!("only-{n}"));
    let hits = routed
        .vector_search_filtered(&query_vec(), 10, Some("z2r-only"), None)
        .await
        .expect("vector search");
    assert_eq!(hits.len(), 1);
}
