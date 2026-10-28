//! `/repairs/split-subjects*` against a throwaway Postgres (`BORING_TEST_DATABASE_URL`; skipped
//! without it, run with `--test-threads=1`). Expected status and bytes in
//! `tests/fixtures/split_subjects_python.json` were recorded from the Python door on the same
//! fixture rows (`tests/fixtures/split_subjects_fixture.sql`), 2026-10-28.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use axum::http::StatusCode;
use tower::ServiceExt;

use crate::serve::{AppState, build_router};
use crate::store::Store;

const FIXTURE_SQL: &str = include_str!("../../tests/fixtures/split_subjects_fixture.sql");
const PYTHON_BYTES: &str = include_str!("../../tests/fixtures/split_subjects_python.json");
const OWNER_TOKEN: &str = "tok";

pub(super) fn dsn() -> Option<String> {
    std::env::var("BORING_TEST_DATABASE_URL").ok()
}

pub(super) fn state_with(store: Option<Store>, owner_token: Option<&str>) -> AppState {
    let cfg = crate::config::BoringConfig::default();
    AppState {
        store: store.map(Arc::new),
        code_index: None,
        llm: Arc::new(crate::llm::Llm::from_config(&cfg)),
        vault_dir: Arc::new(None),
        pii: Arc::new(None),
        cfg: Arc::new(cfg),
        cfg_path: Arc::new(None),
        sync_lock: Arc::default(),
        remember_gate: Arc::default(),
        note_index: Arc::default(),
        wiki_index: Arc::default(),
        last_compact: Arc::default(),
        compact_failure: Arc::default(),
        db_healthy_last: Arc::default(),
        owner_token: Arc::new(owner_token.map(str::to_owned)),
    }
}

pub(super) async fn seeded_store(dsn: &str) -> Store {
    let store = Store::open(dsn, 1024).await.expect("open store");
    let (client, conn) = tokio_postgres::connect(dsn, tokio_postgres::NoTls)
        .await
        .expect("connect");
    tokio::spawn(conn);
    client.batch_execute(FIXTURE_SQL).await.expect("fixture");
    store
}

pub(super) async fn call(
    router: &axum::Router,
    method: &str,
    uri: &str,
    headers: &[(&str, &str)],
    body: &str,
) -> (StatusCode, Vec<u8>) {
    let mut req = axum::extract::Request::builder().method(method).uri(uri);
    for (k, v) in headers {
        req = req.header(*k, *v);
    }
    let req = req
        .header("content-type", "application/json")
        .body(axum::body::Body::from(body.to_owned()))
        .unwrap();
    let res = router.clone().oneshot(req).await.unwrap();
    let status = res.status();
    let bytes = axum::body::to_bytes(res.into_body(), 1 << 20)
        .await
        .unwrap();
    (status, bytes.to_vec())
}

#[tokio::test]
async fn reads_answer_the_python_bytes() {
    let Some(dsn) = dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let router = build_router(state_with(
        Some(seeded_store(&dsn).await),
        Some(OWNER_TOKEN),
    ));
    let cases: Vec<serde_json::Value> = serde_json::from_str(PYTHON_BYTES).unwrap();
    let mut wrong = Vec::new();
    for case in &cases {
        let name = case[0].as_str().unwrap();
        let uri = case[2].as_str().unwrap();
        let (status, body) = call(&router, case[1].as_str().unwrap(), uri, &[], "").await;
        let want_status = u16::try_from(case[5].as_u64().unwrap()).unwrap();
        let want_body = case[6].as_str().unwrap();
        if status.as_u16() != want_status || body != want_body.as_bytes() {
            wrong.push(format!(
                "{name}: got {status} {} want {want_status} {want_body}",
                String::from_utf8_lossy(&body)
            ));
        }
    }
    assert!(
        wrong.is_empty(),
        "{} of {} differ:\n{}",
        wrong.len(),
        cases.len(),
        wrong.join("\n")
    );
}
