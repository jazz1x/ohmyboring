//! C-SURALL2 A3 as C0b1 bundle 3 left it: `fill_from` (the Z4 switch-over tool) is the only copy
//! of pg's claims into Surreal. `catch_up` copies no claim row any more: a seal written only to
//! pg is left alone, and the compare (`claim_row_diff`, `compare`) is what sees the difference.
//!
//! Needs a throwaway pgvector (its document and claim tables are emptied on each run; the 16-dim
//! test creates and drops its own `claim_rows_dim16` database beside it) and a
//! throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test vector_surreal_claim_rows -- --ignored --nocapture --test-threads=1
#![allow(
    clippy::expect_used,
    clippy::unwrap_used,
    clippy::too_many_lines,
    clippy::cast_precision_loss,
    clippy::cast_possible_wrap
)]

use std::time::{Duration, SystemTime};

use drudge::config::SurrealConfig;
use drudge::frontmatter::FrontMatter;
use drudge::store::{RegisterAsk, Store, SurrealClient};
use serde_json::json;
use tokio_postgres::NoTls;

const DIM: usize = 1024;

/// A database of its own for the 16-dim test: the vector column's dimension is fixed by whichever
/// schema was created first, so 16-dim inserts into the 1024-dim shared database fail.
async fn sixteen_dim_database() -> String {
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").expect("BORING_TEST_DATABASE_URL");
    let (server, _) = dsn.rsplit_once('/').expect("dsn ends in /<database>");
    let name = "claim_rows_dim16";
    let (admin, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    admin
        .batch_execute(&format!("DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        .await
        .expect("drop the 16-dim database");
    admin
        .batch_execute(&format!("CREATE DATABASE {name}"))
        .await
        .expect("create the 16-dim database");
    format!("{server}/{name}")
}

/// C0b1: a note's `document` row lives in Surreal (the engine writes it there), and the claim
/// rows `fill_from` copies read the note's origin and project from it. These tests seed pg, so the
/// Surreal row is put by hand the way the engine's write puts it.
async fn put_doc(client: &SurrealClient, fm: &FrontMatter) {
    let q = |s: &str| serde_json::to_string(s).unwrap();
    let p = q(&fm.source_path);
    client
        .run(&format!(
            "UPSERT type::record('document', {p}) CONTENT {{cid: {p}, source_path: {p}, origin: {o}, \
             project: {pr}, kind: {k}, tags: [], sha: 'sha', extracted_sha: '', \
             updated_us: 1700000000000000, author: 'unknown', via: 'surreal'}};",
            o = q(&fm.origin),
            pr = q(&fm.project),
            k = q(&fm.kind)
        ))
        .await
        .expect("surreal document");
}

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

/// Old contract: a seal written only to pg was found by the value compare and repaired by
/// `catch_up`. New contract: `catch_up` repairs nothing and returns `(0, 0)`; the compare still
/// sees the difference, and the register read over Surreal's rows answers from Surreal alone.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_seal_written_only_to_pg_is_seen_by_the_compare_and_not_repaired_by_catch_up() {
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
    put_doc(&client, &front(path)).await;
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
    client.fill_from(&store).await.expect("fill");
    let first = client.claim_row_diff(&store).await.expect("diff");
    assert!(first.same(), "{}", first.line());
    assert_eq!(first.pg_rows, 2);

    db.batch_execute("UPDATE claim SET superseded_at = now() WHERE predicate = 'scope'")
        .await
        .unwrap();
    let broken = client.claim_row_diff(&store).await.expect("diff");
    assert_eq!(broken.different.len(), 1, "{}", broken.line());

    let moved = client.catch_up(&store).await.expect("catch_up");
    assert_eq!(moved, (0, 0), "catch_up copies no claim row");
    let still = client.claim_row_diff(&store).await.expect("diff");
    assert_eq!(
        still.different.len(),
        1,
        "catch_up must not repair a pg-only seal: {}",
        still.line()
    );

    let kinds = vec!["decision".to_owned()];
    let ask = RegisterAsk {
        k: 10,
        project: Some("claim-rows"),
        kinds: Some(&kinds),
        exclude_origins: &[],
    };
    let surreal = client.register_rows(&ask).await.expect("register");
    assert_eq!(
        surreal.total_matching, 2,
        "the register reads Surreal's rows, where the pg-only seal never arrived"
    );
}

/// 300 open claims, the same in pg and in Surreal (`fill_from` copies them).
async fn three_hundred_open_claims(
    namespace: &str,
) -> (SurrealClient, Store, tokio_postgres::Client) {
    let dsn = sixteen_dim_database().await;
    let url = std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL");
    let config: SurrealConfig =
        serde_json::from_value(json!({"url": url, "namespace": namespace, "database": namespace}))
            .unwrap();
    let client = SurrealClient::new(config.settings(&|k| std::env::var(k).ok()).unwrap()).unwrap();
    let dim = 16;
    let store = Store::open(&dsn, dim).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db.batch_execute("DELETE FROM chunk; DELETE FROM claim; DELETE FROM document;")
        .await
        .unwrap();
    client.fill_from(&store).await.expect("fill of an empty pg");
    let path = "/vault/wiki/claim-stale.md";
    store
        .upsert_document(&front(path), "sha", SystemTime::now())
        .await
        .expect("document");
    put_doc(&client, &front(path)).await;
    db.execute(
        "INSERT INTO claim (subject, predicate, value, source_path, valid_from, embedding, kind, confidence)
         SELECT 'claim-stale-' || lpad(i::text, 4, '0'), 'goal',
                'a long enough value to count as a statement', $1, now() - make_interval(secs => i),
                ('[' || (i / 1000.0)::text || repeat(',0.01', 14) || ',1]')::vector,
                'decision', 'certain'
         FROM generate_series(1, 300) AS i",
        &[&path],
    )
    .await
    .expect("claims");
    client.fill_from(&store).await.expect("fill");
    (client, store, db)
}

/// `catch_up` carries no same-key `stale_at` change either: the pg-only value stays pg-only.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn catch_up_does_not_carry_a_same_key_stale_at_change_into_the_vector_claim_table() {
    let (client, store, db) = three_hundred_open_claims("claim_stale_a").await;
    let changed = db
        .execute(
            "UPDATE claim SET stale_at = now() WHERE subject <= 'claim-stale-0100'",
            &[],
        )
        .await
        .expect("stale");
    assert_eq!(changed, 100);
    client.catch_up(&store).await.expect("catch_up");
    let stale = client
        .run("SELECT VALUE count() FROM claim WHERE stale_us != NONE GROUP ALL;")
        .await
        .expect("count");
    assert_eq!(
        stale[0][0], 0,
        "no claim may carry a stale_us after catch_up (it copies nothing): {stale:?}"
    );
}

/// `compare` (what `vector-check` prints) sees the same-key `stale_at` change.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn compare_sees_a_stale_at_that_differs_under_the_same_key() {
    let (client, store, _db) = three_hundred_open_claims("claim_stale_b").await;
    // Surreal's stale_us moved under 100 keys, pg's did not; claim_row stays equal to pg.
    client
        .run("UPDATE claim SET stale_us = 1 WHERE subject <= 'claim-stale-0100';")
        .await
        .expect("drift");
    let tables = client.compare(&store).await.expect("compare");
    let lines: Vec<String> = tables.iter().map(drudge::store::FillTable::line).collect();
    assert!(
        tables.iter().any(|t| !t.same()),
        "vector-check reports every table the same while 100 stale_at differ under the same key: {lines:?}"
    );
}
