//! C0b2: on `backend=surreal` the code-index sync reads and writes Surreal, and pg is not touched.
//! The store is given a pg DSN that nothing listens on: a sync that reached for pg would fail.
//!
//! Needs a throwaway SurrealDB only. Run:
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test code_index_surreal -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::fmt::Write as _;
use std::fs;

use drudge::code_index::{CodeIndexStore, code_check, sync_repository};
use drudge::config::{CodeIndexSource, CodeLanguage, SurrealConfig};
use drudge::store::SurrealClient;
use serde_json::json;
use tempfile::tempdir;

const DEAD_PG: &str = "postgresql://nobody:nobody@127.0.0.1:1/none";

/// A namespace of its own per run: rows a past run left (a mutant's real sha) made the next run
/// see the big file as unchanged and pass or fail for the wrong reason.
fn store(name: &str) -> CodeIndexStore {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let namespace = format!("{name}_{nanos}");
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

/// After a sync that changed a file, code-check agrees; a file edited after the sync makes it
/// disagree. Before C0b2 gap 2 it compared pg, which a surreal sync no longer writes.
#[tokio::test]
#[ignore = "needs a throwaway surreal: see the run command in this file's header"]
async fn code_check_agrees_after_a_changing_sync_and_disagrees_on_an_unsynced_edit() {
    let namespace = format!(
        "c0b2_check_{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let config: SurrealConfig = serde_json::from_value(json!({
        "url": std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL"),
        "namespace": namespace,
        "database": namespace,
    }))
    .unwrap();
    let client = SurrealClient::new(config.settings(&|k| std::env::var(k).ok()).unwrap()).unwrap();
    let mut store = CodeIndexStore::connect(DEAD_PG)
        .unwrap()
        .with_surreal(Some(client.clone()));
    let root = tempdir().unwrap();
    fs::write(root.path().join("a.rs"), "pub fn alpha() {}\n").unwrap();
    let src = source("c0b2-check", root.path());
    sync_repository(&mut store, &src).await.expect("first sync");
    fs::write(root.path().join("a.rs"), "pub fn gamma() {}\n").unwrap();
    sync_repository(&mut store, &src)
        .await
        .expect("changing sync");

    let after_sync = code_check(client.clone(), DEAD_PG, &[&src])
        .await
        .expect("code-check after sync");
    fs::write(root.path().join("a.rs"), "pub fn delta() {}\n").unwrap();
    let after_edit = code_check(client, DEAD_PG, &[&src])
        .await
        .expect("code-check after edit");

    assert!(after_sync.same, "{:?}", after_sync.lines);
    assert!(!after_edit.same, "{:?}", after_edit.lines);
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

/// A file whose rows cannot fit one request lands in pieces, whole: its last symbol is there and
/// its sha is recorded, so the next sync skips it.
#[tokio::test]
#[ignore = "needs a throwaway surreal: see the run command in this file's header"]
async fn a_file_over_one_request_lands_whole_in_pieces() {
    let mut store = store("c0b2_pieces");
    let root = tempdir().unwrap();
    let mut big = String::new();
    for i in 0..12_000 {
        writeln!(big, "pub fn big_fn_{i}() {{}}").unwrap();
    }
    fs::write(root.path().join("big.rs"), &big).unwrap();
    let src = source("c0b2-pieces", root.path());

    let first = sync_repository(&mut store, &src).await.expect("first sync");
    assert_eq!(first.changed, 1);
    assert_eq!(
        store
            .search("big_fn_11999", Some("c0b2-pieces"))
            .await
            .unwrap()
            .len(),
        1,
        "the last symbol of the big file landed"
    );
    let again = sync_repository(&mut store, &src)
        .await
        .expect("second sync");
    assert_eq!(
        (again.changed, again.unchanged),
        (0, 1),
        "its sha was recorded"
    );
}

/// A file cut off mid-way (one of its rows alone is over the request cap) fails the sync loudly,
/// and no real sha vouches for its rows: the next sync redoes it. The repository row stays as it was.
#[tokio::test]
#[ignore = "needs a throwaway surreal: see the run command in this file's header"]
async fn a_file_cut_off_mid_way_has_no_sha_vouching_for_it() {
    let mut store = store("c0b2_atomic");
    let root = tempdir().unwrap();
    // Sorts before big.rs, so it is written before the sync fails.
    fs::write(root.path().join("a_small.rs"), "pub fn small_one() {}\n").unwrap();
    let mut big = String::new();
    for i in 0..12_000 {
        writeln!(big, "pub fn big_fn_{i}() {{}}").unwrap();
    }
    writeln!(big, "pub fn {}() {{}}", "x".repeat(600 * 1024)).unwrap();
    fs::write(root.path().join("big.rs"), &big).unwrap();
    let src = source("c0b2-atomic", root.path());

    let failed = sync_repository(&mut store, &src).await;
    assert!(
        failed.is_err(),
        "a row over the request cap must fail loudly"
    );
    let hashes = store.existing_hashes("c0b2-atomic").await.unwrap();
    assert!(
        hashes.get("big.rs").is_none_or(String::is_empty),
        "no sha vouches for rows that are not there: {:?}",
        hashes.get("big.rs")
    );
    let small_has_sha = hashes.get("a_small.rs").is_some_and(|sha| !sha.is_empty());
    let small_has_symbols = !store.search("small_one", None).await.unwrap().is_empty();
    assert!(
        small_has_sha && small_has_symbols,
        "a file written before the failure is whole: sha {small_has_sha}, symbols {small_has_symbols}"
    );
    assert!(
        store.status(Some("c0b2-atomic")).await.unwrap().is_empty(),
        "the repository row is written last, so a failed sync does not advance it"
    );
}
