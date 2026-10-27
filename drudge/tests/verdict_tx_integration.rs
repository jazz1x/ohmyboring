//! One verdict write = one transaction (contract C-PORT2b stage 1). Today `record_consumption`
//! commits statement by statement over fresh pool connections, so a failure midway leaves the
//! earlier rows. These tests are written red against that code; the Python twin
//! (`verdict/run.py::_transact`, `test_a_failure_midway_writes_nothing`) is the model.
//!
//! The failure is injected in SQL: a trigger raises when an edge row points at the "boom" doc.
//!
//! Needs a Postgres instance reachable via `BORING_TEST_DATABASE_URL`; without it the tests skip.
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::frontmatter::FrontMatter;
use drudge::store::Store;
use tokio_postgres::{Client, NoTls};

const BOOM_PATH: &str = "/vault/wiki/tx-test-boom.md";
const ITERATIONS: usize = 50;
const OLDER_NOTES: usize = 8;

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

fn unique_id(prefix: &str) -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}-{ts}-{}", SEQ.fetch_add(1, Ordering::Relaxed))
}

fn unique_path(prefix: &str) -> String {
    format!("/vault/wiki/tx-test-{}.md", unique_id(prefix))
}

/// Edges pointing at the boom doc raise. Idempotent; harmless to other suites (fixed fake path).
async fn install_boom(db: &Client) {
    db.batch_execute(&format!(
        "CREATE OR REPLACE FUNCTION tx_test_fail_on_boom() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN IF NEW.dst = 'doc:{BOOM_PATH}' THEN RAISE EXCEPTION 'boom'; END IF; RETURN NEW; END $$;
         DROP TRIGGER IF EXISTS tx_test_edge_boom ON edge;
         CREATE TRIGGER tx_test_edge_boom BEFORE INSERT ON edge
             FOR EACH ROW EXECUTE FUNCTION tx_test_fail_on_boom();"
    ))
    .await
    .expect("install boom trigger");
}

async fn seed_doc(store: &Store, path: &str, author: &str) {
    let front = FrontMatter {
        origin: "personal".to_string(),
        project: "test".to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("tx test note".to_string()),
        tags: vec!["test".to_string()],
        author: author.parse().expect("author"),
        ..Default::default()
    };
    store
        .upsert_document(&front, "sha-v1", SystemTime::now())
        .await
        .expect("upsert doc");
}

async fn seed_claim(store: &Store, path: &str, offset_secs: u64) {
    store
        .upsert_claim(
            &unique_id("tx-subject"),
            "deploy_day",
            "v1",
            path,
            SystemTime::now() + Duration::from_secs(offset_secs),
            &[0.0_f32; 1024],
            "fact",
            "certain",
        )
        .await
        .expect("upsert claim");
}

async fn count(db: &Client, sql: &str, arg: &str) -> i64 {
    db.query_one(sql, &[&arg]).await.expect("count").get(0)
}

async fn session_rows(db: &Client, session: &str) -> (i64, i64) {
    let node = format!("session:{session}");
    (
        count(db, "SELECT count(*) FROM node WHERE id = $1;", &node).await,
        count(db, "SELECT count(*) FROM edge WHERE src = $1;", &node).await,
    )
}

async fn supersedes_edges_from(db: &Client, path: &str) -> i64 {
    let src = format!("doc:{path}");
    count(
        db,
        "SELECT count(*) FROM edge WHERE kind = 'supersedes' AND src = $1;",
        &src,
    )
    .await
}

async fn sealed_claims(db: &Client, path: &str) -> i64 {
    count(
        db,
        "SELECT count(*) FROM claim WHERE source_path = $1 AND superseded_at IS NOT NULL;",
        path,
    )
    .await
}

fn pair(newer: &str, older: &str) -> [String; 2] {
    [newer.to_string(), older.to_string()]
}

#[tokio::test]
async fn a_failure_midway_in_consumption_writes_nothing() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    install_boom(&db).await;
    let ok = unique_path("ok");
    seed_doc(&store, &ok, "unknown").await;
    seed_doc(&store, BOOM_PATH, "unknown").await;

    let failing = unique_id("tx-s");
    let result = store
        .record_consumption(
            &failing,
            "2026-01-01",
            &[ok.clone(), BOOM_PATH.to_string()],
            &[],
            &[],
            None,
        )
        .await;
    assert!(result.is_err(), "boom edge must fail the request");
    let (nodes, edges) = session_rows(&db, &failing).await;
    eprintln!("consumption after failure: session nodes {nodes}, edges {edges}");
    assert_eq!((nodes, edges), (0, 0), "a failed request writes nothing");

    let control = unique_id("tx-s");
    store
        .record_consumption(&control, "2026-01-01", &[ok], &[], &[], None)
        .await
        .expect("control: without boom the same request succeeds");
    assert_eq!(
        session_rows(&db, &control).await,
        (1, 1),
        "control: rows written"
    );
}

#[tokio::test]
async fn a_failure_midway_in_supersedes_writes_nothing() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    install_boom(&db).await;
    let (newer1, older1, newer2) = (unique_path("n1"), unique_path("o1"), unique_path("n2"));
    for p in [&newer1, &older1, &newer2] {
        seed_doc(&store, p, "unknown").await;
    }
    seed_doc(&store, BOOM_PATH, "unknown").await;
    seed_claim(&store, &newer1, 1).await;
    seed_claim(&store, &older1, 0).await;

    let result = store
        .record_supersedes(&[pair(&newer1, &older1), pair(&newer2, BOOM_PATH)], None)
        .await;
    assert!(result.is_err(), "boom edge must fail the second pair");
    let edges = supersedes_edges_from(&db, &newer1).await;
    let sealed = sealed_claims(&db, &older1).await;
    eprintln!("supersedes after failure: first-pair edges {edges}, sealed claims {sealed}");
    assert_eq!(
        (edges, sealed),
        (0, 0),
        "a failed request leaves no first-pair edge or seal"
    );

    // control: same first pair alone succeeds and writes
    store
        .record_supersedes(&[pair(&newer1, &older1)], None)
        .await
        .expect("control: without boom the request succeeds");
    assert_eq!(
        supersedes_edges_from(&db, &newer1).await,
        1,
        "control: edge"
    );
    assert_eq!(sealed_claims(&db, &older1).await, 1, "control: seal");
}

#[derive(Default)]
struct Tally {
    half_written: usize,
    pool_or_timeout: Vec<String>,
    other_errors: usize,
}

/// One request: its own session, supersedes pairs `newer -> older` in the given order.
async fn request(
    store: &Store,
    limit: Duration,
    session: &str,
    pairs: &[[String; 2]],
) -> Result<(), String> {
    match tokio::time::timeout(
        limit,
        store.record_consumption(session, "2026-01-01", &[], &[], pairs, None),
    )
    .await
    {
        Err(_) => Err(format!("timeout after {limit:?}")),
        Ok(Err(e)) => Err(format!("{e:#}")),
        Ok(Ok(_)) => Ok(()),
    }
}

/// All-or-nothing: Ok → the session node and every pair's edge exist; Err → none of them.
async fn all_or_nothing(
    db: &Client,
    session: &str,
    newer: &str,
    expected_edges: i64,
    outcome: &Result<(), String>,
) -> bool {
    let (nodes, _) = session_rows(db, session).await;
    let edges = supersedes_edges_from(db, newer).await;
    match outcome {
        Ok(()) => nodes == 1 && edges == expected_edges,
        Err(_) => nodes == 0 && edges == 0,
    }
}

/// Two requests with different sessions whose supersedes pairs touch the same two older notes in
/// opposite order (X: o0..o7; Y: o7..o0), run together.
async fn run_pairs(store: &Store, db: &Client, limit: Duration, rounds: usize) -> Tally {
    let mut tally = Tally::default();
    for _ in 0..rounds {
        let (x, y) = (unique_path("x"), unique_path("y"));
        let olders: Vec<String> = (0..OLDER_NOTES).map(|_| unique_path("old")).collect();
        for (i, p) in [&x, &y].into_iter().chain(olders.iter()).enumerate() {
            seed_doc(store, p, "unknown").await;
            seed_claim(store, p, i as u64).await;
        }
        let (s1, s2) = (unique_id("tx-s1"), unique_id("tx-s2"));
        let pairs1: Vec<[String; 2]> = olders.iter().map(|o| pair(&x, o)).collect();
        let pairs2: Vec<[String; 2]> = olders.iter().rev().map(|o| pair(&y, o)).collect();
        let (r1, r2) = tokio::join!(
            request(store, limit, &s1, &pairs1),
            request(store, limit, &s2, &pairs2)
        );
        let expected = i64::try_from(OLDER_NOTES).unwrap();
        let ok1 = all_or_nothing(db, &s1, &x, expected, &r1).await;
        let ok2 = all_or_nothing(db, &s2, &y, expected, &r2).await;
        tally.half_written += usize::from(!ok1) + usize::from(!ok2);
        for r in [r1, r2] {
            if let Err(msg) = r {
                if msg.contains("get postgres connection from pool") || msg.starts_with("timeout") {
                    tally.pool_or_timeout.push(msg);
                } else {
                    tally.other_errors += 1;
                }
            }
        }
    }
    tally
}

#[tokio::test]
async fn concurrent_opposite_order_requests_are_all_or_nothing() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let tally = run_pairs(&store, &db, Duration::from_secs(30), ITERATIONS).await;
    eprintln!(
        "half_written {}/{ITERATIONS} (requests erroring: {})",
        tally.half_written, tally.other_errors
    );
    assert_eq!(tally.half_written, 0, "half_written n/{ITERATIONS}");
}

async fn pool_of(size: usize) {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open_with_pool_size(&dsn, 1024, size)
        .await
        .expect("open store");
    let db = connect(&dsn).await;
    let tally = run_pairs(&store, &db, Duration::from_secs(10), ITERATIONS).await;
    eprintln!(
        "pool {size}: pool_or_timeout {}/{} first={:?}; half_written {}/{ITERATIONS}",
        tally.pool_or_timeout.len(),
        ITERATIONS * 2,
        tally.pool_or_timeout.first(),
        tally.half_written
    );
    assert!(
        tally.pool_or_timeout.is_empty(),
        "pool of {size} must not starve a request: {:?}",
        tally.pool_or_timeout.first()
    );
    assert_eq!(tally.half_written, 0, "pool {size}: half_written");
}

#[tokio::test]
async fn pool_of_one_does_not_self_deadlock() {
    pool_of(1).await;
}

#[tokio::test]
async fn pool_of_two_does_not_self_deadlock() {
    pool_of(2).await;
}

/// `/consumption` handler order (serve/http.rs handle_consumption): the owner-authored
/// supersedes targets are looked up and refused with one `owner_supersede_refused` event, then the
/// kept pairs go to `record_consumption`. `serve::owner` is crate-private, so the event is written
/// here with the same payload `owner::record_refusal` builds. The event is a separate fact from
/// the verdict write: it stays once, while the failed verdict leaves no rows.
#[tokio::test]
async fn refusal_event_stands_once_while_a_failed_verdict_writes_nothing() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    install_boom(&db).await;
    let (newer, owned, plain, ok) = (
        unique_path("n"),
        unique_path("owned"),
        unique_path("plain"),
        unique_path("ok"),
    );
    seed_doc(&store, &newer, "agent:x").await;
    seed_doc(&store, &owned, "owner").await;
    seed_doc(&store, &plain, "unknown").await;
    seed_doc(&store, &ok, "unknown").await;
    seed_doc(&store, BOOM_PATH, "unknown").await;

    let session = unique_id("tx-s");
    let pairs = [pair(&newer, &owned), pair(&newer, &plain)];
    let old_paths: Vec<String> = pairs.iter().map(|[_, old]| old.clone()).collect();
    let refused = store
        .owner_authored(&old_paths)
        .await
        .expect("owner_authored");
    assert_eq!(refused, vec![owned.clone()], "the owner's note is refused");
    store
        .log_event(&serde_json::json!({
            "component": "drudge.owner",
            "event": "owner_supersede_refused",
            "status": "warn",
            "door": "consumption",
            "targets": refused,
        }))
        .await
        .expect("log refusal");
    let kept: Vec<[String; 2]> = pairs
        .iter()
        .filter(|[_, old]| !refused.contains(old))
        .cloned()
        .collect();
    let result = store
        .record_consumption(
            &session,
            "2026-01-01",
            &[ok, BOOM_PATH.to_string()],
            &[],
            &kept,
            None,
        )
        .await;
    assert!(result.is_err(), "boom edge must fail the verdict write");

    let events: i64 = db
        .query_one(
            "SELECT count(*) FROM event_log
             WHERE event_name = 'owner_supersede_refused' AND (body::text || attributes::text) LIKE $1;",
            &[&format!("%{owned}%")],
        )
        .await
        .expect("count events")
        .get(0);
    let (nodes, edges) = session_rows(&db, &session).await;
    let supersedes = supersedes_edges_from(&db, &newer).await;
    eprintln!(
        "refusal+failure: events {events}, session nodes {nodes}, session edges {edges}, supersedes edges {supersedes}"
    );
    assert_eq!(events, 1, "exactly one refusal event for the request");
    assert_eq!(
        (nodes, edges, supersedes),
        (0, 0, 0),
        "no verdict rows from the failed request"
    );
}
