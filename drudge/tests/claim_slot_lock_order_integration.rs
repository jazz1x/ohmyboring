//! Contract C-LOCKSEAL1: inside one (subject, predicate) slot, `remember`'s supersede seal takes
//! the rows in `valid_from` order, while ingest's fact seal takes them in scan order. Here the
//! order is forced from the test side: one slot with three live rows, scan order B, A, X and
//! `valid_from` order A, X, B. A raw connection holds X, so the supersede stops after A; ingest
//! then seals the slot, takes B and stops on A; releasing X sends the supersede to B.
//!
//! Needs a Postgres instance reachable via `BORING_TEST_DATABASE_URL`; without it the test skips.
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::fs;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::config::BoringConfig;
use drudge::frontmatter::FrontMatter;
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

async fn seed_doc(store: &Store, path: &str) {
    let front = FrontMatter {
        origin: "personal".to_string(),
        project: "test".to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("slot lock order note".to_string()),
        tags: vec!["test".to_string()],
        author: "unknown".parse().expect("author"),
        ..Default::default()
    };
    store
        .upsert_document(&front, "sha-v1", SystemTime::now())
        .await
        .expect("upsert doc");
}

/// Waits until `waiting` backends sit on a lock inside a claim-row lock query (the supersede's
/// and the ingest's slot lock) or the ingest's seal statement.
async fn wait_lock_wait(db: &Client, waiting: i64) {
    loop {
        let n: i64 = db
            .query_one(
                "SELECT count(*) FROM pg_stat_activity
                  WHERE wait_event_type = 'Lock'
                    AND (query LIKE 'SELECT 1 FROM claim c%'
                         OR query LIKE 'UPDATE claim c SET superseded_at = m.mx%');",
                &[],
            )
            .await
            .expect("read pg_stat_activity")
            .get(0);
        if n >= waiting {
            return;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn remembers_supersede_and_ingest_take_one_slots_rows_in_one_order() {
    let Some(dsn) = std::env::var("BORING_TEST_DATABASE_URL").ok() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    let hold = connect(&dsn).await;
    let t = format!(
        "slk{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let dir = tempdir().expect("vault dir");
    let path = |name: &str| {
        dir.path()
            .join(format!("{t}-{name}.md"))
            .to_string_lossy()
            .into_owned()
    };
    let (judge, older, other, newer) = (path("judge"), path("old"), path("other"), path("new"));
    for p in [&judge, &older, &other] {
        seed_doc(&store, p).await;
    }
    // Insert order is scan order: B (latest) first, then A, then X; `valid_from` order is A, X, B.
    let subject = format!("{t}s00");
    for (source, age_h) in [(&older, 3_i32), (&judge, 5), (&other, 4)] {
        db.execute(
            "INSERT INTO claim (subject, predicate, value, source_path, valid_from, kind, confidence)
             VALUES ($1, 'axis', 'v1', $2, now() - make_interval(hours => $3), 'fact', 'certain');",
            &[&subject, source, &age_h],
        )
        .await
        .expect("seed claim row");
    }
    fs::write(
        &newer,
        format!(
            "---\norigin: personal\nproject: \"\"\nkind: note\ntags: []\ntools: []\nconcepts: []\nclaims:\n  - subject: {subject}\n    predicate: axis\n    value: v2\n    kind: fact\n    confidence: certain\n---\n\nnew note\n"
        ),
    )
    .expect("write new note");

    hold.batch_execute("BEGIN;").await.expect("begin");
    hold.execute(
        "SELECT 1 FROM claim WHERE subject = $1 AND source_path = $2 FOR UPDATE;",
        &[&subject, &other],
    )
    .await
    .expect("hold row X");

    let pairs = [[judge.clone(), older.clone()]];
    let cfg = BoringConfig::default();
    let (remember, ingest) = tokio::join!(store.remember_supersedes_in_tx(&pairs, None), async {
        wait_lock_wait(&db, 1).await;
        let ingest = ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor, _>(
            &store, &ZeroEmbed, &cfg, &newer,
        );
        let release = async {
            wait_lock_wait(&db, 2).await;
            hold.batch_execute("ROLLBACK;")
                .await
                .expect("release row X");
        };
        tokio::join!(ingest, release).0
    });

    let like = format!("{t}%");
    for p in [&judge, &older, &other, &newer] {
        store.delete_document(p).await.expect("delete document");
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

    let remember = remember.map(|_| ()).map_err(|e| format!("remember: {e:#}"));
    let ingest = ingest.map(|_| ()).map_err(|e| format!("ingest: {e:#}"));
    assert!(
        remember.is_ok() && ingest.is_ok(),
        "{remember:?} / {ingest:?}"
    );
}
