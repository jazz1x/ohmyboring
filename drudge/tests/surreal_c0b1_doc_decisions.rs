//! C-SURALL2 C0b1 bundle 1, the reads a write transaction decides on. pg no longer receives a
//! note's `document` row on `backend=surreal`, so these reads go to Surreal's.
//!
//! (m) the same inputs into the pg path and the Surreal path give the same answer, both sides of
//!     each branch (present / absent, owner / not owner), printed as `C0b1 (m) <read>: same n/N`.
//! (o1) the claim write fills a Surreal-only note's origin and project.
//! (o2) the `valid_from` source (`doc_updated_at`) is the Surreal document's `updated_at`, not now().
//! (o3) `record_gap_in_tx` picks a Surreal-only note as a handed note.
//! Each of (o1)-(o3) uses one document that exists in Surreal only, written with plain SurrealQL so
//! the test also compiles (and fails) on trunk, where the reads ask pg.
//!
//! Needs a throwaway pgvector and a throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test surreal_c0b1_doc_decisions -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::too_many_lines)]

use std::collections::BTreeSet;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::config::SurrealConfig;
use drudge::frontmatter::{Author, FrontMatter};
use drudge::store::{DocLookup, Store, SurrealClient, VectorRoute};
use serde_json::{Value, json};
use tokio_postgres::NoTls;

const DIM: usize = 1024;
const SURREAL_ONLY_US: i64 = 1_700_000_000_000_000;

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

fn front(path: &str, author: Author) -> FrontMatter {
    FrontMatter {
        origin: "personal".to_owned(),
        project: "c0b1-decisions".to_owned(),
        kind: "note".to_owned(),
        source_path: path.to_owned(),
        title: Some("c0b1".to_owned()),
        author,
        ..Default::default()
    }
}

/// An empty pg, Surreal tables laid down by a fill of it, and a store routed to that Surreal.
async fn setup(namespace: &str) -> (Store, Store, SurrealClient, tokio_postgres::Client) {
    let dsn = env("BORING_TEST_DATABASE_URL");
    let pg = Store::open(&dsn, DIM).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db.batch_execute(
        "DELETE FROM claim; DELETE FROM edge; DELETE FROM node; DELETE FROM document;",
    )
    .await
    .unwrap();
    let client = client_at(&format!("{namespace}_{}", nanos()));
    client.fill_from(&pg).await.expect("fill of an empty pg");
    let routed = Store::open(&dsn, DIM)
        .await
        .unwrap()
        .with_vector_route(VectorRoute::Surreal(client.clone()));
    (pg, routed, client, db)
}

/// A document row written into Surreal only, the way the engine's write puts it.
async fn surreal_only_note(client: &SurrealClient, path: &str, author: &str, updated_us: i64) {
    let p = serde_json::to_string(path).unwrap();
    client
        .run(&format!(
            "UPSERT type::record('document', {p}) CONTENT {{cid: {p}, source_path: {p}, \
             origin: 'personal', project: 'c0b1-decisions', kind: 'note', tags: [], sha: 'sha-s', \
             extracted_sha: '', updated_us: {updated_us}, author: '{author}', via: 'surreal'}};"
        ))
        .await
        .expect("surreal-only document");
}

/// Prints `C0b1 (m) <read>: same n/N` and fails on any difference.
fn same<T: std::fmt::Debug>(read: &str, pg: &[T], surreal: &[T]) {
    let (p, s): (Vec<String>, Vec<String>) = (
        pg.iter().map(|x| format!("{x:?}")).collect(),
        surreal.iter().map(|x| format!("{x:?}")).collect(),
    );
    let equal = p.iter().zip(&s).filter(|(a, b)| a == b).count();
    println!("C0b1 (m) {read}: same {equal}/{}", p.len());
    assert_eq!(p, s, "{read}: pg answer vs Surreal answer");
}

fn micros(t: SystemTime) -> u128 {
    t.duration_since(UNIX_EPOCH).unwrap().as_micros()
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn the_branches_answer_the_same_on_the_pg_path_and_the_surreal_path() {
    let (pg, routed, client, _db) = setup("c0b1_m").await;
    let n = nanos();
    let owner = format!("/vault/wiki/m-{n}-owner.md");
    let other = format!("/vault/wiki/m-{n}-other.md");
    let wide = format!("/vault/wiki/wiki-{:011}.md", n % 99_999_999);
    let missing = format!("/vault/wiki/m-{n}-missing.md");
    let alias = {
        let digits = n % 99_999_999;
        format!("/vault/wiki/wiki-{digits:04}.md")
    };
    let now = SystemTime::now();
    for (path, author, age_h) in [
        (&owner, Author::Owner, 5_u64),
        (&other, Author::Unknown, 9),
        (&wide, Author::Unknown, 20),
    ] {
        pg.upsert_document(
            &front(path, author),
            "sha",
            now - Duration::from_hours(age_h),
        )
        .await
        .unwrap();
    }
    let tables = client.fill_from(&pg).await.expect("fill");
    assert!(tables.iter().all(drudge::store::FillTable::same), "fill");

    let pg_path = DocLookup::Pg;
    let surreal_path = routed.doc_lookup();
    let mut conn = routed.conn().await.unwrap();
    let tx = conn.transaction().await.unwrap();

    // get_doc_sha: a note both hold, and a note neither holds.
    let all = [&owner, &other, &wide, &missing];
    let mut a = Vec::new();
    let mut b = Vec::new();
    for p in all {
        a.push(pg_path.sha(&tx, p).await.unwrap());
        b.push(surreal_path.sha(&tx, p).await.unwrap());
    }
    same("get_doc_sha", &a, &b);

    // doc_updated_at: the stored time for a held note; now() for a missing one on both paths.
    let mut a = Vec::new();
    let mut b = Vec::new();
    for p in [&owner, &other, &wide] {
        a.push(micros(pg_path.updated_at(&tx, p).await.unwrap()));
        b.push(micros(surreal_path.updated_at(&tx, p).await.unwrap()));
    }
    same("doc_updated_at (held)", &a, &b);
    let (pg_missing, surreal_missing) = (
        pg_path.updated_at(&tx, &missing).await.unwrap(),
        surreal_path.updated_at(&tx, &missing).await.unwrap(),
    );
    let near_now = |t: SystemTime| t.elapsed().is_ok_and(|e| e < Duration::from_secs(30));
    println!(
        "C0b1 (m) doc_updated_at (absent): same {}/1",
        u8::from(near_now(pg_missing) && near_now(surreal_missing))
    );
    assert!(near_now(pg_missing) && near_now(surreal_missing));

    // owner_authored: owner yes (owner note), owner no (other note), absent.
    let mut a = Vec::new();
    let mut b = Vec::new();
    for set in [
        vec![owner.clone()],
        vec![other.clone()],
        vec![owner.clone(), other.clone(), missing.clone()],
        vec![missing.clone()],
    ] {
        a.push(pg_path.owner_authored(&tx, &set).await.unwrap());
        b.push(surreal_path.owner_authored(&tx, &set).await.unwrap());
    }
    same("owner_authored", &a, &b);

    // known documents (the gap's handed notes) and the width-tolerant path lookup.
    let ask = vec![owner.clone(), other.clone(), missing.clone()];
    let a: BTreeSet<String> = pg_path
        .known(&tx, &ask)
        .await
        .unwrap()
        .into_iter()
        .collect();
    let b: BTreeSet<String> = surreal_path
        .known(&tx, &ask)
        .await
        .unwrap()
        .into_iter()
        .collect();
    same("known documents", &[a], &[b]);
    let mut a = Vec::new();
    let mut b = Vec::new();
    for p in [&wide, &alias, &missing] {
        a.push(pg_path.resolve_path(&tx, p).await.unwrap());
        b.push(surreal_path.resolve_path(&tx, p).await.unwrap());
    }
    same("resolve_doc_path", &a, &b);
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn o1_the_claim_write_fills_origin_and_project_of_a_surreal_only_note() {
    let (_pg, routed, client, _db) = setup("c0b1_o1").await;
    let path = format!("/vault/wiki/o1-{}.md", nanos());
    surreal_only_note(&client, &path, "unknown", SURREAL_ONLY_US).await;
    let mut emb = vec![0.0_f32; DIM];
    emb[0] = 1.0;
    routed
        .upsert_claim(
            "c0b1-o1-subject",
            "goal",
            "a long enough value to count as a statement",
            &path,
            SystemTime::now() - Duration::from_hours(1),
            &emb,
            "decision",
            "certain",
        )
        .await
        .expect("claim");

    let rows = client
        .run(&format!(
            "SELECT origin, project FROM claim_row WHERE source_path = {};",
            serde_json::to_string(&path).unwrap()
        ))
        .await
        .unwrap();
    let rows: Vec<Vec<Value>> = serde_json::from_value(Value::Array(rows)).unwrap();
    let row = rows[0].first().expect("the claim_row was copied");
    println!(
        "C0b1 o1 claim_row origin={} project={}",
        row["origin"], row["project"]
    );
    assert_eq!(row["origin"], "personal");
    assert_eq!(row["project"], "c0b1-decisions");
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn o2_the_valid_from_source_is_the_surreal_documents_updated_at() {
    let (_pg, routed, client, _db) = setup("c0b1_o2").await;
    let path = format!("/vault/wiki/o2-{}.md", nanos());
    surreal_only_note(&client, &path, "unknown", SURREAL_ONLY_US).await;
    let got = routed.doc_updated_at(&path).await.expect("updated_at");
    let want = UNIX_EPOCH + Duration::from_micros(u64::try_from(SURREAL_ONLY_US).unwrap());
    println!("C0b1 o2 doc_updated_at={got:?} want={want:?}");
    assert_eq!(got, want, "not now(): the Surreal row's updated_us");
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn o3_record_gap_picks_a_surreal_only_note_as_handed() {
    let (_pg, routed, client, _db) = setup("c0b1_o3").await;
    let path = format!("/vault/wiki/o3-{}.md", nanos());
    surreal_only_note(&client, &path, "unknown", SURREAL_ONLY_US).await;
    let report = routed
        .record_gap_in_tx(
            &drudge::gap::GapArgs {
                session_id: format!("o3-{}", nanos()),
                query: "what".to_owned(),
                kind: drudge::gap::Kind::Missing,
                handed: vec![path.clone()],
            },
            "2026-11-02",
        )
        .await
        .expect("gap");
    println!(
        "C0b1 o3 handed={} unknown={}",
        report.handed, report.unknown
    );
    assert_eq!((report.handed, report.unknown), (1, 0));
}
