//! C-SURALL2 A3: the `claim_row` copy holds every claim row, sealed ones included, and a value that
//! moved only in pg (a seal written straight to pg, as the Python door does) is found and repaired
//! by `catch_up`, which a key-only compare would miss. Also the register read over the copy.
//!
//! Needs a throwaway pgvector (its document and claim tables are emptied on each run; the 16-dim
//! tests create and drop their own `claim_rows_dim16` database beside it) and a
//! throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test vector_surreal_claim_rows -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::time::{Duration, SystemTime};

use drudge::config::SurrealConfig;
use drudge::frontmatter::FrontMatter;
use drudge::store::{Doc, RegisterAsk, Scope, Store, SurrealClient};
use serde_json::json;
use tokio_postgres::NoTls;

const DIM: usize = 1024;

/// A database of its own for the 16-dim tests: the vector column's dimension is fixed by whichever
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

/// A seal, an insert, a new chunk and a vanished chunk written straight to pg (the Python door has
/// no mirror) leave the vector `claim` and `chunk` tables off by exactly those keys; `catch_up`
/// has to level them, not only `document`/`node`/`edge`/`claim_row`.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn catch_up_levels_the_vector_claim_and_chunk_tables() {
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").expect("BORING_TEST_DATABASE_URL");
    let url = std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL");
    let config: SurrealConfig = serde_json::from_value(
        json!({"url": url, "namespace": "claim_vectors", "database": "claim_vectors"}),
    )
    .unwrap();
    let client = SurrealClient::new(config.settings(&|k| std::env::var(k).ok()).unwrap()).unwrap();
    let store = Store::open(&dsn, DIM).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db.batch_execute("DELETE FROM chunk; DELETE FROM claim; DELETE FROM document;")
        .await
        .unwrap();
    client.fill_from(&store).await.expect("fill of an empty pg");

    let mut emb = vec![0.0; DIM];
    emb[0] = 1.0;
    let kept = "/vault/wiki/claim-vectors-kept.md";
    let added = "/vault/wiki/claim-vectors-added.md";
    store
        .upsert_document(&front(kept), "sha", SystemTime::now())
        .await
        .expect("document");
    store
        .upsert_chunk(&Doc {
            id: format!("{kept}#0"),
            content: "the chunk that will vanish from pg".to_owned(),
            embedding: emb.clone(),
            front: front(kept),
            chunk_idx: 0,
        })
        .await
        .expect("chunk");
    for (i, predicate) in ["goal", "scope"].iter().enumerate() {
        store
            .upsert_claim(
                "claim-vectors-subject",
                predicate,
                "a long enough value to count as a statement",
                kept,
                SystemTime::now() - Duration::from_hours(1 + i as u64),
                &emb,
                "decision",
                "certain",
            )
            .await
            .expect("claim");
    }
    client
        .mirror(&store, &Scope::notes([kept.to_owned()]))
        .await
        .expect("mirror");
    for t in client.compare(&store).await.expect("compare") {
        assert!(t.same(), "baseline: {}", t.line());
    }

    // What the Python door does: pg only, no Surreal write.
    db.batch_execute("UPDATE claim SET superseded_at = now() WHERE predicate = 'scope'")
        .await
        .unwrap();
    store
        .upsert_document(&front(added), "sha", SystemTime::now())
        .await
        .expect("document");
    store
        .upsert_chunk(&Doc {
            id: format!("{added}#0"),
            content: "a chunk only pg holds".to_owned(),
            embedding: emb.clone(),
            front: front(added),
            chunk_idx: 0,
        })
        .await
        .expect("chunk");
    store
        .upsert_claim(
            "claim-vectors-new",
            "goal",
            "a long enough value to count as a statement",
            added,
            SystemTime::now(),
            &emb,
            "decision",
            "certain",
        )
        .await
        .expect("claim");
    db.execute("DELETE FROM chunk WHERE id = $1", &[&format!("{kept}#0")])
        .await
        .unwrap();
    let before = client.compare(&store).await.expect("compare");
    for name in ["chunk", "claim(open)"] {
        let t = before.iter().find(|t| t.table == name).expect("table");
        assert!(
            !t.same(),
            "the write straight to pg must show: {}",
            t.line()
        );
    }

    client.catch_up(&store).await.expect("catch_up");
    for t in client.compare(&store).await.expect("compare") {
        assert!(t.same(), "after catch_up: {}", t.line());
    }
}

/// A catch-up that deletes thousands of vector claims must not feed them to the HNSW index one by
/// one (v3.3.5 keeps ~85 KB of server memory per index delete, measured 2.9 -> 4.5 GiB for 6.1k):
/// it removes the index for the delete and defines the next generation after, which is `ready` and
/// answers again with the rows pg still holds.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_mass_delete_catch_up_rebuilds_the_claim_index_instead_of_deleting_through_it() {
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").expect("BORING_TEST_DATABASE_URL");
    let url = std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL");
    let config: SurrealConfig = serde_json::from_value(
        json!({"url": url, "namespace": "claim_mass", "database": "claim_mass"}),
    )
    .unwrap();
    let client = SurrealClient::new(config.settings(&|k| std::env::var(k).ok()).unwrap()).unwrap();
    let store = Store::open(&dsn, DIM).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db.batch_execute("DELETE FROM chunk; DELETE FROM claim; DELETE FROM document;")
        .await
        .unwrap();
    client.fill_from(&store).await.expect("fill of an empty pg");

    let path = "/vault/wiki/claim-mass.md";
    store
        .upsert_document(&front(path), "sha", SystemTime::now())
        .await
        .expect("document");
    let mut emb = vec![0.0; DIM];
    emb[0] = 1.0;
    let (kept, total) = (50, 2_100);
    for i in 0..total {
        let mut e = emb.clone();
        e[1 + i % 100] = 0.01 * f32::from(u8::try_from(1 + i / 100).expect("at most 22 steps"));
        store
            .upsert_claim(
                &format!("claim-mass-{i:04}"),
                "goal",
                "a long enough value to count as a statement",
                path,
                SystemTime::now() - Duration::from_secs(60 + i as u64),
                &e,
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
    let index_names = |statements: Vec<serde_json::Value>| -> Vec<String> {
        statements[0]["indexes"]
            .as_object()
            .expect("indexes")
            .keys()
            .cloned()
            .collect()
    };
    let before = index_names(client.run("INFO FOR TABLE claim;").await.expect("info"));

    // The Python door's kind of write: pg only, past the mass-delete line.
    db.execute(
        "DELETE FROM claim WHERE subject > $1",
        &[&format!("claim-mass-{:04}", kept - 1)],
    )
    .await
    .unwrap();
    client.catch_up(&store).await.expect("catch_up");

    for t in client.compare(&store).await.expect("compare") {
        assert!(t.same(), "after the mass delete: {}", t.line());
    }
    let after = index_names(client.run("INFO FOR TABLE claim;").await.expect("info"));
    assert_ne!(
        before, after,
        "the index must be a new generation: {after:?}"
    );
    assert!(
        after.iter().any(|n| n.starts_with("claim_embedding_g")),
        "{after:?}"
    );
    let near = client
        .run(&format!(
            "SELECT subject FROM claim WHERE embedding <|3,100|> {};",
            serde_json::to_string(&emb).unwrap()
        ))
        .await
        .expect("knn");
    assert_eq!(near[0].as_array().expect("rows").len(), 3);
}

/// `total` claims in pg (bulk SQL, one scan each), mirrored into Surreal, then all but `kept`
/// deleted in pg only and levelled by `catch_up`: the step "mass delete -> define the next HNSW
/// generation right away". The claim table must hold an HNSW index when `catch_up` returns, and
/// `claim_row` must equal pg's.
async fn mass_delete_leaves_an_index(namespace: &str, total: i64, kept: i64) {
    // A small dimension keeps 150k indexed rows inside the throwaway server (1024 dims ran it out of memory in the fill).
    let dim = 16;
    let dsn = sixteen_dim_database().await;
    let url = std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL");
    let config: SurrealConfig =
        serde_json::from_value(json!({"url": url, "namespace": namespace, "database": namespace}))
            .unwrap();
    let client = SurrealClient::new(config.settings(&|k| std::env::var(k).ok()).unwrap()).unwrap();
    let store = Store::open(&dsn, dim).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db.batch_execute("DELETE FROM chunk; DELETE FROM claim; DELETE FROM document;")
        .await
        .unwrap();
    client.fill_from(&store).await.expect("fill of an empty pg");

    let path = "/vault/wiki/claim-mass.md";
    store
        .upsert_document(&front(path), "sha", SystemTime::now())
        .await
        .expect("document");
    db.execute(
        "INSERT INTO claim (subject, predicate, value, source_path, valid_from, embedding, kind, confidence)
         SELECT 'claim-mass-' || lpad(i::text, 7, '0'), 'goal',
                'a long enough value to count as a statement', $1, now() - make_interval(secs => i),
                ('[' || (i / 1000000.0)::text || repeat(',0.01', $3::int - 2) || ',1]')::vector,
                'decision', 'certain'
         FROM generate_series(1, $2::bigint) AS i",
        &[&path, &total, &i32::try_from(dim).unwrap()],
    )
    .await
    .expect("bulk claims");
    client.fill_from(&store).await.expect("fill");
    db.execute(
        "DELETE FROM claim WHERE subject > $1",
        &[&format!("claim-mass-{kept:07}")],
    )
    .await
    .unwrap();

    let caught = client.catch_up(&store).await;
    let info = client.run("INFO FOR TABLE claim;").await.expect("info");
    let indexes: Vec<String> = info[0]["indexes"]
        .as_object()
        .expect("indexes")
        .keys()
        .cloned()
        .collect();
    assert!(caught.is_ok(), "catch_up: {caught:#?}; indexes {indexes:?}");
    assert!(
        indexes.iter().any(|n| n.starts_with("claim_embedding")),
        "no HNSW index on claim after catch_up: {indexes:?}"
    );
    for t in client.compare(&store).await.expect("compare") {
        assert!(t.same(), "after the mass delete: {}", t.line());
    }
    let diff = client.claim_row_diff(&store).await.expect("diff");
    assert!(diff.same(), "{}", diff.line());
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_mass_delete_then_define_leaves_an_index_after_deleting_all_40k() {
    mass_delete_leaves_an_index("claim_mass_40k", 40_000, 0).await;
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_mass_delete_then_define_leaves_an_index_after_deleting_all_150k() {
    mass_delete_leaves_an_index("claim_mass_150k", 150_000, 0).await;
}

/// 300 open claims, the same in pg and in Surreal.
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

/// End condition 1: one catch-up carries 100 same-key `stale_at` changes into Surreal.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn catch_up_carries_a_same_key_stale_at_change_into_the_vector_claim_table() {
    let (client, store, db) = three_hundred_open_claims("claim_stale_a").await;
    // A write outside `mark_claim_stale`'s mirror: same key, new value, pg only.
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
        stale[0][0], 100,
        "claims whose stale_us was carried over (want 100 of 100): {stale:?}"
    );
}

/// End condition 2: `compare` (what `vector-check` prints) sees the same-key `stale_at` change.
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
