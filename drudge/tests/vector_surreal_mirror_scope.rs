//! C-SURMIRROR1: `SurrealClient::mirror` of a wide scope. Surreal's parser refuses an `OR` chain of
//! about 120 terms ("nests or chains operators too deeply"); the claim slot read built one term per
//! slot, so a scope of 119 notes (one open claim each) already failed and left the chunks written
//! and the claims not.
//!
//! Needs a throwaway pgvector (its document and claim tables are emptied on each run) and a
//! throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test vector_surreal_mirror_scope -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::too_many_lines)]

use std::time::{Duration, SystemTime};

use drudge::config::SurrealConfig;
use drudge::frontmatter::FrontMatter;
use drudge::store::{Doc, Scope, Store, SurrealClient};
use serde_json::{Value, json};
use tokio_postgres::NoTls;

const DIM: usize = 1024;

fn vec_at(i: usize) -> Vec<f32> {
    let mut v = vec![0.0; DIM];
    v[i % DIM] = 1.0;
    v
}

fn front(path: &str) -> FrontMatter {
    FrontMatter {
        origin: "personal".to_string(),
        project: "mirror-scope".to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("scope".to_string()),
        ..Default::default()
    }
}

/// `(key, source_path, index of the 1.0 in the embedding)`: what the row says, as one string.
fn row(key: &str, path: &str, embedding: &[f64]) -> String {
    let hot = embedding
        .iter()
        .position(|x| (x - 1.0).abs() < 1e-6)
        .unwrap();
    format!("{key}|{path}|{hot}")
}

/// Write `n` notes into pg (one chunk and one open claim each, plain pg store), then mirror the
/// whole scope in one call and return (pg rows, Surreal rows) for chunks and for open claims.
async fn mirror_scope(namespace: &str, n: usize) -> [(Vec<String>, Vec<String>); 2] {
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").expect("BORING_TEST_DATABASE_URL");
    let url = std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL");
    let config: SurrealConfig =
        serde_json::from_value(json!({"url": url, "namespace": namespace, "database": namespace}))
            .unwrap();
    let client = SurrealClient::new(config.settings(&|k| std::env::var(k).ok()).unwrap()).unwrap();
    let store = Store::open(&dsn, DIM).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db.batch_execute("DELETE FROM claim; DELETE FROM document;")
        .await
        .unwrap();
    client.fill_from(&store).await.expect("fill of an empty pg");

    let mut paths = Vec::new();
    for i in 0..n {
        let path = format!("/vault/wiki/scope-{n}-{i:05}.md");
        store
            .upsert_document(&front(&path), "sha", SystemTime::now())
            .await
            .expect("document");
        store
            .upsert_chunk(&Doc {
                id: format!("{path}#0"),
                content: format!("chunk {i}"),
                embedding: vec_at(i),
                front: front(&path),
                chunk_idx: 0,
            })
            .await
            .expect("chunk");
        store
            .upsert_claim(
                &format!("slot-{n}-{i:05}"),
                "observed",
                &format!("a long enough value from {path}"),
                &path,
                SystemTime::now() - Duration::from_hours(1),
                &vec_at(i + 3),
                "fact",
                "certain",
            )
            .await
            .expect("claim");
        paths.push(path);
    }

    client
        .mirror(&store, &Scope::notes(paths))
        .await
        .expect("mirror of the whole scope");

    let pg_chunks: Vec<String> = db
        .query("SELECT id, source_path, embedding::text FROM chunk", &[])
        .await
        .unwrap()
        .iter()
        .map(|r| {
            row(
                &r.get::<_, String>(0),
                &r.get::<_, String>(1),
                &parse(&r.get::<_, String>(2)),
            )
        })
        .collect();
    let pg_claims: Vec<String> = db
        .query(
            "SELECT subject || '|' || predicate || '|' ||
                    (extract(epoch from valid_from) * 1000000)::bigint,
                    source_path, embedding::text
             FROM claim WHERE superseded_at IS NULL",
            &[],
        )
        .await
        .unwrap()
        .iter()
        .map(|r| {
            row(
                &r.get::<_, String>(0),
                &r.get::<_, String>(1),
                &parse(&r.get::<_, String>(2)),
            )
        })
        .collect();
    let surreal = |sql: &str| {
        let sql = sql.to_string();
        let client = client.clone();
        async move {
            let rows = client.run(&sql).await.unwrap();
            serde_json::from_value::<Vec<Vec<Value>>>(Value::Array(rows)).unwrap()[0].clone()
        }
    };
    let s_chunks = surreal("SELECT cid, source_path, embedding FROM chunk;").await;
    let s_claims = surreal("SELECT cid, source_path, embedding FROM claim;").await;
    let fold = |rows: Vec<Value>| -> Vec<String> {
        rows.iter()
            .map(|v| {
                let e: Vec<f64> = serde_json::from_value(v["embedding"].clone()).unwrap();
                row(
                    &v["cid"].as_str().unwrap().replace('\u{1f}', "|"),
                    v["source_path"].as_str().unwrap(),
                    &e,
                )
            })
            .collect()
    };
    let sorted = |mut v: Vec<String>| {
        v.sort();
        v
    };
    [
        (sorted(pg_chunks), sorted(fold(s_chunks))),
        (sorted(pg_claims), sorted(fold(s_claims))),
    ]
}

fn parse(text: &str) -> Vec<f64> {
    text.trim_matches(|c| c == '[' || c == ']')
        .split(',')
        .map(|x| x.parse().unwrap())
        .collect()
}

async fn assert_scope_mirrors(namespace: &str, n: usize) {
    let [(pg_chunks, s_chunks), (pg_claims, s_claims)] = mirror_scope(namespace, n).await;
    let same = |a: &[String], b: &[String]| a.iter().filter(|x| b.contains(x)).count();
    println!(
        "n={n}: chunk {}/{} claim {}/{}",
        same(&pg_chunks, &s_chunks),
        pg_chunks.len(),
        same(&pg_claims, &s_claims),
        pg_claims.len()
    );
    assert_eq!(pg_chunks.len(), n);
    assert_eq!(pg_claims.len(), n);
    assert_eq!(pg_chunks, s_chunks, "chunk (key, content) differ");
    assert_eq!(pg_claims, s_claims, "open claim (key, content) differ");
}

/// 119 is the first scope whose claim slot read exceeded Surreal's chain limit; 120 is not a
/// multiple of FILL_BATCH either way; 1,280 is the size the defect was seen at.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn mirror_of_119_notes_copies_every_row() {
    assert_scope_mirrors("ms_119", 119).await;
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn mirror_of_120_notes_copies_every_row() {
    assert_scope_mirrors("ms_120", 120).await;
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn mirror_of_1280_notes_copies_every_row() {
    assert_scope_mirrors("ms_1280", 1280).await;
}
