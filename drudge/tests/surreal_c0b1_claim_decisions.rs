//! C-SURALL2 C0b1 bundle 3, claim and claim_row writes.
//!
//! (a) An ingest on `backend=surreal` puts the note's claim in Surreal's `claim_row` and `claim`
//!     and nothing in pg; the sync's `catch_up` copies nothing either way. RED on f46a994, where the
//!     ingest wrote the claim to pg and the mirror copied it.
//! (b) The owner rule of the claim seal reads Surreal's `document`. An owner note that exists only
//!     in Surreal keeps its claim open when a non-owner note supersedes it, both by a newer claim in
//!     the same slot and by a `supersedes` edge. RED on f46a994, where the seal SQL ran in pg and
//!     read pg's `document.author`: a Surreal-only owner note looked like a non-owner one.
//!     The control (an owner note supersedes an owner note) must seal, so (b) is not "never seals".
//!
//! Needs a throwaway pgvector and a throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test surreal_c0b1_claim_decisions -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::too_many_lines)]

use std::path::Path;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::config::{BoringConfig, SurrealConfig};
use drudge::ingest::{DefaultChunker, Embed, FrontmatterGraphExtractor, ingest_file_with};
use drudge::store::{Store, SurrealClient, VectorRoute};
use serde_json::{Value, json};
use tokio_postgres::NoTls;

const DIM: usize = 1024;

fn env(name: &str) -> String {
    std::env::var(name).expect(name)
}

fn nanos() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos()
}

struct FlatEmbed;

impl Embed for FlatEmbed {
    fn embed(
        &self,
        _text: &str,
    ) -> impl std::future::Future<Output = anyhow::Result<Vec<f32>>> + Send {
        std::future::ready(Ok(vec![0.5_f32; DIM]))
    }
}

/// An empty pg, Surreal tables laid down by a fill of it, and a store routed to that Surreal.
async fn setup(namespace: &str) -> (Store, SurrealClient, tokio_postgres::Client) {
    let dsn = env("BORING_TEST_DATABASE_URL");
    let pg = Store::open(&dsn, DIM).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db.batch_execute(
        "DELETE FROM claim; DELETE FROM edge; DELETE FROM node; DELETE FROM document;",
    )
    .await
    .unwrap();
    let ns = format!("{namespace}_{}", nanos());
    let config: SurrealConfig = serde_json::from_value(
        json!({"url": env("BORING_TEST_SURREAL_URL"), "namespace": ns, "database": ns}),
    )
    .unwrap();
    let client = SurrealClient::new(config.settings(&|n| std::env::var(n).ok()).unwrap()).unwrap();
    client.fill_from(&pg).await.expect("fill of an empty pg");
    let routed = Store::open(&dsn, DIM)
        .await
        .unwrap()
        .with_vector_route(VectorRoute::Surreal(client.clone()));
    (routed, client, db)
}

/// A note file with one fact claim `tag/axis = value`, last modified `age_h` hours ago (the claim's
/// `valid_from` is the note's mtime).
fn note(dir: &Path, name: &str, author: &str, tag: &str, value: &str, age_h: u64) -> String {
    let path = dir.join(name);
    std::fs::write(
        &path,
        format!(
            "---\norigin: personal\nproject: \"\"\nkind: note\nauthor: {author}\nclaims:\n  - subject: {tag}\n    predicate: axis\n    value: {value}\n    kind: fact\n    confidence: certain\n---\n\nbody {tag} {value}\n"
        ),
    )
    .unwrap();
    std::fs::OpenOptions::new()
        .write(true)
        .open(&path)
        .unwrap()
        .set_modified(SystemTime::now() - Duration::from_hours(age_h))
        .unwrap();
    path.to_string_lossy().into_owned()
}

async fn ingest(store: &Store, path: &str) {
    ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor, _>(
        store,
        &FlatEmbed,
        &BoringConfig::from_env(),
        path,
    )
    .await
    .expect("ingest");
}

async fn surreal_rows(client: &SurrealClient, sql: &str) -> Vec<Value> {
    let answer = client.run(sql).await.expect("surreal query");
    answer[0].as_array().cloned().unwrap_or_default()
}

/// `(value, sealed)` of every `claim_row` of `tag`, oldest first.
async fn claim_rows(client: &SurrealClient, tag: &str) -> Vec<(String, bool)> {
    surreal_rows(
        client,
        &format!("SELECT * FROM claim_row WHERE subject = \"{tag}\" ORDER BY valid_from_us;"),
    )
    .await
    .iter()
    .map(|r| {
        (
            r["value"].as_str().unwrap().to_owned(),
            !r["superseded_us"].is_null(),
        )
    })
    .collect()
}

async fn pg_claims(db: &tokio_postgres::Client, tag: &str) -> i64 {
    db.query_one("SELECT count(*) FROM claim WHERE subject = $1", &[&tag])
        .await
        .unwrap()
        .get(0)
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_claim_written_by_the_engine_lands_only_in_surreal_and_catch_up_copies_nothing() {
    let (routed, client, db) = setup("c0b1_claim_a").await;
    let dir = tempfile::tempdir().unwrap();
    let tag = format!("c0b1a{}", nanos());
    let path = note(dir.path(), "a.md", "unknown", &tag, "v1", 1);
    ingest(&routed, &path).await;

    let in_pg = pg_claims(&db, &tag).await;
    let rows = claim_rows(&client, &tag).await;
    let vectors = surreal_rows(
        &client,
        &format!("SELECT cid FROM claim WHERE subject = \"{tag}\";"),
    )
    .await;
    println!(
        "C0b1 (a) after ingest: pg claim {in_pg}, surreal claim_row {}, surreal claim {}",
        rows.len(),
        vectors.len()
    );
    assert_eq!(in_pg, 0, "pg must not receive the claim");
    assert_eq!(rows, vec![("v1".to_owned(), false)], "claim_row in Surreal");
    assert_eq!(vectors.len(), 1, "open vector claim in Surreal");

    let moved = routed.catch_up_surreal().await.expect("catch_up");
    let after = claim_rows(&client, &tag).await;
    println!(
        "C0b1 (a) after catch_up: moved {moved:?}, claim_row {}",
        after.len()
    );
    assert_eq!(moved, (0, 0), "catch_up copies nothing");
    assert_eq!(after, rows, "catch_up neither deleted nor changed the row");
    assert_eq!(
        pg_claims(&db, &tag).await,
        0,
        "catch_up wrote nothing to pg"
    );
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn an_owner_note_only_surreal_holds_keeps_its_claim_when_a_non_owner_note_supersedes_it() {
    let (routed, client, db) = setup("c0b1_claim_b").await;
    let dir = tempfile::tempdir().unwrap();
    let tag = format!("c0b1b{}", nanos());
    let owner = note(dir.path(), "owner.md", "owner", &tag, "owner-says", 3);
    ingest(&routed, &owner).await;
    assert_eq!(
        pg_claims(&db, &tag).await,
        0,
        "pg holds neither the owner note nor its claim"
    );

    // A newer claim in the same slot from a note that is not the owner's.
    let other = note(dir.path(), "other.md", "unknown", &tag, "other-says", 1);
    ingest(&routed, &other).await;
    let rows = claim_rows(&client, &tag).await;
    println!("C0b1 (b) after the newer claim: {rows:?}");
    assert_eq!(
        rows,
        vec![
            ("owner-says".to_owned(), false),
            ("other-says".to_owned(), false)
        ],
        "a non-owner claim must not seal the owner's row"
    );

    // An explicit supersedes edge from the non-owner note.
    let report = routed
        .supersedes_in_tx(&[[other.clone(), owner.clone()]], None)
        .await
        .expect("supersedes");
    let rows = claim_rows(&client, &tag).await;
    println!(
        "C0b1 (b) after the edge: retired {:?}, rows {rows:?}",
        report.retired
    );
    assert!(
        report.retired.is_empty(),
        "the owner's slot was reported retired: {:?}",
        report.retired
    );
    assert_eq!(
        rows[0],
        ("owner-says".to_owned(), false),
        "owner row sealed"
    );
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn control_an_owner_note_supersedes_an_owner_note_and_the_older_claim_is_sealed() {
    let (routed, client, _db) = setup("c0b1_claim_c").await;
    let dir = tempfile::tempdir().unwrap();
    let tag = format!("c0b1c{}", nanos());
    let older = note(dir.path(), "older.md", "owner", &tag, "first", 3);
    ingest(&routed, &older).await;
    let newer = note(dir.path(), "newer.md", "owner", &tag, "second", 1);
    ingest(&routed, &newer).await;
    let rows = claim_rows(&client, &tag).await;
    println!("C0b1 (b) control: {rows:?}");
    assert_eq!(
        rows,
        vec![("first".to_owned(), true), ("second".to_owned(), false)],
        "an owner claim does seal an older owner claim"
    );
}
