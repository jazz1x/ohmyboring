//! `compact` must not wait forever behind an open transaction: a table it cannot lock within the
//! lock timeout is skipped and reported. Needs `BORING_TEST_DATABASE_URL` (skipped when unset).
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::time::Duration;

use drudge::store::Store;
use tokio_postgres::NoTls;

#[tokio::test]
async fn compact_skips_a_table_it_cannot_lock_and_the_next_run_sweeps_the_leftover() {
    let Ok(dsn) = std::env::var("BORING_TEST_DATABASE_URL") else {
        eprintln!("skip: BORING_TEST_DATABASE_URL unset");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open");
    let (blocker, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    blocker.batch_execute("BEGIN").await.unwrap();
    blocker
        .execute(
            "INSERT INTO document (source_path) VALUES ('/vault/compact-lock-timeout-held.md')",
            &[],
        )
        .await
        .unwrap();

    let started = std::time::Instant::now();
    let run = tokio::time::timeout(
        Duration::from_secs(20),
        store.compact_with_lock_timeout(Duration::from_millis(500)),
    )
    .await;
    let elapsed = started.elapsed();
    if run.is_err() {
        blocker.batch_execute("ROLLBACK").await.unwrap();
    }
    let first = run
        .expect("compact still waiting behind the open transaction")
        .expect("compact must report a skip, not fail");
    assert!(elapsed < Duration::from_secs(20), "took {elapsed:?}");
    assert!(
        first
            .report
            .skipped_lock_timeout
            .iter()
            .any(|table| table == "document"),
        "report must name the skipped table: {:?}",
        first.report.skipped_lock_timeout
    );

    let leftovers: i64 = blocker
        .query_one(
            "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
              WHERE NOT i.indisvalid AND c.relname LIKE '%\\_ccnew%'",
            &[],
        )
        .await
        .unwrap()
        .get(0);
    assert!(leftovers >= 1, "cancelled REINDEX left no _ccnew index");

    blocker.batch_execute("ROLLBACK").await.unwrap();
    let second = store
        .compact_with_lock_timeout(Duration::from_secs(30))
        .await
        .expect("second compact");
    assert_eq!(second.report.skipped_lock_timeout, Vec::<String>::new());
    assert!(
        second.report.dropped_invalid_indexes >= 1,
        "next run must count the swept _ccnew"
    );
}
