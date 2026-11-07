//! C-SURALL2 Z3 r2: every claim write transaction bumps the one row `vector_meta:claim.dirty`.
//! K writers, each on a slot of its own (so no slot guard is shared), race on that row alone.
//! Per concurrency level the test prints the conflicts and resends the writers counted, and
//! checks that every write landed: K x ROUNDS claim rows, and `dirty` grew by that many.
//!
//! Needs a throwaway pgvector (only so `fill_from` can lay down the Surreal tables from an empty
//! pg) and a throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   Z3_ROUNDS=50 cargo test --test vector_surreal_meta_bump -- --ignored --nocapture --test-threads=1
#![allow(
    clippy::expect_used,
    clippy::unwrap_used,
    clippy::cast_possible_wrap,
    clippy::cast_precision_loss
)]

use drudge::config::SurrealConfig;
use drudge::store::{ClaimWrite, Store, SurrealClient, TxPlace};
use serde_json::json;

const DIM: usize = 1024;
const LEVELS: [usize; 3] = [2, 4, 8];

fn env(name: &str) -> String {
    std::env::var(name).expect(name)
}

fn rounds() -> usize {
    std::env::var("Z3_ROUNDS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(50)
}

fn client(namespace: &str, retries: u32) -> SurrealClient {
    let config: SurrealConfig = serde_json::from_value(
        json!({"url": env("BORING_TEST_SURREAL_URL"), "namespace": namespace, "database": "z3"}),
    )
    .unwrap();
    SurrealClient::new(config.settings(&|k| std::env::var(k).ok()).unwrap())
        .unwrap()
        .with_tx_retries(retries)
}

async fn lay_tables(namespace: &str) {
    let store = Store::open(&env("BORING_TEST_DATABASE_URL"), DIM)
        .await
        .expect("open store");
    let (db, conn) =
        tokio_postgres::connect(&env("BORING_TEST_DATABASE_URL"), tokio_postgres::NoTls)
            .await
            .expect("connect");
    tokio::spawn(conn);
    db.batch_execute("DELETE FROM claim; DELETE FROM document;")
        .await
        .unwrap();
    client(namespace, drudge::store::TX_RETRIES)
        .fill_from(&store)
        .await
        .expect("fill of an empty pg");
}

async fn one_int(c: &SurrealClient, sql: &str) -> i64 {
    let answer = c.run(sql).await.expect("query");
    answer[0]
        .as_array()
        .unwrap()
        .first()
        .and_then(serde_json::Value::as_i64)
        .unwrap_or(0)
}

async fn doc(c: &SurrealClient, path: &str) {
    c.run(&format!(
        "UPSERT type::record('document', {p}) CONTENT {{cid: {p}, source_path: {p}, origin: 'personal', \
         project: 'z3', kind: 'note', tags: [], sha: 'sha', extracted_sha: '', updated_us: 0, author: 'agent'}};",
        p = json!(path)
    ))
    .await
    .expect("document");
}

async fn put(c: &SurrealClient, subject: &str, path: &str) -> anyhow::Result<()> {
    let mut e = vec![0.0_f32; DIM];
    e[0] = 1.0;
    c.claim_slot_upsert(&ClaimWrite {
        subject,
        predicate: "goal",
        value: "a long enough value to count as a statement",
        source_path: path,
        valid_from_us: 1000,
        embedding: &e,
        kind: "fact",
        confidence: "certain",
        anchor: None,
        anchor_hash: None,
        anchor_symbol: None,
    })
    .await
}

/// `writers` concurrent claim writes per round, `rounds` rounds, on `race` (whose counters hold
/// only these writes). Returns (writes that returned Ok, writes made).
async fn race_level(
    setup: &SurrealClient,
    race: &SurrealClient,
    tag: &str,
    writers: usize,
    rounds: usize,
) -> (usize, usize) {
    let mut ok = 0;
    for r in 0..rounds {
        let mut jobs = Vec::new();
        for w in 0..writers {
            let subject = format!("z3-{tag}-{r}-{w}");
            let path = format!("/z3/{subject}.md");
            doc(setup, &path).await;
            let c = race.clone();
            jobs.push(tokio::spawn(async move { put(&c, &subject, &path).await }));
        }
        for j in jobs {
            if j.await.unwrap().is_ok() {
                ok += 1;
            }
        }
    }
    (ok, writers * rounds)
}

fn print_level(label: &str, race: &SurrealClient, ok: usize, made: usize, landed: i64, dirty: i64) {
    let n = race.tx_count(TxPlace::Slot);
    println!(
        "Z3 r2 {label} writes_ok={ok}/{made} rows_landed={landed}/{made} dirty_grew={dirty} \
         committed={} conflicts={}/{} retries={}/{} exhausted={}",
        n.committed,
        n.conflicts,
        n.committed + n.conflicts,
        n.retries,
        n.committed + n.conflicts,
        n.exhausted
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn concurrent_claim_writes_all_land_and_bump_the_dirty_count() {
    let ns = "z3_meta_bump";
    lay_tables(ns).await;
    let setup = client(ns, drudge::store::TX_RETRIES);
    let rounds = rounds();
    let mut failed = Vec::new();
    for writers in LEVELS {
        let race = client(ns, drudge::store::TX_RETRIES);
        let tag = format!("c{writers}");
        let dirty_before = one_int(&setup, "SELECT VALUE dirty FROM vector_meta:claim;").await;
        let (ok, made) = race_level(&setup, &race, &tag, writers, rounds).await;
        let dirty_after = one_int(&setup, "SELECT VALUE dirty FROM vector_meta:claim;").await;
        let landed = one_int(
            &setup,
            &format!(
                "SELECT VALUE count() FROM claim_row WHERE string::starts_with(subject, 'z3-{tag}-') GROUP ALL;"
            ),
        )
        .await;
        print_level(
            &format!("concurrency={writers}"),
            &race,
            ok,
            made,
            landed,
            dirty_after - dirty_before,
        );
        if ok != made || landed != made as i64 || dirty_after - dirty_before != made as i64 {
            failed.push(writers);
        }
    }
    // Control: the same race at the top level with the retry turned off shows the conflict as a failure.
    let top = *LEVELS.last().unwrap();
    let race = client(ns, 0);
    let (ok, made) = race_level(&setup, &race, "c0retry", top, rounds).await;
    let landed = one_int(
        &setup,
        "SELECT VALUE count() FROM claim_row WHERE string::starts_with(subject, 'z3-c0retry-') GROUP ALL;",
    )
    .await;
    print_level(
        "control_retries=0 concurrency=8",
        &race,
        ok,
        made,
        landed,
        -1,
    );
    assert!(
        failed.is_empty(),
        "levels where a write did not land or dirty did not add up: {failed:?}"
    );
}
