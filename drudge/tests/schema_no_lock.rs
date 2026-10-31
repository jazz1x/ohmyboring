//! `Store::open` on an already-migrated database must not queue behind a concurrent writer.
//! Needs `BORING_TEST_DATABASE_URL` (skipped when unset), like `store_integration`.
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::time::Duration;

use drudge::store::Store;
use tokio_postgres::NoTls;

#[tokio::test]
async fn open_does_not_wait_for_a_writer_holding_document_rows() {
    let Ok(dsn) = std::env::var("BORING_TEST_DATABASE_URL") else {
        eprintln!("skip: BORING_TEST_DATABASE_URL unset");
        return;
    };
    Store::open(&dsn, 1024)
        .await
        .expect("first open creates the schema");

    let (writer, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    writer.batch_execute("BEGIN").await.unwrap();
    writer
        .execute(
            "INSERT INTO document (source_path) VALUES ('/vault/schema-no-lock-held.md')",
            &[],
        )
        .await
        .unwrap();

    let second = tokio::time::timeout(Duration::from_secs(5), Store::open(&dsn, 1024)).await;
    writer.batch_execute("ROLLBACK").await.unwrap();
    assert!(
        second.is_ok(),
        "second open blocked behind the writer's row lock"
    );
    second.unwrap().expect("second open");
}
