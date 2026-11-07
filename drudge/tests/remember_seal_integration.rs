//! What a `remember` with `supersedes` reports and closes (contract C-PORT3 items 2 and 5).
//! Python reference: `src/ohmyboring/remember/test_writer.py` (retired-claims suffix, partial seal).
//!
//! Needs a Postgres instance reachable via `BORING_TEST_DATABASE_URL`; without it the tests skip.
#![allow(clippy::expect_used, clippy::unwrap_used)]

#[path = "support/tx_store.rs"]
mod tx_store;
use tx_store::StoreTx as _;

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::frontmatter::FrontMatter;
use drudge::store::Store;
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

fn unique_id(prefix: &str) -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}-{ts}-{}", SEQ.fetch_add(1, Ordering::Relaxed))
}

fn whole_second_now() -> SystemTime {
    UNIX_EPOCH
        + Duration::from_secs(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs(),
        )
}

async fn seed_doc(store: &Store, path: &str) {
    let front = FrontMatter {
        origin: "personal".to_string(),
        project: "test".to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("seal test note".to_string()),
        tags: vec!["test".to_string()],
        author: "unknown".parse().expect("author"),
        ..Default::default()
    };
    store
        .upsert_document(&front, "sha-v1", SystemTime::now())
        .await
        .expect("upsert doc");
}

async fn seed_fact(store: &Store, path: &str, subject: &str, value: &str, at: SystemTime) {
    store
        .upsert_claim(
            subject,
            "state",
            value,
            path,
            at,
            &[0.0_f32; 1024],
            "fact",
            "certain",
        )
        .await
        .expect("upsert claim");
}

async fn live_subjects(db: &Client, path: &str) -> Vec<String> {
    db.query(
        "SELECT subject FROM claim WHERE source_path = $1 AND superseded_at IS NULL ORDER BY subject;",
        &[&path],
    )
    .await
    .expect("live claims")
    .iter()
    .map(|r| r.get(0))
    .collect()
}

struct Pair {
    store: Store,
    db: Client,
    old: String,
    new: String,
    slot_a: String,
    slot_b: String,
    t_new: SystemTime,
}

/// Old note says slot a (and b when `with_b`); the new note re-states only a. `new_claims` =
/// false leaves the new note with no claims at all.
async fn pair(dsn: &str, new_claims: bool, with_b: bool) -> Pair {
    let store = Store::open(dsn, 1024).await.expect("open store");
    let db = connect(dsn).await;
    let id = unique_id("seal");
    let old = format!("/vault/wiki/seal-{id}-old.md");
    let new = format!("/vault/wiki/seal-{id}-new.md");
    let slot_a = format!("slot-a-{id}");
    let slot_b = format!("slot-b-{id}");
    let t_old = whole_second_now();
    let t_new = t_old + Duration::from_mins(1);
    seed_doc(&store, &old).await;
    seed_doc(&store, &new).await;
    seed_fact(&store, &old, &slot_a, "old-a", t_old).await;
    if with_b {
        seed_fact(&store, &old, &slot_b, "old-b", t_old).await;
    }
    if new_claims {
        seed_fact(&store, &new, &slot_a, "new-a", t_new).await;
    }
    Pair {
        store,
        db,
        old,
        new,
        slot_a,
        slot_b,
        t_new,
    }
}

fn skip() -> bool {
    if test_dsn().is_none() {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return true;
    }
    false
}

/// Python `test_dedup_supersede_links_edge_and_partial_seal_only` (test_writer.py:642) and
/// `test_named_supersedes_skips_gate_and_counts_unknown` (:700): the answer lists the slots this
/// write closed, read after the seal, and says `none` when it closed nothing.
#[tokio::test]
async fn the_report_names_the_slots_this_write_closed() {
    if skip() {
        return;
    }
    let dsn = test_dsn().unwrap();
    let p = pair(&dsn, true, false).await;
    let report = p
        .store
        .record_supersedes(&[[p.new.clone(), p.old.clone()]], None)
        .await
        .expect("supersedes");
    assert_eq!(
        report.retired,
        vec![(p.slot_a.clone(), "state".to_owned())],
        "slot a was closed by this write"
    );
    let _ = (&p.slot_b, p.t_new, &p.db);
    p.store.delete_document(&p.old).await.ok();
    p.store.delete_document(&p.new).await.ok();
}

#[tokio::test]
async fn a_note_with_no_claims_retires_none() {
    if skip() {
        return;
    }
    let dsn = test_dsn().unwrap();
    let p = pair(&dsn, false, false).await;
    let report = p
        .store
        .record_supersedes(&[[p.new.clone(), p.old.clone()]], None)
        .await
        .expect("supersedes");
    assert!(report.retired.is_empty(), "{:?}", report.retired);
    p.store.delete_document(&p.old).await.ok();
    p.store.delete_document(&p.new).await.ok();
    let _ = live_subjects;
}

/// Owner decision (C-PORT3 item 5; Python `writer.py` top docstring, `_PARTIAL_SEAL_SQL`,
/// test_writer.py:642): a remember closes only the slots the new note re-states. The old note's
/// other facts stay live, and the report names only what this write closed.
#[tokio::test]
async fn remember_closes_only_the_slots_the_new_note_restates() {
    if skip() {
        return;
    }
    let dsn = test_dsn().unwrap();
    let p = pair(&dsn, true, true).await;
    let report = p
        .store
        .remember_supersedes_in_tx(&[[p.new.clone(), p.old.clone()]], None)
        .await
        .expect("supersedes");
    assert_eq!(
        live_subjects(&p.db, &p.old).await,
        vec![p.slot_b.clone()],
        "slot b was not re-stated, so it stays live"
    );
    assert_eq!(report.retired, vec![(p.slot_a.clone(), "state".to_owned())]);
    assert_eq!(report.supersedes, 1);
    p.store.delete_document(&p.old).await.ok();
    p.store.delete_document(&p.new).await.ok();
}

/// A new note that re-states nothing of the old one closes nothing, though the edge is written.
#[tokio::test]
async fn remember_closes_nothing_when_no_slot_is_restated() {
    if skip() {
        return;
    }
    let dsn = test_dsn().unwrap();
    let p = pair(&dsn, false, true).await;
    seed_fact(
        &p.store,
        &p.new,
        &format!("other-{}", p.slot_a),
        "x",
        p.t_new,
    )
    .await;
    let report = p
        .store
        .remember_supersedes_in_tx(&[[p.new.clone(), p.old.clone()]], None)
        .await
        .expect("supersedes");
    let mut both = vec![p.slot_a.clone(), p.slot_b.clone()];
    both.sort();
    assert_eq!(live_subjects(&p.db, &p.old).await, both);
    assert!(report.retired.is_empty(), "{:?}", report.retired);
    assert_eq!(report.supersedes, 1);
    p.store.delete_document(&p.old).await.ok();
    p.store.delete_document(&p.new).await.ok();
}
