//! Consumption write-back: `used`/`contested` edges must survive re-ingest, and `session` nodes
//! must survive GC — a note the scorer says was consumed has to stay consumed.
//!
//! Needs a Postgres instance reachable via `BORING_TEST_DATABASE_URL`; without it the tests skip
//! (the same guard `store_integration.rs` / `code_index_integration.rs` use).
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::time::{SystemTime, UNIX_EPOCH};

use drudge::frontmatter::FrontMatter;
use drudge::store::Store;
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

fn unique_id(prefix: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}-{ts}")
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

async fn node_exists(db: &Client, id: &str) -> bool {
    let n: i64 = db
        .query_one("SELECT count(*) FROM node WHERE id = $1;", &[&id])
        .await
        .expect("count nodes")
        .get(0);
    n == 1
}

/// Seed two docs and one session's consumption of them (one `contested`). Returns the paths.
async fn seed_consumption(store: &Store) -> (String, String, String) {
    let path_a = unique_path("consumption-a");
    let path_b = unique_path("consumption-b");
    let session = unique_id("consumption-s");
    store
        .upsert_document(&dummy_frontmatter(&path_a), "sha-v1", SystemTime::now())
        .await
        .expect("upsert doc a");
    store
        .upsert_document(&dummy_frontmatter(&path_b), "sha-v1", SystemTime::now())
        .await
        .expect("upsert doc b");
    let report = store
        .record_consumption(
            &session,
            "2026-09-11T05:12:00+00:00",
            &[
                path_a.clone(),
                path_b.clone(),
                "/vault/wiki/never-ingested.md".to_owned(),
            ],
            std::slice::from_ref(&path_a),
            &[],
        )
        .await
        .expect("record consumption");
    assert_eq!(report.used, 2, "two known paths wrote two edges");
    assert_eq!(report.contested, 1);
    assert_eq!(
        report.unknown, 1,
        "a path with no document row is counted, not an error"
    );
    (path_a, path_b, session)
}

/// Two `used` edges + one `contested` survive a re-ingest of one doc (same path, changed content,
/// including the semantic-edge rebuild step re-ingest runs), and the counts read back through the
/// same query `/search` uses. Also pins idempotence: a repeat call updates the label and adds no
/// duplicate edges. Prune is the intended removal: a pruned note's edges go with it.
#[tokio::test]
async fn consumption_edges_survive_reingest_and_repeat_calls() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let (path_a, path_b, session) = seed_consumption(&store).await;
    let session_node = format!("session:{session}");

    // Re-ingest doc a: same path, changed content. This is exactly what ingest does on update —
    // upsert the document row, rebuild the doc's own semantic edges. None of it may touch
    // consumption edges whose dst is this doc and whose src is a session node.
    store
        .upsert_document(&dummy_frontmatter(&path_a), "sha-v2", SystemTime::now())
        .await
        .expect("re-ingest doc a");
    store
        .clear_semantic_edges(&path_a)
        .await
        .expect("semantic edge rebuild step");

    // Counts read back through the same query /search uses.
    let counts = store
        .consumption_counts(&[path_a.clone(), path_b.clone()])
        .await
        .expect("consumption counts");
    let a = counts.get(&path_a).copied().unwrap_or_default();
    assert_eq!(a.used, 1, "doc a keeps its used edge after re-ingest");
    assert_eq!(
        a.contested, 1,
        "doc a keeps its contested edge after re-ingest"
    );
    let b = counts.get(&path_b).copied().unwrap_or_default();
    assert_eq!(b.used, 1);
    assert_eq!(b.contested, 0, "doc b was never contested");

    // Idempotence: a second call for the same session adds nothing and updates the label.
    store
        .record_consumption(
            &session,
            "2026-09-11T07:00:00+00:00",
            std::slice::from_ref(&path_a),
            &[],
            &[],
        )
        .await
        .expect("repeat consumption");
    let label: String = db
        .query_one("SELECT label FROM node WHERE id = $1;", &[&session_node])
        .await
        .expect("session node label")
        .get(0);
    assert_eq!(
        label, "2026-09-11T07:00:00+00:00",
        "label follows the latest call"
    );
    let counts = store
        .consumption_counts(std::slice::from_ref(&path_a))
        .await
        .expect("counts again");
    assert_eq!(
        counts.get(&path_a).copied().unwrap_or_default().used,
        1,
        "a repeat call must not duplicate edges"
    );

    // Prune takes a note's consumption edges with it — that is the intended semantics, so the
    // counts for a deleted doc read as absent, never stale.
    store.delete_document(&path_b).await.expect("prune doc b");
    let counts = store
        .consumption_counts(&[path_a.clone(), path_b.clone()])
        .await
        .expect("counts after prune");
    assert!(
        !counts.contains_key(&path_b),
        "a pruned note's consumption edges go with it"
    );
    assert_eq!(counts.get(&path_a).copied().unwrap_or_default().used, 1);

    // delete_document removed edges by dst but not the session node itself — drop it by hand.
    store.delete_document(&path_a).await.expect("prune doc a");
    db.execute("DELETE FROM node WHERE id = $1;", &[&session_node])
        .await
        .expect("cleanup session node");
}

/// GC only sweeps orphan tool/concept nodes — session nodes are out of scope, even edge-less
/// ones (a session that consumed nothing yet must not be swept before its first report).
#[tokio::test]
async fn gc_keeps_session_nodes() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;

    let (path_a, path_b, session) = seed_consumption(&store).await;
    let idle_session = unique_id("consumption-idle");
    let idle_report = store
        .record_consumption(&idle_session, "2026-09-11T06:00:00+00:00", &[], &[], &[])
        .await
        .expect("record edge-less session");
    assert_eq!(
        idle_report.used + idle_report.contested + idle_report.unknown,
        0
    );

    store.gc_orphans().await.expect("gc orphans");
    assert!(
        node_exists(&db, &format!("session:{session}")).await,
        "GC must not delete session nodes"
    );
    assert!(
        node_exists(&db, &format!("session:{idle_session}")).await,
        "GC must not delete edge-less session nodes"
    );

    store.delete_document(&path_a).await.expect("cleanup doc a");
    store.delete_document(&path_b).await.expect("cleanup doc b");
    db.execute(
        "DELETE FROM node WHERE id = ANY($1);",
        &[&vec![
            format!("session:{session}"),
            format!("session:{idle_session}"),
        ]],
    )
    .await
    .expect("cleanup session nodes");
}

/// `supersedes` pairs write `(doc:<newer>) -[supersedes]-> (doc:<older>)` edges, and the
/// `superseded_by` read path `/search` uses reports the newer doc's path back. An equal-paths
/// pair replaces nothing: skipped, counted as unknown, never written.
#[tokio::test]
async fn supersedes_edges_are_written_and_read_back() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let newer = unique_path("supersedes-new");
    let older = unique_path("supersedes-old");
    let session = unique_id("supersedes-s");
    store
        .upsert_document(&dummy_frontmatter(&newer), "sha-v1", SystemTime::now())
        .await
        .expect("upsert newer doc");
    store
        .upsert_document(&dummy_frontmatter(&older), "sha-v1", SystemTime::now())
        .await
        .expect("upsert older doc");

    let report = store
        .record_consumption(
            &session,
            "2026-09-11T05:12:00+00:00",
            &[],
            &[],
            &[
                [newer.clone(), older.clone()],
                [newer.clone(), newer.clone()],
                [newer.clone(), "/vault/wiki/never-ingested.md".to_owned()],
            ],
        )
        .await
        .expect("record supersedes pairs");
    assert_eq!(
        report.supersedes, 1,
        "one known distinct pair wrote one edge"
    );
    assert_eq!(
        report.unknown, 2,
        "the equal-paths pair and the unknown path are counted, not written"
    );

    // Read back through the same query path /search uses.
    let by = store
        .superseded_by(std::slice::from_ref(&older))
        .await
        .expect("superseded by");
    assert_eq!(
        by.get(&older).map(Vec::as_slice),
        Some(std::slice::from_ref(&newer)),
        "the newer doc is reported as the replacement for the older one"
    );

    store
        .delete_document(&newer)
        .await
        .expect("cleanup newer doc");
    store
        .delete_document(&older)
        .await
        .expect("cleanup older doc");
    db.execute(
        "DELETE FROM node WHERE id = $1;",
        &[&format!("session:{session}")],
    )
    .await
    .expect("cleanup session node");
}

/// `reused_recently` counts only edges from sessions whose `observed_at` label is inside the
/// window: two `used` edges from sessions labelled today count, one from a session labelled
/// 30 days ago does not.
#[tokio::test]
async fn reused_recently_counts_only_sessions_inside_the_window() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let path = unique_path("reused-doc");
    let today_session_a = unique_id("reused-today-a");
    let today_session_b = unique_id("reused-today-b");
    let old_session = unique_id("reused-old");
    let today = chrono::Utc::now().to_rfc3339();
    let thirty_days_ago = (chrono::Utc::now() - chrono::Duration::days(30)).to_rfc3339();
    store
        .upsert_document(&dummy_frontmatter(&path), "sha-v1", SystemTime::now())
        .await
        .expect("upsert reused doc");

    for session in [&today_session_a, &today_session_b] {
        store
            .record_consumption(session, &today, std::slice::from_ref(&path), &[], &[])
            .await
            .expect("record today's consumption");
    }
    store
        .record_consumption(
            &old_session,
            &thirty_days_ago,
            std::slice::from_ref(&path),
            &[],
            &[],
        )
        .await
        .expect("record old consumption");

    let rows = store.reused_recently(7, 5).await.expect("reused recently");
    let row = rows
        .iter()
        .find(|(doc, _, _)| doc.source_path == path)
        .expect("the reused doc is in the window");
    assert_eq!(row.1, 2, "only the two in-window used edges count");
    assert_eq!(row.2, 0);

    store
        .delete_document(&path)
        .await
        .expect("cleanup reused doc");
    db.execute(
        "DELETE FROM node WHERE id = ANY($1);",
        &[&vec![
            format!("session:{today_session_a}"),
            format!("session:{today_session_b}"),
            format!("session:{old_session}"),
        ]],
    )
    .await
    .expect("cleanup session nodes");
}
