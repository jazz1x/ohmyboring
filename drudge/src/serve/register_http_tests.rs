//! The HTTP registers (`/decisions`, `/risks`, `/next_actions`) answer the keys MCP's
//! `register_json` does, and every item names the note its claim was written in — the card reads
//! `items[].source_path` instead of one `/claim-source` call per line (C-CARDADVICE6).
//! Needs `BORING_TEST_DATABASE_URL` (a fresh database is made and dropped); skipped without it.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::time::SystemTime;

use axum::http::StatusCode;

use super::build_router;
use super::pg_failure_tests::{admin, fresh_store, unique};
use super::split_tests::{call, state_with};

#[tokio::test]
async fn http_registers_carry_items_with_the_claims_source_path() {
    let Some(admin_dsn) = std::env::var("BORING_TEST_DATABASE_URL").ok() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let name = unique("t_register_http");
    let (store, dsn) = fresh_store(&admin_dsn, &name).await;
    let path = "wiki/wiki-cardadvice6.md";
    let (db, conn) = tokio_postgres::connect(&dsn, tokio_postgres::NoTls)
        .await
        .unwrap();
    tokio::spawn(conn);
    db.execute(
        "INSERT INTO document (source_path, project, origin, updated_at) \
         VALUES ($1, 'cardadvice6', 'personal', now())",
        &[&path],
    )
    .await
    .unwrap();
    let emb = [0.1_f32; 1024];
    for kind in ["decision", "risk", "next"] {
        store
            .upsert_claim(
                &format!("subj-{kind}"),
                "verdict",
                "the register row names the note it was written in",
                path,
                SystemTime::now(),
                &emb,
                kind,
                "certain",
            )
            .await
            .unwrap();
    }
    let router = build_router(state_with(Some(store), None));

    for route in ["/decisions", "/risks", "/next_actions"] {
        let (status, body) = call(
            &router,
            "POST",
            route,
            &[],
            r#"{"project":"cardadvice6","limit":2}"#,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{route}");
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["items"][0]["source_path"], path, "{route}: {v}");
        assert_eq!(v["total_matching"], 1, "{route}: {v}");
        assert_eq!(v["limit_applied"], false, "{route}: {v}");
    }

    drop(router);
    admin(&admin_dsn)
        .await
        .batch_execute(&format!("DROP DATABASE {name} WITH (FORCE)"))
        .await
        .unwrap();
}
