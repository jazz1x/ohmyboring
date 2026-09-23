//! Consumption write-back: `used`/`contested` edges must survive re-ingest, and `session` nodes
//! must survive GC — a note the scorer says was consumed has to stay consumed. Handover is the
//! door before consumption: `handed` edges record what a session was shown, and a bare
//! `{session_id, verdict}` applies the verdict to exactly those notes.
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
            None,
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
            None,
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

/// `record_consumption` writes `judge` onto every `used`/`contested` edge it creates, and the
/// first judge to name an edge stays on it: a repeat call with another judge (or none) hits
/// ON CONFLICT DO NOTHING, never an overwrite. A call with no judge writes NULL — the edge
/// exists but names nobody, the same state every pre-column edge is in.
#[tokio::test]
async fn consumption_judge_lands_on_edges_and_the_first_judge_stays() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let path_a = unique_path("judge-a");
    let path_b = unique_path("judge-b");
    let session = unique_id("judge-s");
    for path in [&path_a, &path_b] {
        store
            .upsert_document(&dummy_frontmatter(path), "sha-v1", SystemTime::now())
            .await
            .expect("upsert doc");
    }

    store
        .record_consumption(
            &session,
            "2026-09-23T05:12:00+00:00",
            std::slice::from_ref(&path_a),
            std::slice::from_ref(&path_b),
            &[],
            Some("agent:r2-check"),
        )
        .await
        .expect("record judged consumption");
    let session_node = format!("session:{session}");
    let doc_a = format!("doc:{path_a}");
    let doc_b = format!("doc:{path_b}");
    assert_eq!(
        edge_judge(&db, &session_node, &doc_a, "used").await,
        Some("agent:r2-check".to_owned()),
        "the used edge carries the judge"
    );
    assert_eq!(
        edge_judge(&db, &session_node, &doc_b, "contested").await,
        Some("agent:r2-check".to_owned()),
        "the contested edge carries the judge"
    );

    // A later call with a different judge must not overwrite the first one.
    store
        .record_consumption(
            &session,
            "2026-09-23T06:00:00+00:00",
            std::slice::from_ref(&path_a),
            &[],
            &[],
            Some("owner"),
        )
        .await
        .expect("repeat with another judge");
    assert_eq!(
        edge_judge(&db, &session_node, &doc_a, "used").await,
        Some("agent:r2-check".to_owned()),
        "ON CONFLICT DO NOTHING keeps the judge that named the edge first"
    );

    // A judge-less session's edges name nobody — NULL, like every pre-column edge.
    let bare_session = unique_id("judge-bare");
    store
        .record_consumption(
            &bare_session,
            "2026-09-23T05:30:00+00:00",
            std::slice::from_ref(&path_a),
            &[],
            &[],
            None,
        )
        .await
        .expect("record judge-less consumption");
    assert_eq!(
        edge_judge(&db, &format!("session:{bare_session}"), &doc_a, "used").await,
        None,
        "no judge in the request → NULL on the edge"
    );

    store.delete_document(&path_a).await.expect("cleanup doc a");
    store.delete_document(&path_b).await.expect("cleanup doc b");
    for s in [&session, &bare_session] {
        db.execute(
            "DELETE FROM node WHERE id = $1;",
            &[&format!("session:{s}")],
        )
        .await
        .expect("cleanup session node");
    }
}

async fn edge_judge(db: &Client, src: &str, dst: &str, kind: &str) -> Option<String> {
    db.query_opt(
        "SELECT judge FROM edge WHERE src = $1 AND dst = $2 AND kind = $3;",
        &[&src, &dst, &kind],
    )
    .await
    .expect("query edge judge")
    .and_then(|row| row.get::<_, Option<String>>(0))
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
        .record_consumption(
            &idle_session,
            "2026-09-11T06:00:00+00:00",
            &[],
            &[],
            &[],
            None,
        )
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
            None,
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
            .record_consumption(session, &today, std::slice::from_ref(&path), &[], &[], None)
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
            None,
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

/// A handover writes one `handed` edge per known path — counted in the report, never in
/// `consumption_counts`: being handed is not being consumed, so the verdict-free docs read as
/// having no consumption at all. Unknown paths are counted, not written; a repeat call adds
/// nothing.
#[tokio::test]
async fn handover_edges_are_written_and_never_counted_as_consumption() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let path_a = unique_path("handover-a");
    let path_b = unique_path("handover-b");
    let session = unique_id("handover-s");
    let session_node = format!("session:{session}");
    for path in [&path_a, &path_b] {
        store
            .upsert_document(&dummy_frontmatter(path), "sha-v1", SystemTime::now())
            .await
            .expect("upsert doc");
    }

    let report = store
        .record_handover(
            &session,
            "2026-09-21T05:00:00+00:00",
            &[
                path_a.clone(),
                path_b.clone(),
                "/vault/wiki/never-ingested.md".to_owned(),
            ],
        )
        .await
        .expect("record handover");
    assert_eq!(report.handed, 2, "two known paths wrote two handed edges");
    assert_eq!(
        report.unknown, 1,
        "a path with no document row is counted, not an error"
    );

    let handed_rows: i64 = db
        .query_one(
            "SELECT count(*) FROM edge WHERE src = $1 AND kind = 'handed';",
            &[&session_node],
        )
        .await
        .expect("count handed edges")
        .get(0);
    assert_eq!(handed_rows, 2);

    // The /search-side aggregation must not see handed: no verdict has happened yet.
    let counts = store
        .consumption_counts(&[path_a.clone(), path_b.clone()])
        .await
        .expect("consumption counts");
    assert!(
        !counts.contains_key(&path_a) && !counts.contains_key(&path_b),
        "handed edges are not consumption — the counts stay empty"
    );

    // Read back through the same query the verdict-only /consumption path uses.
    let handed = store.handed_paths(&session).await.expect("handed paths");
    let mut expected = vec![path_a.clone(), path_b.clone()];
    expected.sort();
    assert_eq!(handed, expected, "distinct, in path order");

    // Idempotence: a repeat call updates the label and adds no duplicate edges.
    store
        .record_handover(
            &session,
            "2026-09-21T06:00:00+00:00",
            std::slice::from_ref(&path_a),
        )
        .await
        .expect("repeat handover");
    let label: String = db
        .query_one("SELECT label FROM node WHERE id = $1;", &[&session_node])
        .await
        .expect("session node label")
        .get(0);
    assert_eq!(label, "2026-09-21T06:00:00+00:00");
    let again: i64 = db
        .query_one(
            "SELECT count(*) FROM edge WHERE src = $1 AND kind = 'handed';",
            &[&session_node],
        )
        .await
        .expect("count handed edges again")
        .get(0);
    assert_eq!(again, 2, "a repeat handover must not duplicate edges");

    store.delete_document(&path_a).await.expect("cleanup doc a");
    store.delete_document(&path_b).await.expect("cleanup doc b");
    db.execute("DELETE FROM node WHERE id = $1;", &[&session_node])
        .await
        .expect("cleanup session node");
}

/// End to end through the real HTTP surface: `/handover` records what a session was shown, and a
/// verdict-only `/consumption` (`{session_id, verdict}` — no path lists) applies the verdict to
/// exactly the handed docs and nothing else. An unknown session is a normal zero response, not an
/// error. The verdict branch ignoring `handed_paths` (empty lists) fails this test: used stays 0.
#[tokio::test]
async fn verdict_only_consumption_marks_exactly_the_handed_docs() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let path_a = unique_path("verdict-a");
    let path_b = unique_path("verdict-b");
    let path_c = unique_path("verdict-c");
    let session = unique_id("verdict-s");
    let unknown_session = unique_id("verdict-unknown-s");
    for path in [&path_a, &path_b, &path_c] {
        store
            .upsert_document(&dummy_frontmatter(path), "sha-v1", SystemTime::now())
            .await
            .expect("upsert doc");
    }

    let port = free_port();
    let _server = spawn_server(&dsn, port);
    let base = format!("http://127.0.0.1:{port}");
    let client = reqwest::Client::new();
    wait_for_health(&client, &base).await;

    // Hand two of the three docs; the third is never handed.
    let resp = client
        .post(format!("{base}/handover"))
        .json(&serde_json::json!({
            "session_id": session,
            "observed_at": "2026-09-21T05:00:00+00:00",
            "paths": [path_a, path_b, "/vault/wiki/never-ingested.md"],
        }))
        .send()
        .await
        .expect("post /handover");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    let body: serde_json::Value = resp.json().await.expect("handover response");
    assert_eq!(body["handed"], 2);
    assert_eq!(body["unknown"], 1);

    // The whole feedback signal: verdict=used, no paths. The handler resolves what was handed.
    // Padded on purpose: the validator and the handler once trimmed differently, and " used "
    // came out as contested. The assertions below (used edges, zero contested) are the check.
    let resp = client
        .post(format!("{base}/consumption"))
        .json(&serde_json::json!({
            "session_id": session,
            "observed_at": "2026-09-21T06:00:00+00:00",
            "verdict": " used ",
        }))
        .send()
        .await
        .expect("post /consumption");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    let body: serde_json::Value = resp.json().await.expect("consumption response");
    assert_eq!(
        body["used"], 2,
        "verdict=used applies to exactly the two handed docs"
    );
    assert_eq!(body["contested"], 0);
    assert_eq!(body["unknown"], 0);

    // An unknown session handed nothing — a normal zero response, not an error.
    let resp = client
        .post(format!("{base}/consumption"))
        .json(&serde_json::json!({
            "session_id": unknown_session,
            "observed_at": "2026-09-21T06:00:00+00:00",
            "verdict": "used",
        }))
        .send()
        .await
        .expect("post /consumption for unknown session");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    let body: serde_json::Value = resp.json().await.expect("consumption response");
    assert_eq!(body["used"], 0);
    assert_eq!(body["unknown"], 0);

    // Exactly the handed docs carry the verdict; the un-handed doc stays untouched.
    let counts = store
        .consumption_counts(&[path_a.clone(), path_b.clone(), path_c.clone()])
        .await
        .expect("consumption counts");
    assert_eq!(counts.get(&path_a).copied().unwrap_or_default().used, 1);
    assert_eq!(counts.get(&path_b).copied().unwrap_or_default().used, 1);
    assert!(
        !counts.contains_key(&path_c),
        "the doc that was never handed gets no verdict edge"
    );

    for path in [&path_a, &path_b, &path_c] {
        store.delete_document(path).await.expect("cleanup doc");
    }
    db.execute(
        "DELETE FROM node WHERE id = ANY($1);",
        &[&vec![
            format!("session:{session}"),
            format!("session:{unknown_session}"),
        ]],
    )
    .await
    .expect("cleanup session nodes");
}

/// Verdicts move the next ranking: two identical notes, one `contested` and one `used` — the
/// used one must land above the contested one even though, before any edge existed, it ranked
/// below by almost two rank steps. Control pair: two edge-less notes keep exactly the relative
/// order they had before the edges were written. The query matches nothing full-text, so only
/// the vector list carries the docs, with one pinned distance per doc — ranking is
/// deterministic before and after the verdicts.
#[tokio::test]
async fn consumption_verdicts_reorder_ranking() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let mut cfg = drudge::config::BoringConfig::default();
    cfg.llm.base_url = spawn_stub_embedder().await;
    let llm = drudge::llm::Llm::from_config(&cfg);

    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let project = format!("feedback-{ts}");
    let path_a = unique_path("feedback-contested");
    let path_m = unique_path("feedback-control-mid");
    let path_b = unique_path("feedback-used");
    let path_c = unique_path("feedback-control-last");
    let content = "feedback ranking fixture note — no query term lives here";
    // The stub embedder returns [1, 0, …]; each doc's pinned vector picks its rank by cosine
    // distance from it: contested 0.0 < mid ≈ 0.29 < used 1.0 < last ≈ 1.71.
    let norm2 = |x: f32, y: f32| {
        let n = (x * x + y * y).sqrt();
        let mut e = vec![0.0_f32; 1024];
        e[0] = x / n;
        e[1] = y / n;
        e
    };
    let mut used_vec = vec![0.0_f32; 1024];
    used_vec[1] = 1.0;
    for (path, embedding) in [
        (&path_a, stub_vector()),
        (&path_m, norm2(1.0, 1.0)),
        (&path_b, used_vec),
        (&path_c, norm2(-1.0, 1.0)),
    ] {
        seed_rank_doc(&store, path, &project, content, embedding).await;
    }

    let base =
        drudge::retrieve::retrieve(&store, &llm, "fiddlesticks", 10, &[], Some(&project), None)
            .await
            .expect("baseline retrieve");
    let base_order: Vec<&str> = base.iter().map(|h| h.source_path.as_str()).collect();
    assert_eq!(
        base_order,
        vec![
            path_a.as_str(),
            path_m.as_str(),
            path_b.as_str(),
            path_c.as_str()
        ],
        "distinct pinned distances fix the base order before any verdict edge exists"
    );

    let session = unique_id("feedback-s");
    store
        .record_consumption(
            &session,
            "2026-09-21T06:00:00+00:00",
            std::slice::from_ref(&path_b),
            std::slice::from_ref(&path_a),
            &[],
            None,
        )
        .await
        .expect("record verdicts");

    let ranked =
        drudge::retrieve::retrieve(&store, &llm, "fiddlesticks", 10, &[], Some(&project), None)
            .await
            .expect("feedback retrieve");
    let pos = |hits: &[drudge::store::Hit], p: &str| {
        hits.iter()
            .position(|h| h.source_path == p)
            .expect("every seeded doc is still retrieved")
    };
    assert_eq!(ranked.len(), 4, "feedback must not drop hits");
    assert!(
        pos(&ranked, &path_b) < pos(&ranked, &path_a),
        "one 👍 lifts the used doc above the contested one (base had it ~2 rank steps below)"
    );
    // Control pair — no verdict edges: same relative order as before the edges, rank for rank.
    assert_eq!(
        pos(&ranked, &path_m) < pos(&ranked, &path_c),
        pos(&base, &path_m) < pos(&base, &path_c),
        "docs without verdict edges keep the order they had before the edges existed"
    );

    for path in [&path_a, &path_m, &path_b, &path_c] {
        store.delete_document(path).await.expect("cleanup doc");
    }
    db.execute(
        "DELETE FROM node WHERE id = $1;",
        &[&format!("session:{session}")],
    )
    .await
    .expect("cleanup session node");
}

/// Spawn `drudge serve` against the disposable test DB on an ephemeral loopback port. No vault
/// (nothing to re-ingest), no brief scheduler (99 is out of range); killed by the guard on drop.
fn spawn_server(dsn: &str, port: u16) -> ServerGuard {
    let child = std::process::Command::new(env!("CARGO_BIN_EXE_drudge"))
        .arg("serve")
        .env("BORING_VECTOR", "on")
        .env("PG_DSN", dsn)
        .env("BORING_HTTP_ADDR", format!("127.0.0.1:{port}"))
        .env("BORING_BRIEF_HOUR", "99")
        .env_remove("BORING_VAULT_DIR")
        .env_remove("BORING_LLM_BASE_URL")
        .env_remove("BORING_LLM_MODEL")
        .spawn()
        .expect("spawn drudge serve");
    ServerGuard(child)
}

/// Poll `/health` until the server answers 2xx (it retries its own DB connect at startup).
async fn wait_for_health(client: &reqwest::Client, base: &str) {
    let mut ready = false;
    for _ in 0..150 {
        if let Ok(resp) = client.get(format!("{base}/health")).send().await
            && resp.status().is_success()
        {
            ready = true;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    assert!(ready, "drudge serve did not come up on {base}");
}

/// Kills the spawned `drudge serve` child when the test ends — on success or mid-panic.
struct ServerGuard(std::process::Child);
impl Drop for ServerGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// An ephemeral port for the spawned server — freed (with the usual tiny race) before bind.
fn free_port() -> u16 {
    let probe = std::net::TcpListener::bind("127.0.0.1:0").expect("bind port probe");
    probe.local_addr().expect("probe local addr").port()
}

/// The stub embedder's fixed vector: [1, 0, 0, …]. The query embeds to this, and each seeded
/// doc's pinned vector picks its rank by cosine distance from it.
fn stub_vector() -> Vec<f32> {
    let mut v = vec![0.0_f32; 1024];
    v[0] = 1.0;
    v
}

/// One OpenAI-compatible `/embeddings` stub: every input embeds to `stub_vector()`, so
/// `retrieve`'s query embedding is pinned without a model server in the loop.
async fn spawn_stub_embedder() -> String {
    async fn embeddings() -> axum::Json<serde_json::Value> {
        axum::Json(serde_json::json!({ "data": [{ "embedding": stub_vector() }] }))
    }
    let app = axum::Router::new().route("/embeddings", axum::routing::post(embeddings));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind stub");
    let url = format!("http://{}", listener.local_addr().expect("stub addr"));
    let _server = tokio::spawn(async move {
        axum::serve(listener, app).await.expect("stub serve");
    });
    url
}

/// One document row + its single chunk, pinned into `project`.
async fn seed_rank_doc(
    store: &Store,
    path: &str,
    project: &str,
    content: &str,
    embedding: Vec<f32>,
) {
    let front = FrontMatter {
        origin: "personal".to_string(),
        project: project.to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("feedback fixture".to_string()),
        tags: vec!["test".to_string()],
        ..Default::default()
    };
    store
        .upsert_document(&front, "sha-v1", SystemTime::now())
        .await
        .expect("upsert doc");
    store
        .upsert_chunk(&drudge::store::Doc {
            id: format!("{path}#0"),
            content: content.to_owned(),
            embedding,
            front,
            chunk_idx: 0,
        })
        .await
        .expect("upsert chunk");
}

/// POST /remember and hand back the parsed body — the door answers 200 or the test fails.
async fn post_remember(
    client: &reqwest::Client,
    base: &str,
    payload: serde_json::Value,
) -> serde_json::Value {
    let resp = client
        .post(format!("{base}/remember"))
        .json(&payload)
        .send()
        .await
        .expect("post /remember");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    resp.json().await.expect("remember response")
}

/// POST /search and hand back the hits array — the door answers 200 or the test fails.
async fn post_search(
    client: &reqwest::Client,
    base: &str,
    payload: serde_json::Value,
) -> Vec<serde_json::Value> {
    let resp = client
        .post(format!("{base}/search"))
        .json(&payload)
        .send()
        .await
        .expect("post /search");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    resp.json::<serde_json::Value>()
        .await
        .expect("search response")["hits"]
        .as_array()
        .expect("hits")
        .clone()
}

/// End to end through the real HTTP surface: `POST /remember` writes note A; note B names A in
/// `supersedes` and the response reports the one edge; `/search` reads A back with
/// `superseded_by` pointing at B. A `supersedes` path that names no document row is counted in
/// `unknown`, not an error. The stub embedder is a hashing vectorizer, so a query sharing
/// tokens with A ranks A first and the 0.07 dedup cap never trips on lexically disjoint notes.
#[tokio::test]
async fn remember_http_supersedes_edges_sink_the_old_note_in_search() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let vault = tempfile::tempdir().expect("temp vault");
    std::fs::create_dir_all(vault.path().join("wiki")).expect("wiki dir");
    let llm_base = format!("http://127.0.0.1:{}/v1", spawn_hashing_embedder().await);

    let port = free_port();
    let _server = spawn_server_with_vault(&dsn, port, vault.path(), &llm_base);
    let base = format!("http://127.0.0.1:{port}");
    let client = reqwest::Client::new();
    wait_for_health(&client, &base).await;

    // Note A — the one that gets corrected.
    let body = post_remember(
        &client,
        &base,
        serde_json::json!({
            "title": "alpha fixture lever jam",
            "body": "The alpha fixture jams when the lever is cold; warm the lever first.",
        }),
    )
    .await;
    let path_a = body["source_path"]
        .as_str()
        .expect("a source_path")
        .to_owned();
    assert!(
        body["wiki_id"]
            .as_str()
            .expect("a wiki_id")
            .starts_with("wiki-"),
        "wiki_id is allocated: {:?}",
        body["wiki_id"]
    );
    assert!(body["duplicate"].is_null(), "a fresh note is no duplicate");
    assert_eq!(body["supersedes"], 0);
    assert_eq!(body["unknown"], 0);

    // Note B corrects A: one supersedes edge, zero unknown.
    let body = post_remember(
        &client,
        &base,
        serde_json::json!({
            "title": "beta widget pedal squeak",
            "body": "The beta widget squeaks when the pedal is humid; park it in the shade.",
            "supersedes": [path_a],
        }),
    )
    .await;
    let path_b = body["source_path"]
        .as_str()
        .expect("b source_path")
        .to_owned();
    assert!(body["duplicate"].is_null());
    assert_eq!(
        body["supersedes"], 1,
        "the note that corrects A reports one supersedes edge"
    );
    assert_eq!(body["unknown"], 0);

    // A supersedes path naming no document row is counted, not an error.
    let body = post_remember(
        &client,
        &base,
        serde_json::json!({
            "title": "gamma panel toggle flicker",
            "body": "The gamma panel flickers when the toggle is wet; dry the toggle first.",
            "supersedes": ["/vault/wiki/never-ingested.md"],
        }),
    )
    .await;
    let path_c = body["source_path"]
        .as_str()
        .expect("c source_path")
        .to_owned();
    assert_eq!(body["supersedes"], 0);
    assert_eq!(
        body["unknown"], 1,
        "a supersedes path with no document row is counted, not an error"
    );

    // The next recall sinks the old note: A comes back with superseded_by pointing at B.
    let hits = post_search(
        &client,
        &base,
        serde_json::json!({"query": "alpha fixture lever", "max_results": 5}),
    )
    .await;
    let hit_a = hits
        .iter()
        .find(|h| h["source_path"] == path_a)
        .expect("note A is a hit for its own tokens");
    let superseded_by = hit_a["superseded_by"]
        .as_array()
        .expect("A carries a superseded_by list now");
    assert_eq!(
        superseded_by
            .iter()
            .filter_map(|v| v.as_str())
            .collect::<Vec<_>>(),
        vec![path_b.as_str()],
        "recall reports B as A's replacement"
    );

    for path in [&path_a, &path_b, &path_c] {
        store.delete_document(path).await.expect("cleanup doc");
    }
}

/// Spawn `drudge serve` with a vault and a stub embedder — the correction door needs both: the
/// wiki note lands in the vault, and vector mode on means ingest/dedup/project all embed.
fn spawn_server_with_vault(
    dsn: &str,
    port: u16,
    vault: &std::path::Path,
    llm_base: &str,
) -> ServerGuard {
    let child = std::process::Command::new(env!("CARGO_BIN_EXE_drudge"))
        .arg("serve")
        .env("BORING_VECTOR", "on")
        .env("PG_DSN", dsn)
        .env("BORING_HTTP_ADDR", format!("127.0.0.1:{port}"))
        .env("BORING_BRIEF_HOUR", "99")
        .env("BORING_VAULT_DIR", vault)
        .env("BORING_LLM_BASE_URL", llm_base)
        .env_remove("BORING_LLM_MODEL")
        .spawn()
        .expect("spawn drudge serve");
    ServerGuard(child)
}

/// A stub OpenAI-compatible `/embeddings` for the spawned server: one 1024-dim hashing vector
/// per request body. Texts sharing tokens land near each other (a query about note A ranks A
/// first); lexically disjoint notes are cosine-far apart, so the 0.07 dedup cap never trips.
/// One request per connection (`connection: close`); runs until the test process exits.
async fn spawn_hashing_embedder() -> u16 {
    use std::hash::{Hash, Hasher};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
        haystack.windows(needle.len()).position(|w| w == needle)
    }

    /// Hashing vectorizer: each alphanumeric token claims one dimension. Same text → same
    /// vector; shared tokens → shared dimensions; disjoint texts → near-orthogonal.
    fn vectorize(text: &str) -> Vec<f32> {
        const DIM: usize = 1024;
        let mut v = vec![0f32; DIM];
        for token in text.split(|c: char| !c.is_alphanumeric()) {
            let token = token.to_lowercase();
            if token.is_empty() {
                continue;
            }
            let mut h = std::collections::hash_map::DefaultHasher::new();
            token.hash(&mut h);
            let dim = usize::try_from(h.finish() % DIM as u64).unwrap_or(0);
            v[dim] += 1.0;
        }
        if v.iter().all(|&x| x == 0.0) {
            v[0] = 1.0;
        }
        v
    }

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind stub embedder");
    let port = listener.local_addr().expect("stub addr").port();
    tokio::spawn(async move {
        loop {
            let Ok((mut socket, _)) = listener.accept().await else {
                return;
            };
            tokio::spawn(async move {
                let mut buf: Vec<u8> = Vec::new();
                let mut chunk = [0u8; 8192];
                let headers_end = loop {
                    let Ok(n) = socket.read(&mut chunk).await else {
                        return;
                    };
                    if n == 0 {
                        return;
                    }
                    buf.extend_from_slice(&chunk[..n]);
                    if let Some(pos) = find_subslice(&buf, b"\r\n\r\n") {
                        break pos + 4;
                    }
                };
                let headers = String::from_utf8_lossy(&buf[..headers_end]).to_string();
                let content_length = headers
                    .lines()
                    .find_map(|l| {
                        l.to_ascii_lowercase()
                            .strip_prefix("content-length:")
                            .and_then(|v| v.trim().parse::<usize>().ok())
                    })
                    .unwrap_or(0);
                while buf.len() < headers_end + content_length {
                    let Ok(n) = socket.read(&mut chunk).await else {
                        break;
                    };
                    if n == 0 {
                        break;
                    }
                    buf.extend_from_slice(&chunk[..n]);
                }
                let body = String::from_utf8_lossy(&buf[headers_end..]).to_string();
                let payload = serde_json::json!({
                    "object": "list",
                    "data": [{"object": "embedding", "index": 0, "embedding": vectorize(&body)}],
                });
                let out = payload.to_string();
                let response = format!(
                    "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{out}",
                    out.len()
                );
                let _ = socket.write_all(response.as_bytes()).await;
                let _ = socket.flush().await;
            });
        }
    });
    port
}
