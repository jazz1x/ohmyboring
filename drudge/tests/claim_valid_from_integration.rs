//! A session note's claims are valid from the moment the distilled conversation ended
//! (`omb_session_until`), not from when the note file was written (contract C-CLAIMTIME1).
//!
//! Needs a Postgres instance reachable via `BORING_TEST_DATABASE_URL`; without it the tests skip.
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::fs;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::config::BoringConfig;
use drudge::ingest::{DefaultChunker, Embed, FrontmatterGraphExtractor, ingest_file_with};
use drudge::store::Store;
use tempfile::tempdir;
use tokio_postgres::{Client, NoTls};

struct ZeroEmbed;

impl Embed for ZeroEmbed {
    fn embed(
        &self,
        _text: &str,
    ) -> impl std::future::Future<Output = anyhow::Result<Vec<f32>>> + Send {
        std::future::ready(Ok(vec![0.0_f32; 1024]))
    }
}

async fn connect(dsn: &str) -> Client {
    let (client, conn) = tokio_postgres::connect(dsn, NoTls)
        .await
        .expect("connect to Postgres");
    tokio::spawn(conn);
    client
}

fn token(prefix: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}{ts}")
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
}

fn session_note(subject: &str, value: &str, until: u64) -> String {
    format!(
        "---\norigin: personal\nproject: \"\"\nkind: note\nomb_session_id: s-{subject}\nomb_session_until: '{until}'\nclaims:\n  - subject: {subject}\n    predicate: axis\n    value: {value}\n    kind: fact\n    confidence: certain\n---\n\n{value} body\n"
    )
}

/// (source_path, whole seconds since the epoch of valid_from) for every row of the subject.
async fn claim_rows(db: &Client, subject: &str) -> Vec<(String, u64)> {
    db.query(
        "SELECT source_path, valid_from FROM claim WHERE subject = $1 ORDER BY source_path;",
        &[&subject],
    )
    .await
    .expect("read claims")
    .into_iter()
    .map(|r| {
        let at: SystemTime = r.get(1);
        (
            r.get::<_, String>(0),
            at.duration_since(UNIX_EPOCH).unwrap().as_secs(),
        )
    })
    .collect()
}

async fn forget(store: &Store, db: &Client, paths: &[&str], subject: &str) {
    for path in paths {
        store.delete_document(path).await.expect("delete document");
    }
    db.execute("DELETE FROM claim WHERE subject = $1;", &[&subject])
        .await
        .expect("delete claims");
    let like = format!("%{subject}%");
    db.execute(
        "DELETE FROM edge WHERE src LIKE $1 OR dst LIKE $1;",
        &[&like],
    )
    .await
    .expect("delete edges");
    db.execute("DELETE FROM node WHERE id LIKE $1;", &[&like])
        .await
        .expect("delete nodes");
}

/// (a) The slice ended 30 days before the file was written: the claim is as old as the slice.
#[tokio::test]
async fn a_session_notes_claim_is_valid_from_the_slice_end_not_the_file_mtime() {
    let Ok(dsn) = std::env::var("BORING_TEST_DATABASE_URL") else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let subject = token("ctslice");
    let until = now_secs() - 30 * 86_400;
    let dir = tempdir().expect("vault dir");
    let path = dir.path().join("old-session.md");
    fs::write(&path, session_note(&subject, "v1", until)).expect("write note");
    let path = path.to_string_lossy().into_owned();

    ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor, _>(
        &store,
        &ZeroEmbed,
        &BoringConfig::default(),
        &path,
    )
    .await
    .expect("ingest");

    let rows = claim_rows(&db, &subject).await;
    forget(&store, &db, &[&path], &subject).await;
    assert_eq!(rows, vec![(path, until)], "claim valid_from, whole seconds");
}

/// (b) Two notes on the same (subject, predicate) whose slices ended in the same second must both
/// keep their row: the claim key is (subject, predicate, valid_from) and an equal key overwrites
/// the other note's row, so the sub-second part of valid_from tells them apart.
#[tokio::test]
async fn two_notes_with_the_same_slice_end_second_both_keep_their_claim_row() {
    let Ok(dsn) = std::env::var("BORING_TEST_DATABASE_URL") else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let subject = token("ctclash");
    let until = now_secs() - 3_600;
    let dir = tempdir().expect("vault dir");
    let mut paths = Vec::new();
    for (name, value) in [("first.md", "v1"), ("second.md", "v2")] {
        let p = dir.path().join(name);
        fs::write(&p, session_note(&subject, value, until)).expect("write note");
        let p = p.to_string_lossy().into_owned();
        ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor, _>(
            &store,
            &ZeroEmbed,
            &BoringConfig::default(),
            &p,
        )
        .await
        .expect("ingest");
        paths.push(p);
    }

    let rows = claim_rows(&db, &subject).await;
    let exact: Vec<SystemTime> = db
        .query(
            "SELECT valid_from FROM claim WHERE subject = $1;",
            &[&subject],
        )
        .await
        .expect("read valid_from")
        .into_iter()
        .map(|r| r.get(0))
        .collect();
    forget(&store, &db, &[&paths[0], &paths[1]], &subject).await;

    let mut sources: Vec<&str> = rows.iter().map(|(p, _)| p.as_str()).collect();
    sources.sort_unstable();
    let mut expected: Vec<&str> = paths.iter().map(String::as_str).collect();
    expected.sort_unstable();
    assert_eq!(sources, expected, "one row per note");
    assert!(
        rows.iter().all(|(_, secs)| *secs == until),
        "both rows stay inside the slice-end second: {rows:?}"
    );
    let floor = UNIX_EPOCH + Duration::from_secs(until);
    assert!(exact.iter().all(|t| *t >= floor));
    assert_ne!(exact[0], exact[1], "the two rows have distinct keys");
}
