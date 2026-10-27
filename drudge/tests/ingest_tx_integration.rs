//! One transaction per synced file (contract C-PORT3b). A file that fails midway leaves no rows —
//! its document row (and the sha it carries) included — so the next sync ingests it from scratch
//! instead of reading the same sha as "unchanged" and skipping the chunks it never got.
//!
//! Needs a Postgres instance reachable via `BORING_TEST_DATABASE_URL`; without it the tests skip.
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::fs;
use std::time::{SystemTime, UNIX_EPOCH};

use drudge::config::BoringConfig;
use drudge::ingest::{
    DefaultChunker, Embed, FrontmatterGraphExtractor, ingest_file_with, run_with,
};
use drudge::store::Store;
use tempfile::tempdir;
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

/// A lowercase alphanumeric token no other test shares — it is the subject, tag, tool and concept
/// of one note, so every row that note writes can be found by it.
fn token(prefix: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}{ts}")
}

/// Returns a vector of the wrong length for any text carrying `BREAKS` — the chunk upsert's
/// dimension guard then fails after the document row is already written.
struct FaultyEmbed {
    broken: bool,
}

impl Embed for FaultyEmbed {
    fn embed(
        &self,
        text: &str,
    ) -> impl std::future::Future<Output = anyhow::Result<Vec<f32>>> + Send {
        let dim = if self.broken && text.contains("BREAKS") {
            3
        } else {
            1024
        };
        std::future::ready(Ok(vec![0.0_f32; dim]))
    }
}

fn note(token: &str, body: &str) -> String {
    format!(
        "---\norigin: personal\nproject: \"\"\nkind: note\ntags:\n  - {token}\ntools:\n  - {token}\nconcepts:\n  - {token}\nclaims:\n  - subject: {token}\n    predicate: axis\n    value: v1\n    kind: fact\n    confidence: certain\n---\n\n{body}\n"
    )
}

/// document, chunk, claim, node, edge rows that one note (path + token) owns.
async fn rows(db: &Client, path: &str, token: &str) -> [i64; 5] {
    let like = format!("%{token}%");
    let doc_id = format!("doc:{path}");
    let one = |row: tokio_postgres::Row| row.get::<_, i64>(0);
    [
        one(db
            .query_one(
                "SELECT count(*) FROM document WHERE source_path = $1;",
                &[&path],
            )
            .await
            .expect("count documents")),
        one(db
            .query_one(
                "SELECT count(*) FROM chunk WHERE source_path = $1;",
                &[&path],
            )
            .await
            .expect("count chunks")),
        one(db
            .query_one(
                "SELECT count(*) FROM claim WHERE source_path = $1;",
                &[&path],
            )
            .await
            .expect("count claims")),
        one(db
            .query_one("SELECT count(*) FROM node WHERE id LIKE $1;", &[&like])
            .await
            .expect("count nodes")),
        one(db
            .query_one(
                "SELECT count(*) FROM edge WHERE src = $1 OR src LIKE $2 OR dst LIKE $2;",
                &[&doc_id, &like],
            )
            .await
            .expect("count edges")),
    ]
}

struct Vault {
    _dir: tempfile::TempDir,
    dirs: Vec<String>,
    bad_path: String,
    bad_token: String,
    ok_path: String,
    ok_token: String,
}

fn vault_with_one_bad_file() -> Vault {
    let dir = tempdir().expect("vault dir");
    let bad_token = token("p3bbad");
    let ok_token = token("p3bok");
    let bad = dir.path().join(format!("{bad_token}.md"));
    let ok = dir.path().join(format!("{ok_token}.md"));
    fs::write(&bad, note(&bad_token, "BREAKS the chunk step")).expect("write bad");
    fs::write(&ok, note(&ok_token, "an ordinary body")).expect("write ok");
    Vault {
        dirs: vec![dir.path().to_string_lossy().into_owned()],
        bad_path: bad.to_string_lossy().into_owned(),
        ok_path: ok.to_string_lossy().into_owned(),
        bad_token,
        ok_token,
        _dir: dir,
    }
}

async fn sync(store: &Store, dirs: &[String], broken: bool) -> drudge::ingest::Stats {
    run_with::<DefaultChunker, FrontmatterGraphExtractor, _>(
        store,
        &FaultyEmbed { broken },
        &BoringConfig::default(),
        dirs,
    )
    .await
    .expect("sync survives a failing file")
}

/// A file whose chunk step fails leaves none of its rows behind.
#[tokio::test]
async fn a_file_that_fails_at_the_chunk_step_leaves_no_rows() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let v = vault_with_one_bad_file();

    let stats = sync(&store, &v.dirs, true).await;

    assert_eq!(stats.failed, 1, "only the bad file failed");
    assert_eq!(
        rows(&db, &v.bad_path, &v.bad_token).await,
        [0; 5],
        "document, chunk, claim, node, edge rows of the failed file"
    );
    let ok = rows(&db, &v.ok_path, &v.ok_token).await;
    assert!(
        ok.iter().all(|n| *n > 0),
        "control: the other file ingested fully: {ok:?}"
    );
}

/// After the failure, the next sync ingests the file to the same state a clean ingest gives.
#[tokio::test]
async fn the_next_sync_ingests_a_failed_file_like_a_clean_one() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let v = vault_with_one_bad_file();

    sync(&store, &v.dirs, true).await;
    let again = sync(&store, &v.dirs, false).await;

    let healed = rows(&db, &v.bad_path, &v.bad_token).await;
    let clean = rows(&db, &v.ok_path, &v.ok_token).await;
    assert!(
        clean.iter().all(|n| *n > 0),
        "control: a clean ingest wrote every kind of row: {clean:?}"
    );
    assert_eq!(
        healed, clean,
        "document, chunk, claim, node, edge rows: failed-then-resynced vs clean (chunks must not stay 0)"
    );
    assert_eq!(again.new, 1, "the retry counts as a new file");
}

/// A note naming `shared` as tags, tools and concepts, in the order given, and one claim per
/// `(subject, said_by_owner)` entry of `claims`.
fn shared_note(shared: &[String], claims: &[(String, bool)], body: &str) -> String {
    let list = |names: &[String]| {
        names
            .iter()
            .map(|n| format!("  - {n}"))
            .collect::<Vec<_>>()
            .join("\n")
    };
    let claims = claims
        .iter()
        .map(|(subject, said)| {
            let said = if *said { "\n    said_by: owner" } else { "" };
            format!(
                "  - subject: {subject}\n    predicate: axis\n    value: v1\n    kind: fact\n    confidence: certain{said}"
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    let names = list(shared);
    let claims = if claims.is_empty() {
        "[]".to_owned()
    } else {
        format!("\n{claims}")
    };
    let names = if shared.is_empty() {
        "[]".to_owned()
    } else {
        format!("\n{names}")
    };
    format!(
        "---\norigin: personal\nproject: \"\"\nkind: note\ntags: {names}\ntools: {names}\nconcepts: {names}\nclaims: {claims}\n---\n\n{body}\n"
    )
}

/// Ingests two files at once, 40 rounds, each round on rows no other round shares. Returns how
/// many rounds one of the two was aborted by Postgres (`deadlock detected`).
async fn aborted_rounds(make: impl Fn(&[String]) -> (String, String)) -> usize {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return 0;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let cfg = BoringConfig::default();
    let embedder = FaultyEmbed { broken: false };
    let mut aborted = 0;
    for round in 0..40 {
        let dir = tempdir().expect("vault dir");
        let names: Vec<String> = (0..6).map(|i| format!("{}x{i}", token("p3bdl"))).collect();
        let (first, second) = make(&names);
        let first_path = dir.path().join("first.md");
        let second_path = dir.path().join("second.md");
        fs::write(&first_path, first).expect("write first");
        fs::write(&second_path, second).expect("write second");
        let (first, second) = (
            first_path.to_string_lossy().into_owned(),
            second_path.to_string_lossy().into_owned(),
        );
        let (a, b) = tokio::join!(
            ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor, _>(
                &store, &embedder, &cfg, &first
            ),
            ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor, _>(
                &store, &embedder, &cfg, &second
            ),
        );
        if let Err(e) = a.as_ref().and(b.as_ref()) {
            eprintln!("round {round}: {e:#}");
            aborted += 1;
        }
    }
    aborted
}

/// One transaction per file holds the shared tag, tool, concept and claim rows it has touched
/// until it commits, so two files naming the same rows in opposite order wait on each other
/// unless every file takes them in one sorted order.
#[tokio::test]
async fn files_naming_shared_rows_in_opposite_order_do_not_deadlock() {
    let aborted = aborted_rounds(|names| {
        let reversed: Vec<String> = names.iter().rev().cloned().collect();
        let claims =
            |order: &[String]| order.iter().map(|n| (n.clone(), false)).collect::<Vec<_>>();
        (
            shared_note(names, &claims(names), "forward body"),
            shared_note(&reversed, &claims(&reversed), "backward body"),
        )
    })
    .await;
    assert_eq!(aborted, 0, "rounds (of 40) where one ingest was aborted");
}

/// The speaker node is written when a claim with `said_by` is reached. If that is a different
/// place in each file's order, one file holds a claim row and wants the speaker while the other
/// holds the speaker and wants that claim.
#[tokio::test]
async fn the_speaker_node_is_taken_at_one_place_in_every_files_order() {
    let aborted = aborted_rounds(|names| {
        (
            shared_note(
                &[],
                &[(names[3].clone(), false), (names[5].clone(), true)],
                "speaks last",
            ),
            shared_note(
                &[],
                &[(names[0].clone(), true), (names[3].clone(), false)],
                "speaks first",
            ),
        )
    })
    .await;
    assert_eq!(aborted, 0, "rounds (of 40) where one ingest was aborted");
}
