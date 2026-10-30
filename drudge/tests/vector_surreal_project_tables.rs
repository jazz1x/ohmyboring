//! C-SURCOMBO1 S2: every chunk is written to the global `chunk` table and to its project's table
//! `chunk_p_<slug>`; a project-filtered KNN reads the project table when it exists.
//!
//! Needs a throwaway pgvector (its document and claim tables are emptied on each run) and a
//! throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test vector_surreal_project_tables -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::too_many_lines)]

use std::time::SystemTime;

use drudge::config::SurrealConfig;
use drudge::frontmatter::FrontMatter;
use drudge::store::{Doc, ProjectTable, ProjectTableError, Store, SurrealClient, VectorRoute};
use drudge::vector::VectorIndex;
use serde_json::{Value, json};
use tokio_postgres::NoTls;

const DIM: usize = 1024;

fn vec_at(i: usize) -> Vec<f32> {
    let mut v = vec![0.0; DIM];
    v[i % DIM] = 1.0;
    v
}

fn front(path: &str, project: &str) -> FrontMatter {
    FrontMatter {
        origin: "personal".to_string(),
        project: project.to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("s2".to_string()),
        ..Default::default()
    }
}

struct Fixture {
    store: Store,
    client: SurrealClient,
    db: tokio_postgres::Client,
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
        db,
    }
}

impl Fixture {
    async fn note(&self, path: &str, project: &str, hot: usize) {
        let front = front(path, project);
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

    async fn cids(&self, sql: &str) -> Vec<String> {
        let rows = self.client.run(sql).await.unwrap();
        let mut cids: Vec<String> = serde_json::from_value::<Vec<Vec<Value>>>(Value::Array(rows))
            .unwrap()[0]
            .iter()
            .map(|v| v["cid"].as_str().unwrap().to_string())
            .collect();
        cids.sort();
        cids
    }

    async fn pg_cids(&self, project: Option<&str>) -> Vec<String> {
        let mut cids: Vec<String> = self
            .db
            .query(
                "SELECT id FROM chunk WHERE $1::text IS NULL OR project = $1",
                &[&project],
            )
            .await
            .unwrap()
            .iter()
            .map(|r| r.get(0))
            .collect();
        cids.sort();
        cids
    }
}

#[test]
fn slug_with_a_bad_char_is_rejected_not_mapped() {
    assert_eq!(
        ProjectTable::parse("my proj"),
        Err(ProjectTableError::BadChar {
            slug: "my proj".into(),
            bad: ' '
        })
    );
    assert!(matches!(
        ProjectTable::parse("Alpha"),
        Err(ProjectTableError::BadChar { bad: 'A', .. })
    ));
    assert!(matches!(
        ProjectTable::parse("a`; REMOVE TABLE chunk; --"),
        Err(ProjectTableError::BadChar { .. })
    ));
    assert_eq!(ProjectTable::parse(""), Err(ProjectTableError::Empty));
    assert_eq!(
        ProjectTable::parse("alpha_2").unwrap().name(),
        "chunk_p_alpha_2"
    );
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn mirror_keeps_global_and_project_tables_equal() {
    let f = fixture("pt_equal").await;
    f.note("/v/a1.md", "alpha", 1).await;
    f.note("/v/a2.md", "alpha", 2).await;
    f.note("/v/b1.md", "beta", 3).await;
    f.note("/v/x1.md", "bad slug", 4).await;

    let global = f.cids("SELECT cid FROM chunk;").await;
    let alpha = f.cids("SELECT cid FROM chunk_p_alpha;").await;
    let beta = f.cids("SELECT cid FROM chunk_p_beta;").await;
    println!(
        "global {}/{} alpha {}/{} beta {}/{}",
        global.len(),
        f.pg_cids(None).await.len(),
        alpha.len(),
        f.pg_cids(Some("alpha")).await.len(),
        beta.len(),
        f.pg_cids(Some("beta")).await.len()
    );
    assert_eq!(global, f.pg_cids(None).await);
    assert_eq!(alpha, f.pg_cids(Some("alpha")).await);
    assert_eq!(beta, f.pg_cids(Some("beta")).await);

    // A note changes project and another is deleted: reconcile reaches the project tables.
    f.note("/v/a2.md", "beta", 2).await;
    f.store.delete_document("/v/a1.md").await.expect("delete");
    assert_eq!(
        f.cids("SELECT cid FROM chunk_p_alpha;").await,
        f.pg_cids(Some("alpha")).await
    );
    assert_eq!(
        f.cids("SELECT cid FROM chunk_p_beta;").await,
        f.pg_cids(Some("beta")).await
    );
    assert_eq!(
        f.cids("SELECT cid FROM chunk;").await,
        f.pg_cids(None).await
    );

    // A project whose table appears after its notes were written is created whole.
    f.client
        .run("REMOVE TABLE chunk_p_beta;")
        .await
        .expect("drop the project table");
    f.note("/v/b2.md", "beta", 5).await;
    assert_eq!(
        f.cids("SELECT cid FROM chunk_p_beta;").await,
        f.pg_cids(Some("beta")).await,
        "a recreated project table holds every chunk of the project, not only the new one"
    );
}

/// `-` is a real slug character (`oh-my-boring`); it is never mapped to `_`, so `oh-my-boring` and
/// `oh_my_boring` are two tables.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_slug_with_a_hyphen_gets_its_own_table_and_is_read() {
    let f = fixture("pt_hyphen").await;
    f.note("/v/h1.md", "oh-my-boring", 1).await;
    f.note("/v/u1.md", "oh_my_boring", 2).await;
    assert_eq!(
        f.cids("SELECT cid FROM `chunk_p_oh-my-boring`;").await,
        ["/v/h1.md#0"]
    );
    assert_eq!(
        f.cids("SELECT cid FROM chunk_p_oh_my_boring;").await,
        ["/v/u1.md#0"]
    );
    f.client
        .run("DELETE chunk WHERE cid = \"/v/h1.md#0\";")
        .await
        .expect("delete from global only");
    let hits = f
        .store
        .vector_search_filtered(&vec_at(1), 5, Some("oh-my-boring"), None)
        .await
        .expect("filtered search");
    assert!(
        hits.iter().any(|h| h.id == "/v/h1.md#0"),
        "the hyphen slug's query must read its own table"
    );
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn project_filtered_query_reads_the_project_table() {
    let f = fixture("pt_read").await;
    f.note("/v/a1.md", "alpha", 1).await;
    f.note("/v/b1.md", "beta", 2).await;

    // Make the two tables disagree on purpose: the row leaves the global table only. A filtered
    // query that still finds it has read the project table; an unfiltered one has not.
    f.client
        .run("DELETE chunk WHERE cid = \"/v/a1.md#0\";")
        .await
        .expect("delete from global only");
    let filtered = f
        .store
        .vector_search_filtered(&vec_at(1), 5, Some("alpha"), None)
        .await
        .expect("filtered search");
    let unfiltered = f.store.vector_search(&vec_at(1), 5).await.expect("search");
    assert!(
        filtered.iter().any(|h| h.id == "/v/a1.md#0"),
        "project query must read chunk_p_alpha"
    );
    assert!(
        unfiltered.iter().all(|h| h.id != "/v/a1.md#0"),
        "global query reads the global table"
    );

    // No project table: the global table answers.
    f.client
        .run("REMOVE TABLE chunk_p_beta;")
        .await
        .expect("drop beta table");
    let beta = f
        .store
        .vector_search_filtered(&vec_at(2), 5, Some("beta"), None)
        .await
        .expect("filtered search without a table");
    assert!(beta.iter().any(|h| h.id == "/v/b1.md#0"));
}
