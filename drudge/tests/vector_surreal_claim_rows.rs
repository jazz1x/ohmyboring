//! C-SURALL2 A3: the `claim_row` copy holds every claim row, sealed ones included, and a value that
//! moved only in pg (a seal written straight to pg, as the Python door does) is found and repaired
//! by `catch_up`, which a key-only compare would miss. Also the register read over the copy.
//!
//! Needs a throwaway pgvector (its document and claim tables are emptied on each run) and a
//! throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test vector_surreal_claim_rows -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::time::{Duration, SystemTime};

use drudge::config::SurrealConfig;
use drudge::frontmatter::FrontMatter;
use drudge::store::{RegisterAsk, Scope, Store, SurrealClient};
use serde_json::json;
use tokio_postgres::NoTls;

const DIM: usize = 1024;

fn front(path: &str) -> FrontMatter {
    FrontMatter {
        origin: "personal".to_string(),
        project: "claim-rows".to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("rows".to_string()),
        ..Default::default()
    }
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_seal_written_only_to_pg_is_repaired_and_the_register_agrees() {
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").expect("BORING_TEST_DATABASE_URL");
    let url = std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL");
    let config: SurrealConfig = serde_json::from_value(
        json!({"url": url, "namespace": "claim_rows", "database": "claim_rows"}),
    )
    .unwrap();
    let client = SurrealClient::new(config.settings(&|k| std::env::var(k).ok()).unwrap()).unwrap();
    let store = Store::open(&dsn, DIM).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db.batch_execute("DELETE FROM claim; DELETE FROM document;")
        .await
        .unwrap();
    client.fill_from(&store).await.expect("fill of an empty pg");

    let path = "/vault/wiki/claim-rows.md";
    store
        .upsert_document(&front(path), "sha", SystemTime::now())
        .await
        .expect("document");
    let mut emb = vec![0.0; DIM];
    emb[0] = 1.0;
    for (i, predicate) in ["goal", "scope"].iter().enumerate() {
        store
            .upsert_claim(
                "claim-rows-subject",
                predicate,
                "a long enough value to count as a statement",
                path,
                SystemTime::now() - Duration::from_hours(1 + i as u64),
                &emb,
                "decision",
                "certain",
            )
            .await
            .expect("claim");
    }
    client
        .mirror(&store, &Scope::notes([path.to_owned()]))
        .await
        .expect("mirror");
    let first = client.claim_row_diff(&store).await.expect("diff");
    assert!(first.same(), "{}", first.line());
    assert_eq!(first.pg_rows, 2);

    db.batch_execute("UPDATE claim SET superseded_at = now() WHERE predicate = 'scope'")
        .await
        .unwrap();
    let broken = client.claim_row_diff(&store).await.expect("diff");
    assert_eq!(broken.different.len(), 1, "{}", broken.line());

    client.catch_up(&store).await.expect("catch_up");
    let healed = client.claim_row_diff(&store).await.expect("diff");
    assert!(healed.same(), "{}", healed.line());

    let kinds = vec!["decision".to_owned()];
    let ask = RegisterAsk {
        k: 10,
        project: Some("claim-rows"),
        kinds: Some(&kinds),
        exclude_origins: &[],
    };
    let surreal = client.register_rows(&ask).await.expect("register");
    assert_eq!(surreal.total_matching, 1);
    assert_eq!(surreal.rows[0].predicate, "goal");
}
