//! C-SURCOMBO1 S3: swap-in rebuild of the HNSW index. After rows changed, a new-named index is
//! defined, confirmed built, and only then is the old one removed; an in-place REBUILD is never
//! used (it answers 0 rows silently while building).
//!
//! Needs a throwaway pgvector (its document and claim tables are emptied on each run) and a
//! throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test vector_surreal_swap -- --ignored --nocapture --test-threads=1
#![allow(
    clippy::expect_used,
    clippy::unwrap_used,
    clippy::too_many_lines,
    clippy::panic,
    clippy::cast_possible_wrap
)]

use std::time::SystemTime;

use drudge::config::SurrealConfig;
use drudge::frontmatter::FrontMatter;
use drudge::store::{Store, SurrealClient, Swap, VectorRoute};
use drudge::vector::VectorIndex;
use serde_json::json;
use tokio_postgres::NoTls;

const DIM: usize = 1024;

fn vec_at(i: usize) -> Vec<f32> {
    let mut v = vec![0.0; DIM];
    v[i % DIM] = 1.0;
    v
}

struct Fixture {
    store: Store,
    client: SurrealClient,
}

async fn fixture(namespace: &str) -> Fixture {
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").expect("BORING_TEST_DATABASE_URL");
    let url = std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL");
    let config: SurrealConfig =
        serde_json::from_value(json!({"url": url, "namespace": namespace, "database": namespace}))
            .unwrap();
    let client = SurrealClient::new(config.settings(&|k| std::env::var(k).ok()).unwrap()).unwrap();
    let pg = Store::open(&dsn, DIM).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db.batch_execute("DELETE FROM claim; DELETE FROM document;")
        .await
        .unwrap();
    client.fill_from(&pg).await.expect("fill of an empty pg");
    Fixture {
        store: pg.with_vector_route(VectorRoute::Surreal(client.clone())),
        client,
    }
}

impl Fixture {
    async fn note(&self, path: &str, project: &str, hot: usize) {
        let front = FrontMatter {
            origin: "personal".to_string(),
            project: project.to_string(),
            kind: "note".to_string(),
            source_path: path.to_string(),
            title: Some("s3".to_string()),
            ..Default::default()
        };
        let mut conn = self.store.conn().await.expect("conn");
        let tx = conn.transaction().await.expect("begin");
        self.store
            .write_note_on(
                &tx,
                &front,
                "sha",
                SystemTime::now(),
                vec![(format!("chunk of {path}"), vec_at(hot))],
            )
            .await
            .expect("document and chunk");
        tx.commit().await.expect("commit");
    }

    /// Index names of one table, as the server reports them.
    async fn indexes(&self, table: &str) -> Vec<String> {
        let rows = self
            .client
            .run(&format!("INFO FOR TABLE {table};"))
            .await
            .unwrap();
        let mut names: Vec<String> = rows[0]["indexes"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect();
        names.sort();
        names
    }

    async fn swap(&self) -> Vec<Swap> {
        self.client.swap_if_changed().await.expect("swap run")
    }
}

fn of<'a>(swaps: &'a [Swap], table: &str) -> &'a Swap {
    swaps
        .iter()
        .find(|s| match s {
            Swap::Skipped { table: t }
            | Swap::Swapped { table: t, .. }
            | Swap::Failed { table: t, .. } => t == table,
        })
        .unwrap_or_else(|| panic!("no swap line for {table}: {swaps:?}"))
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn zero_changed_rows_means_no_swap() {
    let f = fixture("sw_zero").await;
    let before = f.indexes("chunk").await;
    let swaps = f.swap().await;
    for s in &swaps {
        println!("{}", s.line());
    }
    assert!(
        swaps.iter().all(|s| matches!(s, Swap::Skipped { .. })),
        "a freshly filled database has nothing to swap: {swaps:?}"
    );
    assert_eq!(
        f.indexes("chunk").await,
        before,
        "no index was defined or removed"
    );
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn changed_rows_swap_in_a_new_index_and_answers_stay() {
    let f = fixture("sw_changed").await;
    f.note("/v/a.md", "oh-my-boring", 1).await;
    f.note("/v/b.md", "oh-my-boring", 2).await;
    let swaps = f.swap().await;
    for s in &swaps {
        println!("{}", s.line());
    }
    let Swap::Swapped {
        old, new, status, ..
    } = of(&swaps, "chunk")
    else {
        panic!("chunk must swap: {swaps:?}");
    };
    assert_eq!(
        (old.as_str(), new.as_str()),
        ("chunk_embedding", "chunk_embedding_g1")
    );
    assert!(
        status.contains("ready"),
        "the build state is quoted: {status}"
    );
    assert_eq!(
        f.indexes("chunk").await,
        [
            "chunk_content_ft",
            "chunk_embedding_g1",
            "chunk_source_path"
        ]
    );
    let hits = f
        .store
        .vector_search(&vec_at(1), 2)
        .await
        .expect("search after swap");
    assert_eq!(hits.first().map(|h| h.id.as_str()), Some("/v/a.md#0"));
    // The changed-row count was consumed: the next run has nothing to do.
    assert!(
        f.swap()
            .await
            .iter()
            .all(|s| matches!(s, Swap::Skipped { .. }))
    );
    // A second change swaps again, to the next generation.
    f.note("/v/c.md", "alpha", 3).await;
    let Swap::Swapped { new, .. } = of(&f.swap().await, "chunk").clone() else {
        panic!("second swap");
    };
    assert_eq!(new, "chunk_embedding_g2");
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_failing_define_keeps_the_old_index_and_fails_the_run() {
    let f = fixture("sw_fail").await;
    f.note("/v/a.md", "oh-my-boring", 1).await;
    // The name the new index would take is taken by another index: DEFINE of the new one fails.
    f.client
        .run("DEFINE INDEX chunk_embedding_g1 ON chunk FIELDS cid;")
        .await
        .expect("blocker");
    let swaps = f.swap().await;
    for s in &swaps {
        println!("{}", s.line());
    }
    let Swap::Failed { error, .. } = of(&swaps, "chunk") else {
        panic!("chunk must fail: {swaps:?}");
    };
    assert!(
        error.contains("chunk_embedding_g1"),
        "the failure names the new index: {error}"
    );
    assert!(swaps.iter().any(Swap::failed));
    assert_eq!(
        f.indexes("chunk").await,
        [
            "chunk_content_ft",
            "chunk_embedding",
            "chunk_embedding_g1",
            "chunk_source_path"
        ],
        "the old HNSW index is still there"
    );
    let hits = f
        .store
        .vector_search(&vec_at(1), 1)
        .await
        .expect("search on the old index");
    assert_eq!(hits.first().map(|h| h.id.as_str()), Some("/v/a.md#0"));

    // Control: with the blocker gone the same state swaps, so the failure above was the blocker.
    f.client
        .run("REMOVE INDEX chunk_embedding_g1 ON chunk;")
        .await
        .expect("remove blocker");
    let swaps = f.swap().await;
    assert!(
        matches!(of(&swaps, "chunk"), Swap::Swapped { .. }),
        "control: {swaps:?}"
    );
}

impl Fixture {
    /// `vector_meta:<table>.dirty`, 0 when the row is absent.
    async fn dirty(&self, table: &str) -> i64 {
        let rows = self
            .client
            .run(&format!(
                "SELECT VALUE dirty FROM type::record('vector_meta', '{table}');"
            ))
            .await
            .unwrap();
        rows[0]
            .as_array()
            .and_then(|a| a.first())
            .and_then(serde_json::Value::as_i64)
            .unwrap_or(0)
    }
}

/// C0b2 (e): the swap subtracts the count it read (`dirty - changed`) as one statement, so a write
/// that bumps `dirty` while the index builds is still counted afterwards:
/// final dirty = rows written in all - rows the swap consumed.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn writes_during_a_swap_are_not_subtracted_away() {
    const FIRST: usize = 3;
    const DURING: usize = 20;
    let f = fixture("sw_overlap").await;
    for i in 0..FIRST {
        f.note(&format!("/v/first-{i}.md"), "oh-my-boring", i).await;
    }
    assert_eq!(f.dirty("chunk").await, FIRST as i64);
    let writer = async {
        for i in 0..DURING {
            f.note(&format!("/v/during-{i}.md"), "oh-my-boring", 10 + i)
                .await;
        }
    };
    let (swaps, ()) = tokio::join!(f.client.swap_if_changed(), writer);
    let swaps = swaps.expect("swap run");
    let Swap::Swapped { changed, .. } = of(&swaps, "chunk") else {
        panic!("chunk must swap: {swaps:?}");
    };
    let written = (FIRST + DURING) as i64;
    let left = f.dirty("chunk").await;
    println!("written {written}, swap consumed {changed}, dirty left {left}");
    assert_eq!(left, written - *changed as i64);
    assert!(
        *changed >= FIRST as u64,
        "the swap consumed at least what it saw"
    );
}

/// C0b2 (e): `split_sql` deletes claim rows (and so their vectors). The next swap has to see them.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_split_merge_leaves_the_claim_index_dirty() {
    let f = fixture("sw_split").await;
    let mut emb = vec![0.0_f32; DIM];
    emb[0] = 1.0;
    for k in 0..3 {
        let path = format!("/v/sp-{k}.md");
        f.client
            .run(&format!(
                "UPSERT type::record('document', {p}) CONTENT {{cid: {p}, source_path: {p}, origin: 'personal', \
                 project: 'sw', kind: 'note', tags: [], sha: 'sha', extracted_sha: '', updated_us: 0, author: 'agent'}};",
                p = json!(path)
            ))
            .await
            .expect("document");
        let subject = ["Sw Split", "sw-split", "sw_split"][k];
        f.client
            .claim_slot_upsert(&drudge::store::ClaimWrite {
                subject,
                predicate: &format!("p{k}"),
                value: "a long enough value to count as a statement",
                source_path: &path,
                valid_from_us: 100 + k as i64,
                embedding: &emb,
                kind: "fact",
                confidence: "certain",
                anchor: None,
                anchor_hash: None,
                anchor_symbol: None,
            })
            .await
            .expect("claim");
    }
    // Consume what the writes bumped, so only the merge can make the counter rise again.
    let first = f.swap().await;
    assert!(
        matches!(of(&first, "claim"), Swap::Swapped { .. }),
        "{first:?}"
    );
    assert_eq!(f.dirty("claim").await, 0);

    let merged = f
        .client
        .merge_split_subject("sw-split", drudge::store::SplitStanding::Owner)
        .await
        .expect("merge")
        .expect("a group to merge");
    assert_eq!(merged.deleted_rows, 3);

    let after = f.swap().await;
    println!("{}", of(&after, "claim").line());
    assert!(
        matches!(of(&after, "claim"), Swap::Swapped { .. }),
        "claim rows were deleted but the swap skipped: {after:?}"
    );
}
