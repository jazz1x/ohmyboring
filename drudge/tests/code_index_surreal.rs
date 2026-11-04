//! C0b2: on `backend=surreal` the code-index sync reads and writes Surreal, and pg is not touched.
//! The store is given a pg DSN that nothing listens on: a sync that reached for pg would fail.
//!
//! Needs a throwaway SurrealDB only. Run:
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test code_index_surreal -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::fmt::Write as _;
use std::fs;

use drudge::code_index::{CodeIndexStore, sync_repository};
use drudge::config::{CodeIndexSource, CodeLanguage, SurrealConfig};
use drudge::store::SurrealClient;
use serde_json::json;
use tempfile::tempdir;

const DEAD_PG: &str = "postgresql://nobody:nobody@127.0.0.1:1/none";

fn store(namespace: &str) -> CodeIndexStore {
    let config: SurrealConfig = serde_json::from_value(json!({
        "url": std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL"),
        "namespace": namespace,
        "database": namespace,
    }))
    .unwrap();
    let client = SurrealClient::new(config.settings(&|k| std::env::var(k).ok()).unwrap()).unwrap();
    CodeIndexStore::connect(DEAD_PG)
        .unwrap()
        .with_surreal(Some(client))
}

fn source(id: &str, root: &std::path::Path) -> CodeIndexSource {
    CodeIndexSource::new(
        id,
        format!("Test {id}"),
        root.to_path_buf(),
        CodeLanguage::Rust,
        true,
    )
    .unwrap()
}

#[tokio::test]
#[ignore = "needs a throwaway surreal: see the run command in this file's header"]
async fn sync_writes_skips_and_prunes_in_surreal_without_pg() {
    let mut store = store("c0b2_sync");
    let root = tempdir().unwrap();
    fs::write(root.path().join("a.rs"), "pub fn alpha() {}\n").unwrap();
    fs::write(root.path().join("b.rs"), "pub fn beta() {}\n").unwrap();
    let src = source("c0b2-sync", root.path());

    let first = sync_repository(&mut store, &src).await.expect("first sync");
    assert_eq!((first.scanned, first.changed, first.deleted), (2, 2, 0));
    let status = store.status(Some("c0b2-sync")).await.unwrap();
    assert_eq!((status[0].files, status[0].symbols), (2, 2));

    let again = sync_repository(&mut store, &src).await.expect("resync");
    assert_eq!(
        (again.changed, again.unchanged),
        (0, 2),
        "sha read from Surreal"
    );

    fs::write(root.path().join("a.rs"), "pub fn gamma() {}\n").unwrap();
    fs::remove_file(root.path().join("b.rs")).unwrap();
    let third = sync_repository(&mut store, &src).await.expect("third sync");
    assert_eq!((third.changed, third.deleted), (1, 1));
    assert!(
        store
            .search("alpha", Some("c0b2-sync"))
            .await
            .unwrap()
            .is_empty()
    );
    assert!(
        store
            .search("beta", Some("c0b2-sync"))
            .await
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        store
            .search("gamma", Some("c0b2-sync"))
            .await
            .unwrap()
            .len(),
        1
    );
}

/// A file whose rows cannot fit one request fails the sync, and none of its rows are left behind;
/// the small file beside it is whole, and the next sync tries the big one again.
#[tokio::test]
#[ignore = "needs a throwaway surreal: see the run command in this file's header"]
async fn a_file_that_fails_leaves_none_of_its_rows() {
    let mut store = store("c0b2_atomic");
    let root = tempdir().unwrap();
    fs::write(root.path().join("small.rs"), "pub fn small_one() {}\n").unwrap();
    let mut big = String::new();
    for i in 0..12_000 {
        writeln!(big, "pub fn big_fn_{i}() {{}}").unwrap();
    }
    fs::write(root.path().join("big.rs"), &big).unwrap();
    let src = source("c0b2-atomic", root.path());

    let failed = sync_repository(&mut store, &src).await;
    assert!(failed.is_err(), "the oversize file must fail loudly");
    assert!(
        store
            .search("big_fn_", Some("c0b2-atomic"))
            .await
            .unwrap()
            .is_empty(),
        "no symbol of the failed file was written"
    );
    let hashes = store.existing_hashes("c0b2-atomic").await.unwrap();
    assert!(
        !hashes.contains_key("big.rs"),
        "no sha vouches for rows that are not there"
    );
    assert!(
        hashes.contains_key("small.rs")
            || store.search("small_one", None).await.unwrap().is_empty(),
        "a file is either whole (sha and symbols) or absent"
    );
}
