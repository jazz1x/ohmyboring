//! One transaction per synced file (contract C-PORT3b). A file that fails midway leaves no rows —
//! its document row (and the sha it carries) included — so the next sync ingests it from scratch
//! instead of reading the same sha as "unchanged" and skipping the chunks it never got.
//!
//! Needs a Postgres instance reachable via `BORING_TEST_DATABASE_URL`; without it the tests skip.
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::fs;
use std::time::{SystemTime, UNIX_EPOCH};

use drudge::config::BoringConfig;
use drudge::ingest::{DefaultChunker, Embed, FrontmatterGraphExtractor, run_with};
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
