//! Claim rows of one (subject, predicate) slot are locked in the same order by ingest and by the
//! judge's seal (contract C-PORT3b). Ingest seals other notes' rows slot by slot in slot order;
//! the seal of a retired note's claims walks its rows in scan order. A note whose rows were
//! written at different times has a scan order that is not its slot order.
//!
//! Needs a Postgres instance reachable via `BORING_TEST_DATABASE_URL`; without it the tests skip.
#![allow(clippy::expect_used, clippy::unwrap_used)]

#[path = "support/tx_store.rs"]
mod tx_store;
use tx_store::StoreTx as _;

use std::fs;
use std::time::{SystemTime, UNIX_EPOCH};

use drudge::config::BoringConfig;
use drudge::frontmatter::FrontMatter;
use drudge::ingest::{DefaultChunker, Embed, FrontmatterGraphExtractor, ingest_file_with};
use drudge::store::Store;
use tempfile::tempdir;
use tokio_postgres::{Client, NoTls};

const ROUNDS: usize = 40;
const SLOTS: usize = 12;

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

fn token(prefix: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}{ts}")
}

struct ZeroEmbed;

impl Embed for ZeroEmbed {
    fn embed(
        &self,
        _text: &str,
    ) -> impl std::future::Future<Output = anyhow::Result<Vec<f32>>> + Send {
        std::future::ready(Ok(vec![0.0_f32; 1024]))
    }
}

fn slot(round_token: &str, i: usize) -> String {
    format!("{round_token}s{i:02}")
}

/// A note with one fact claim per slot index in `slots`.
fn note(round_token: &str, slots: &[usize], body: &str) -> String {
    let claims = slots
        .iter()
        .map(|i| {
            format!(
                "  - subject: {}\n    predicate: axis\n    value: v1\n    kind: fact\n    confidence: certain",
                slot(round_token, *i)
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    format!(
        "---\norigin: personal\nproject: \"\"\nkind: note\ntags: []\ntools: []\nconcepts: []\nclaims:\n{claims}\n---\n\n{body}\n"
    )
}

async fn ingest(store: &Store, path: &str) -> anyhow::Result<()> {
    let cfg = BoringConfig::default();
    ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor, _>(store, &ZeroEmbed, &cfg, path)
        .await
        .map(|_| ())
}

async fn seed_doc(store: &Store, path: &str) {
    let front = FrontMatter {
        origin: "personal".to_string(),
        project: "test".to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("lock order note".to_string()),
        tags: vec!["test".to_string()],
        author: "unknown".parse().expect("author"),
        ..Default::default()
    };
    store
        .upsert_document(&front, "sha-v1", SystemTime::now())
        .await
        .expect("upsert doc");
}

async fn forget(store: &Store, db: &Client, paths: &[&str], round_token: &str) {
    let like = format!("%{round_token}%");
    for path in paths {
        store.delete_document(path).await.expect("delete document");
    }
    db.execute(
        "DELETE FROM edge WHERE src LIKE $1 OR dst LIKE $1;",
        &[&like],
    )
    .await
    .expect("delete edges");
    db.execute("DELETE FROM claim WHERE subject LIKE $1;", &[&like])
        .await
        .expect("delete claims");
    db.execute("DELETE FROM node WHERE id LIKE $1;", &[&like])
        .await
        .expect("delete nodes");
}

/// Slots where both the old note's row and the new note's row exist — the rows both requests
/// had to lock.
async fn shared_slot_rows(db: &Client, round_token: &str, old: &str, new: &str) -> i64 {
    let like = format!("{round_token}%");
    db.query_one(
        "SELECT count(*) FROM claim o JOIN claim n
           ON n.subject = o.subject AND n.predicate = o.predicate
          WHERE o.source_path = $1 AND n.source_path = $2 AND o.subject LIKE $3;",
        &[&old, &new, &like],
    )
    .await
    .expect("count shared slots")
    .get(0)
}

/// A request ingesting a new note over the old note's slots while a judge write retires the old
/// note, 40 rounds. The old note's rows were written back half first (slots 6..11, then 0..5), so
/// its scan order is not slot order. Returns rounds in which a request was aborted.
async fn aborted_rounds() -> usize {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return 0;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let mut aborted = 0;
    let mut overlap_total = 0;
    for round in 0..ROUNDS {
        let dir = tempdir().expect("vault dir");
        let t = token("p3cl");
        let older = dir
            .path()
            .join(format!("{t}-old.md"))
            .to_string_lossy()
            .into_owned();
        let newer = dir
            .path()
            .join(format!("{t}-new.md"))
            .to_string_lossy()
            .into_owned();
        let judge_note = dir
            .path()
            .join(format!("{t}-judge.md"))
            .to_string_lossy()
            .into_owned();
        let back_half: Vec<usize> = (SLOTS / 2..SLOTS).collect();
        let all: Vec<usize> = (0..SLOTS).collect();
        fs::write(&older, note(&t, &back_half, "first half")).expect("write old");
        ingest(&store, &older).await.expect("seed old, back half");
        fs::write(&older, note(&t, &all, "all slots")).expect("rewrite old");
        ingest(&store, &older).await.expect("seed old, all slots");
        seed_doc(&store, &judge_note).await;
        fs::write(&newer, note(&t, &all, "new note")).expect("write new");

        let session = format!("s-{t}");
        let pairs = [[judge_note.clone(), older.clone()]];
        // The judge write starts 0..7 ms after the ingest, so some rounds meet in the middle of
        // the ingest's slot walk wherever that walk is on a given machine.
        let delay = std::time::Duration::from_millis((round % 8) as u64);
        let (a, b) = tokio::join!(ingest(&store, &newer), async {
            tokio::time::sleep(delay).await;
            store
                .record_consumption(&session, "2026-10-28", &[], &[], &pairs, None)
                .await
        });
        let overlap = shared_slot_rows(&db, &t, &older, &newer).await;
        overlap_total += overlap;
        let err = a
            .err()
            .map(|e| format!("ingest: {e:#}"))
            .or(b.err().map(|e| format!("judge: {e:#}")));
        if let Some(e) = err {
            eprintln!("round {round}: {e}");
            aborted += 1;
        }
        forget(&store, &db, &[&older, &newer, &judge_note], &t).await;
    }
    eprintln!(
        "slots both requests wrote, summed over {ROUNDS} rounds: {overlap_total} (of {})",
        ROUNDS * SLOTS
    );
    aborted
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn ingest_and_the_verdict_seal_lock_one_slots_rows_in_one_order() {
    let aborted = aborted_rounds().await;
    assert_eq!(
        aborted, 0,
        "rounds (of {ROUNDS}) where a request was aborted"
    );
}
