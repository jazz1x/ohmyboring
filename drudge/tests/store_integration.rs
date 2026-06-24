//! Rust integration tests for the Storage Layer contract.
//!
//! These tests exercise the live PostgreSQL backend, NOT the HTTP/MCP surface
//! (that belongs in `scripts/e2e.sh` and `data/eval/run_eval.py`). They need a
//! Postgres instance reachable via `BORING_TEST_DATABASE_URL`. If the variable is
//! unset, the tests are skipped with a clear message.
//!
//! Run via: `BORING_TEST_DATABASE_URL=postgresql://boring:boring@localhost:5432/boring_test cargo test -p drudge --test store_integration`
#![allow(clippy::expect_used, clippy::unwrap_used)] // tests may fail fast on setup errors

use std::time::{SystemTime, UNIX_EPOCH};

use drudge::frontmatter::{Claim, FrontMatter};
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

/// Ensure delete_document removes not only document/edge rows but also claims,
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
