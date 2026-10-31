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
    clippy::panic
)]

use std::time::SystemTime;

use drudge::config::SurrealConfig;
use drudge::frontmatter::FrontMatter;
use drudge::store::{Doc, Store, SurrealClient, Swap, VectorRoute};
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
        self.store
            .upsert_document(&front, "sha", SystemTime::now())
            .await
            .expect("document");
        self.store
            .upsert_chunk(&Doc {
                id: format!("{path}#0"),
                content: format!("chunk of {path}"),
                embedding: vec_at(hot),
                front,
                chunk_idx: 0,
            })
            .await
            .expect("chunk");
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
        ["chunk_embedding_g1", "chunk_source_path"]
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
        ["chunk_embedding", "chunk_embedding_g1", "chunk_source_path"],
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
