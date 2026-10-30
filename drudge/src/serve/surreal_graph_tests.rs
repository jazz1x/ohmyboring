//! The two routes that write a session's verdict edges (`/consumption`, MCP `verdict`) on
//! `backend=surreal` with a Surreal that is down: pg has the session node, and the answer says so
//! instead of a plain success. Needs `BORING_TEST_DATABASE_URL` (a fresh database is made and
//! dropped); skipped without it. No Surreal server is needed — nothing listens on the port.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use axum::http::StatusCode;

use super::pg_failure_tests::{admin, fresh_store, unique};
use super::split_tests::{call, state_with};
use crate::serve::build_router;
use crate::store::{SURREAL_BEHIND, SurrealClient, VectorRoute};

fn dead_surreal() -> SurrealClient {
    let config: crate::config::SurrealConfig = serde_json::from_value(
        serde_json::json!({"url": "http://127.0.0.1:1", "namespace": "dead", "database": "dead"}),
    )
    .unwrap();
    SurrealClient::new(config.settings(&|n| std::env::var(n).ok()).unwrap()).unwrap()
}

async fn session_nodes_in_pg(dsn: &str, session: &str) -> i64 {
    let (db, conn) = tokio_postgres::connect(dsn, tokio_postgres::NoTls)
        .await
        .unwrap();
    tokio::spawn(conn);
    db.query_one(
        "SELECT count(*) FROM node WHERE id = $1",
        &[&format!("session:{session}")],
    )
    .await
    .unwrap()
    .get(0)
}

async fn drop_database(admin_dsn: &str, name: &str) {
    admin(admin_dsn)
        .await
        .batch_execute(&format!("DROP DATABASE {name} WITH (FORCE)"))
        .await
        .ok();
}

#[tokio::test]
async fn consumption_with_a_dead_surreal_says_pg_has_the_session() {
    let Some(admin_dsn) = std::env::var("BORING_TEST_DATABASE_URL").ok() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let name = unique("tsurc");
    let (store, dsn) = fresh_store(&admin_dsn, &name).await;
    let router = build_router(state_with(
        Some(store.with_vector_route(VectorRoute::Surreal(dead_surreal()))),
        None,
    ));
    let (status, body) = call(
        &router,
        "POST",
        "/consumption",
        &[],
        r#"{"session_id":"s-c","observed_at":"2026-10-30","used":[],"contested":[]}"#,
    )
    .await;
    let body = String::from_utf8_lossy(&body).into_owned();
    assert_ne!(
        status,
        StatusCode::OK,
        "a dead Surreal was swallowed: {body}"
    );
    assert!(body.contains(SURREAL_BEHIND), "answer was {body}");
    assert_eq!(session_nodes_in_pg(&dsn, "s-c").await, 1);
    drop_database(&admin_dsn, &name).await;
}

#[tokio::test]
async fn verdict_with_a_dead_surreal_says_pg_has_the_session() {
    let Some(admin_dsn) = std::env::var("BORING_TEST_DATABASE_URL").ok() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let name = unique("tsurv");
    let (store, dsn) = fresh_store(&admin_dsn, &name).await;
    let router = build_router(state_with(
        Some(store.with_vector_route(VectorRoute::Surreal(dead_surreal()))),
        None,
    ));
    let (_, body) = call(
        &router,
        "POST",
        "/mcp",
        &[],
        r#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"verdict","arguments":{"session_id":"s-v","verdict":"used"}}}"#,
    )
    .await;
    let body = String::from_utf8_lossy(&body).into_owned();
    assert!(body.contains(SURREAL_BEHIND), "answer was {body}");
    assert_eq!(session_nodes_in_pg(&dsn, "s-v").await, 1);
    drop_database(&admin_dsn, &name).await;
}
