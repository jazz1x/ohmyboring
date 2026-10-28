//! A project-filtered vector search returns as many rows as exist, up to the pool, even when the
//! filter's rows are all farther than 40 other-project chunks (pgvector's `hnsw.ef_search`
//! window). Plain HNSW post-filtering would return none of them; iterative scan fills the pool.
//!
//! Same assertions run against the pgvector route and the Surreal route.
//!
//! Needs a throwaway pgvector (`BORING_TEST_DATABASE_URL`) and, for the Surreal case, a throwaway
//! SurrealDB (`BORING_TEST_SURREAL_URL`, password in `BORING_SURREAL_PASSWORD`). Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:t@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test vector_iterative_scan -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::too_many_lines)]

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::config::SurrealConfig;
use drudge::frontmatter::FrontMatter;
use drudge::store::{Doc, Store, SurrealClient, VectorRoute};
use drudge::vector::VectorIndex;
use serde_json::json;
use tokio_postgres::NoTls;

const DIM: usize = 1024;
const POOL: usize = 20;
/// More than the 40-candidate window, all nearer to the query than every target chunk.
const DECOYS: usize = 100;
/// More than the pool, so the right answer is exactly `POOL` rows.
const TARGETS: usize = 30;

fn tilted_vec(tilt_at: usize, cos: f32, salt: usize) -> Vec<f32> {
    let mut v = vec![0.0; DIM];
    v[0] = cos;
    v[tilt_at] = (1.0 - cos * cos).sqrt();
    v[10 + salt % 1000] = 0.001;
    v
}

async fn put(pg: &Store, path: &str, project: &str, emb: Vec<f32>) {
    let fm = FrontMatter {
        origin: "personal".to_string(),
        project: project.to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("iterative scan fixture".to_string()),
        ..Default::default()
    };
    pg.upsert_document(&fm, "sha", SystemTime::now() - Duration::from_hours(1))
        .await
        .expect("doc");
    pg.upsert_chunk(&Doc {
        id: format!("{path}#0"),
        content: "chunk".to_string(),
        embedding: emb,
        front: fm,
        chunk_idx: 0,
    })
    .await
    .expect("chunk");
}

/// Returns the pgvector store (default route) and the target project name.
async fn fixture(tag: &str) -> (Store, String) {
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").expect("BORING_TEST_DATABASE_URL");
    // At 130 rows the planner prefers a seq scan, which is exact and would hide the bug. At
    // production size it takes the HNSW index; make that choice here before any connection opens.
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db.batch_execute(
        "DO $$ BEGIN EXECUTE format('ALTER DATABASE %I SET enable_seqscan = off', current_database()); END $$;",
    )
    .await
    .expect("planner setting");
    let pg = Store::open(&dsn, DIM).await.expect("open store");
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let target = format!("iter-{tag}-{nanos}");
    let decoy = format!("decoy-{tag}-{nanos}");
    for i in 0..DECOYS {
        #[allow(clippy::cast_precision_loss)]
        let cos = 0.99 - i as f32 * 0.0002;
        let path = format!("/vault/wiki/iter-{tag}-{nanos}-d{i}.md");
        put(&pg, &path, &decoy, tilted_vec(1 + i % 3, cos, i)).await;
    }
    for j in 0..TARGETS {
        #[allow(clippy::cast_precision_loss)]
        let cos = 0.30 + j as f32 * 0.001;
        let path = format!("/vault/wiki/iter-{tag}-{nanos}-t{j}.md");
        put(&pg, &path, &target, tilted_vec(1 + j % 3, cos, j)).await;
    }
    (pg, target)
}

async fn assert_fills_pool(index: &impl VectorIndex, project: &str, route: &str) {
    let mut query = vec![0.0_f32; DIM];
    query[0] = 1.0;

    let filtered = index
        .vector_search_filtered(&query, POOL, Some(project), None)
        .await
        .expect("filtered search");
    println!("{route}: filtered n={} pool={POOL}", filtered.len());
    assert_eq!(
        filtered.len(),
        POOL.min(TARGETS),
        "{route}: project filter must fill the pool (n vs pool)"
    );
    assert!(
        filtered.iter().all(|h| h.project == project),
        "{route}: only the target project"
    );
    assert!(
        filtered.windows(2).all(|w| w[0].dist <= w[1].dist),
        "{route}: results must be in non-decreasing dist order"
    );

    let unfiltered = index
        .vector_search_filtered(&query, POOL, None, None)
        .await
        .expect("unfiltered search");
    assert_eq!(unfiltered.len(), POOL, "{route}: unfiltered returns k");
}

#[tokio::test]
#[ignore = "needs throwaway pg: see the run command at the top of this file"]
async fn pgvector_project_filter_fills_the_pool() {
    let (pg, project) = fixture("pg").await;
    assert_fills_pool(&pg, &project, "pgvector").await;
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command at the top of this file"]
async fn surreal_project_filter_fills_the_pool() {
    let url = std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL");
    let config: SurrealConfig = serde_json::from_value(json!({
        "url": url, "namespace": "iterscan", "database": "iterscan"
    }))
    .unwrap();
    let client = SurrealClient::new(config.settings(&|n| std::env::var(n).ok()).unwrap()).unwrap();
    let (pg, project) = fixture("sr").await;
    let tables = client.fill_from(&pg).await.expect("fill");
    assert!(tables.iter().all(drudge::store::FillTable::same), "fill");
    let store = pg.with_vector_route(VectorRoute::Surreal(client));
    assert_fills_pool(&store, &project, "surreal").await;
}
