//! C-SURALL2 C0b1 (j): a row written only into Surreal (document / chunk / node / edge / claim_row /
//! claim) must still be there after the sync's `catch_up_surreal`. Before each bundle `catch_up`
//! deleted every Surreal key pg lacks in that table.
//!
//! Prints `C0b1 survive <table>: before 1, after N`. Needs a throwaway pgvector and SurrealDB:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test surreal_c0b1_survive -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::time::{SystemTime, UNIX_EPOCH};

use drudge::config::SurrealConfig;
use drudge::frontmatter::FrontMatter;
use drudge::store::{Doc, Store, SurrealClient, VectorRoute};
use serde_json::json;
use tokio_postgres::NoTls;

const DIM: usize = 1024;

fn env(name: &str) -> String {
    std::env::var(name).expect(name)
}

fn nanos() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos()
}

fn client_at(namespace: &str) -> SurrealClient {
    let config: SurrealConfig = serde_json::from_value(
        json!({"url": env("BORING_TEST_SURREAL_URL"), "namespace": namespace, "database": namespace}),
    )
    .unwrap();
    SurrealClient::new(config.settings(&|n| std::env::var(n).ok()).unwrap()).unwrap()
}

async fn count(client: &SurrealClient, table: &str, cid: &str) -> usize {
    let reply = client
        .run(&format!(
            "SELECT count() AS n FROM {table} WHERE cid = '{cid}' GROUP ALL;"
        ))
        .await
        .unwrap();
    usize::try_from(reply[0][0]["n"].as_u64().unwrap_or(0)).unwrap()
}

fn floats() -> String {
    let mut v = vec!["0.0"; DIM];
    v[0] = "0.9";
    format!("[{}]", v.join(","))
}

/// One row of `table` written only into Surreal, then one `catch_up`: the row must still be there.
/// All four tables are written so that each test starts from the same Surreal state.
async fn survive(table: &str) {
    let dsn = env("BORING_TEST_DATABASE_URL");
    let pg = Store::open(&dsn, DIM).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.unwrap();
    tokio::spawn(conn);
    db.batch_execute(
        "DELETE FROM claim; DELETE FROM edge; DELETE FROM node; DELETE FROM document;",
    )
    .await
    .unwrap();
    let front = FrontMatter {
        origin: "personal".to_owned(),
        project: "c0b1".to_owned(),
        kind: "note".to_owned(),
        source_path: "/vault/wiki/c0b1-pg.md".to_owned(),
        ..Default::default()
    };
    pg.upsert_document(&front, "sha-pg", SystemTime::now())
        .await
        .unwrap();
    let mut embedding = vec![0.0_f32; DIM];
    embedding[0] = 0.9;
    pg.upsert_chunk(&Doc {
        id: "/vault/wiki/c0b1-pg.md#0".to_owned(),
        content: "pg row".to_owned(),
        embedding,
        front,
        chunk_idx: 0,
    })
    .await
    .unwrap();

    let client = client_at(&format!("c0b1_survive_{}", nanos()));
    let tables = client.fill_from(&pg).await.expect("fill");
    assert!(tables.iter().all(drudge::store::FillTable::same), "fill");
    let routed = Store::open(&dsn, DIM)
        .await
        .unwrap()
        .with_vector_route(VectorRoute::Surreal(client.clone()));

    // Written the way a Surreal-native write would: into Surreal, never into pg.
    let path = "/vault/wiki/c0b1-surreal.md";
    client
        .run(&format!(
            "CREATE document:`{path}` CONTENT {{cid: '{path}', source_path: '{path}', origin: 'personal', \
             project: 'c0b1', kind: 'note', tags: [], sha: 'sha-s', extracted_sha: '', updated_us: 1, \
             author: 'unknown'}};
             CREATE node:`concept:c0b1` CONTENT {{cid: 'concept:c0b1', kind: 'concept', label: 'c0b1'}};
             CREATE edge:`e1` CONTENT {{cid: 'e1', src: 'doc:{path}', dst: 'concept:c0b1', kind: 'about'}};
             CREATE chunk:`{path}#0` CONTENT {{cid: '{path}#0', source_path: '{path}', project: 'c0b1', \
             doc_updated_us: 1, content: 'surreal only', origin: 'personal', embedding: {emb}}};
             CREATE claim_row:`c1` CONTENT {{cid: 'c1', subject: 'c0b1', predicate: 'p', value: 'v', \
             source_path: '{path}', valid_from_us: 1, kind: 'fact', confidence: 'certain', \
             era: 'unanchored', origin: 'personal', project: 'c0b1', via: 'surreal', embedding: {emb}}};
             CREATE claim:`c1` CONTENT {{cid: 'c1', subject: 'c0b1', predicate: 'p', valid_from_us: 1, \
             source_path: '{path}', origin: 'personal', project: 'c0b1', kind: 'fact', embedding: {emb}}};",
            emb = floats()
        ))
        .await
        .unwrap();
    let rows = [
        ("document", path.to_owned()),
        ("node", "concept:c0b1".to_owned()),
        ("edge", "e1".to_owned()),
        ("chunk", format!("{path}#0")),
        ("claim_row", "c1".to_owned()),
        ("claim", "c1".to_owned()),
    ];
    for (table, cid) in &rows {
        assert_eq!(count(&client, table, cid).await, 1, "{table} written");
    }

    routed.catch_up_surreal().await.expect("catch_up");

    let (_, cid) = rows.iter().find(|(t, _)| *t == table).expect("table");
    let after = count(&client, table, cid).await;
    println!("C0b1 survive {table}: before 1, after {after}");
    assert_eq!(after, 1, "catch_up deleted the Surreal-only {table} row");
}

// Bundle 1 moved document and chunk, bundle 2 node and edge, bundle 3 claim_row and claim: all six
// are GREEN on head (RED on f46a994 for the two claim tables, where catch_up still copied pg over
// them).

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_document_written_only_to_surreal_survives_catch_up() {
    survive("document").await;
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_chunk_written_only_to_surreal_survives_catch_up() {
    survive("chunk").await;
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_node_written_only_to_surreal_survives_catch_up() {
    survive("node").await;
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn an_edge_written_only_to_surreal_survives_catch_up() {
    survive("edge").await;
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_claim_row_written_only_to_surreal_survives_catch_up() {
    survive("claim_row").await;
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_vector_claim_written_only_to_surreal_survives_catch_up() {
    survive("claim").await;
}
