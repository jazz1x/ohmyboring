//! `remember` while a sync holds `sync_lock`: the caller gets its answer without waiting, and the
//! indexing finishes in order once the lock frees. Real throwaway Postgres
//! (`BORING_TEST_DATABASE_URL`); run serially. The lock is held by the test itself.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::Duration;

use serde_json::{Value, json};

use super::AppState;
use super::mcp::{Remembered, remember_note};
use super::owner::Caller;
use super::pg_failure_tests::{admin, fresh_store, unique};
use crate::store::{EventLogFilter, Store};

/// Bound on a call that must come back; reached only when the code under test waits on the lock.
const ANSWER_WITHIN: Duration = Duration::from_secs(10);

/// `/embeddings` answering a one-hot vector keyed on the text after the title, so two notes with
/// one body are one point whatever their titles, and different bodies are orthogonal.
async fn embed_stub() -> String {
    let app = axum::Router::new().route(
        "/embeddings",
        axum::routing::post(|axum::Json(req): axum::Json<Value>| async move {
            let text = req["input"].as_str().unwrap_or_default();
            let body = text.split_once("\n\n").map_or(text, |(_, b)| b);
            let hot = body.bytes().map(usize::from).sum::<usize>() % 1024;
            let mut v = vec![0.0_f32; 1024];
            v[hot] = 1.0;
            axum::Json(json!({"data": [{"embedding": v}]}))
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await });
    url
}

struct Fx {
    s: AppState,
    db: tokio_postgres::Client,
    _vault: tempfile::TempDir,
}

async fn fixture() -> Option<Fx> {
    let Some(admin_dsn) = std::env::var("BORING_TEST_DATABASE_URL").ok() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return None;
    };
    let name = unique("tdefer");
    let (store, dsn): (Store, String) = fresh_store(&admin_dsn, &name).await;
    let vault = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(vault.path().join("wiki")).unwrap();
    let mut cfg = crate::config::BoringConfig::default();
    cfg.llm.base_url = embed_stub().await;
    let s = AppState {
        store: Some(Arc::new(store)),
        code_index: None,
        llm: Arc::new(crate::llm::Llm::from_config(&cfg)),
        vault_dir: Arc::new(Some(vault.path().to_path_buf())),
        pii: Arc::new(None),
        cfg: Arc::new(cfg),
        cfg_path: Arc::new(None),
        sync_lock: Arc::default(),
        remember_gate: Arc::default(),
        deferred: Arc::default(),
        note_index: Arc::default(),
        wiki_index: Arc::default(),
        last_compact: Arc::default(),
        compact_failure: Arc::default(),
        db_healthy_last: Arc::default(),
        owner_token: Arc::new(None),
    };
    Some(Fx {
        s,
        db: admin(&dsn).await,
        _vault: vault,
    })
}

fn note(title: &str, body: &str, supersedes: &[&str]) -> Value {
    json!({
        "title": title,
        "body": body,
        "claims": [{"subject": title, "predicate": "state", "value": body, "kind": "fact"}],
        "supersedes": supersedes,
    })
}

async fn remember(s: &AppState, args: &Value) -> Remembered {
    tokio::time::timeout(
        ANSWER_WITHIN,
        remember_note(s, Caller::Unverified, Some(args)),
    )
    .await
    .expect("remember answered while sync_lock was held")
    .unwrap()
}

/// Until the deferred worker has committed everything it was given.
async fn drained(s: &AppState) {
    tokio::time::timeout(ANSWER_WITHIN, async {
        while s.deferred.has_backlog() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("deferred indexing finished");
}

async fn count(db: &tokio_postgres::Client, sql: &str, arg: &str) -> i64 {
    db.query_one(sql, &[&arg]).await.unwrap().get(0)
}

const CLAIMS_OF: &str = "SELECT count(*) FROM claim WHERE source_path = $1";
const SUPERSEDES_OF: &str =
    "SELECT count(*) FROM edge WHERE kind = 'supersedes' AND src = 'doc:' || $1";

async fn events(s: &AppState, name: &str) -> usize {
    event_ids(s, name).await.len()
}

async fn event_ids(s: &AppState, name: &str) -> Vec<i64> {
    s.store
        .as_ref()
        .unwrap()
        .recent_events(EventLogFilter {
            limit: 100,
            component: Some("drudge.mcp.remember"),
            event_name: Some(name),
            status: None,
            run_id: None,
            workflow: None,
            since_hours: None,
        })
        .await
        .unwrap()
        .into_iter()
        .map(|r| r.id)
        .collect()
}

#[tokio::test]
async fn remember_answers_while_a_sync_holds_the_lock() {
    let Some(fx) = fixture().await else { return };
    let _sync = fx.s.sync_lock.lock().await;

    let r = remember(&fx.s, &note("alpha", "first body", &[])).await;

    assert!(
        r.message.contains("indexing deferred"),
        "answer says indexing waits: {}",
        r.message
    );
    assert!(std::path::Path::new(&r.source_path).exists(), "note saved");
}

#[tokio::test]
async fn deferred_note_gets_its_claims_and_supersedes_edge_once_the_lock_frees() {
    let Some(fx) = fixture().await else { return };
    let old = remember(&fx.s, &note("old", "old body", &[])).await;
    let sync = fx.s.sync_lock.lock().await;

    let fix = remember(&fx.s, &note("fix", "fix body", &[&old.source_path])).await;
    assert_eq!(count(&fx.db, CLAIMS_OF, &fix.source_path).await, 0);
    drop(sync);
    drained(&fx.s).await;

    assert_eq!(count(&fx.db, CLAIMS_OF, &fix.source_path).await, 1);
    assert_eq!(count(&fx.db, SUPERSEDES_OF, &fix.source_path).await, 1);
}

#[tokio::test]
async fn deferred_notes_index_in_the_order_they_came() {
    let Some(fx) = fixture().await else { return };
    let sync = fx.s.sync_lock.lock().await;

    let a = remember(&fx.s, &note("first", "first body", &[])).await;
    let b = remember(&fx.s, &note("second", "second body", &[&a.source_path])).await;
    drop(sync);
    drained(&fx.s).await;

    assert_eq!(
        count(&fx.db, SUPERSEDES_OF, &b.source_path).await,
        1,
        "B points at A, and A was indexed first"
    );
}

#[tokio::test]
async fn embedding_duplicate_check_sees_a_note_still_waiting_to_be_indexed() {
    let Some(fx) = fixture().await else { return };
    let _sync = fx.s.sync_lock.lock().await;

    let a = remember(&fx.s, &note("one title", "same body", &[])).await;
    let again = remember(&fx.s, &note("other title", "same body", &[])).await;

    assert_eq!(again.duplicate.as_deref(), Some(a.source_path.as_str()));
}

#[tokio::test]
async fn deferring_writes_one_deferred_and_one_finished_line() {
    let Some(fx) = fixture().await else { return };
    let sync = fx.s.sync_lock.lock().await;

    remember(&fx.s, &note("alpha", "first body", &[])).await;
    assert_eq!(events(&fx.s, "indexing_deferred").await, 1);
    assert_eq!(events(&fx.s, "indexing_finished").await, 0);
    drop(sync);
    drained(&fx.s).await;

    assert_eq!(events(&fx.s, "indexing_deferred").await, 1);
    assert_eq!(events(&fx.s, "indexing_finished").await, 1);
    let deferred = event_ids(&fx.s, "indexing_deferred").await;
    let finished = event_ids(&fx.s, "indexing_finished").await;
    assert!(
        deferred[0] < finished[0],
        "deferred is logged before finished"
    );
}

#[tokio::test]
async fn sync_indexing_the_note_first_leaves_one_document_and_no_second_edge() {
    let Some(fx) = fixture().await else { return };
    let old = remember(&fx.s, &note("old", "old body", &[])).await;
    let sync = fx.s.sync_lock.lock().await;
    let fix = remember(&fx.s, &note("fix", "fix body", &[&old.source_path])).await;

    // What a sync that got the lock first does with the file the remember left on disk.
    let store = fx.s.store.as_ref().unwrap();
    let mut conn = store.conn().await.unwrap();
    let tx = conn.transaction().await.unwrap();
    crate::ingest::ingest_file_in_tx(store, &tx, &fx.s.llm, &fx.s.cfg, &fix.source_path)
        .await
        .unwrap();
    tx.commit().await.unwrap();
    drop(conn);
    drop(sync);
    drained(&fx.s).await;

    let docs = "SELECT count(*) FROM document WHERE source_path = $1";
    assert_eq!(count(&fx.db, docs, &fix.source_path).await, 1);
    assert_eq!(count(&fx.db, SUPERSEDES_OF, &fix.source_path).await, 1);
    assert_eq!(events(&fx.s, "indexing_finished").await, 1);
    let failed =
        fx.s.store
            .as_ref()
            .unwrap()
            .recent_events(EventLogFilter {
                limit: 100,
                component: Some("drudge.mcp.remember"),
                event_name: Some("indexing_finished"),
                status: Some("failed"),
                run_id: None,
                workflow: None,
                since_hours: None,
            })
            .await
            .unwrap();
    assert!(failed.is_empty(), "the finish after sync did not fail");
}

fn by_owner(mut args: Value) -> Value {
    args["author"] = json!("owner");
    args
}

const EDGES_INTO: &str =
    "SELECT count(*) FROM edge WHERE kind = 'supersedes' AND dst = 'doc:' || $1";

async fn refusals(s: &AppState) -> Vec<Value> {
    s.store
        .as_ref()
        .unwrap()
        .recent_events(EventLogFilter {
            limit: 100,
            component: Some("drudge.owner"),
            event_name: Some("owner_supersede_refused"),
            status: None,
            run_id: None,
            workflow: None,
            since_hours: None,
        })
        .await
        .unwrap()
        .into_iter()
        .map(|r| r.attributes)
        .collect()
}

#[tokio::test]
async fn a_non_owner_cannot_supersede_an_owner_note_that_is_still_waiting() {
    let Some(fx) = fixture().await else { return };
    let sync = fx.s.sync_lock.lock().await;
    let a = remember_note(
        &fx.s,
        Caller::Owner,
        Some(&by_owner(note("mine", "owner body", &[]))),
    )
    .await
    .unwrap();

    let b = tokio::time::timeout(
        ANSWER_WITHIN,
        remember_note(
            &fx.s,
            Caller::Unverified,
            Some(&note("theirs", "other body", &[&a.source_path])),
        ),
    )
    .await
    .expect("answered while sync_lock was held");
    drop(sync);
    drained(&fx.s).await;

    assert_eq!(count(&fx.db, EDGES_INTO, &a.source_path).await, 0);
    assert!(b.is_err(), "the request is refused");
    assert_eq!(refusals(&fx.s).await.len(), 1);
}

#[tokio::test]
async fn the_owner_can_supersede_their_own_note_that_is_still_waiting() {
    let Some(fx) = fixture().await else { return };
    let sync = fx.s.sync_lock.lock().await;
    let a = remember_note(
        &fx.s,
        Caller::Owner,
        Some(&by_owner(note("mine", "owner body", &[]))),
    )
    .await
    .unwrap();
    remember_note(
        &fx.s,
        Caller::Owner,
        Some(&by_owner(note(
            "mine again",
            "newer body",
            &[&a.source_path],
        ))),
    )
    .await
    .unwrap();
    drop(sync);
    drained(&fx.s).await;

    assert_eq!(count(&fx.db, EDGES_INTO, &a.source_path).await, 1);
    assert!(refusals(&fx.s).await.is_empty());
}

/// The worker's own check, with the request-time one out of the way: A is waiting as a plain
/// note when B names it, and becomes an owner note on disk before the worker ingests it.
#[tokio::test]
async fn the_worker_rechecks_owner_authorship_before_writing_the_edge() {
    let Some(fx) = fixture().await else { return };
    let sync = fx.s.sync_lock.lock().await;
    let a = remember(&fx.s, &note("plain", "plain body", &[])).await;
    remember(&fx.s, &note("theirs", "other body", &[&a.source_path])).await;
    let text = std::fs::read_to_string(&a.source_path).unwrap();
    let owned = if text.contains("author: unknown") {
        text.replace("author: unknown", "author: owner")
    } else {
        text.replacen("---\n", "---\nauthor: owner\n", 1)
    };
    std::fs::write(&a.source_path, owned).unwrap();
    drop(sync);
    drained(&fx.s).await;

    assert_eq!(count(&fx.db, EDGES_INTO, &a.source_path).await, 0);
    let refused = refusals(&fx.s).await;
    assert_eq!(refused.len(), 1);
    assert!(refused[0].to_string().contains(&a.source_path));
    let finished =
        fx.s.store
            .as_ref()
            .unwrap()
            .recent_events(EventLogFilter {
                limit: 100,
                component: Some("drudge.mcp.remember"),
                event_name: Some("indexing_finished"),
                status: None,
                run_id: None,
                workflow: None,
                since_hours: None,
            })
            .await
            .unwrap();
    assert!(
        finished
            .iter()
            .any(|f| f.attributes["refused"] == json!([a.source_path])),
        "B's finished line names the refused target"
    );
}
