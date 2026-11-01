//! C-SURALL2 Z1: the three places pg locks claim rows, on Surreal — one claim slot, every slot of
//! a note, a split-subjects merge. Each test races two requests and compares the final rows with
//! a serial run of the same writes; the counts of conflicts and resends are printed as `Z1 ...`.
//!
//! Needs a throwaway pgvector (only so `fill_from` can lay down the Surreal tables from an empty
//! pg) and a throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   Z1_RUNS=100 Z1_TX_RETRIES=8 \
//!   cargo test --test vector_surreal_claim_tx -- --ignored --nocapture --test-threads=1
//! `Z1_TX_RETRIES=0` turns the retry off: the same tests then show the conflict as a failure.
#![allow(
    clippy::expect_used,
    clippy::unwrap_used,
    clippy::many_single_char_names,
    clippy::cast_possible_wrap
)]

use drudge::config::SurrealConfig;
use drudge::split_subjects::Merged;
use drudge::store::{ClaimWrite, SplitStanding, Store, SurrealClient, TxPlace};
use serde_json::{Value, json};

const DIM: usize = 1024;

fn env(name: &str) -> String {
    std::env::var(name).expect(name)
}

fn runs() -> usize {
    std::env::var("Z1_RUNS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(100)
}

async fn client(namespace: &str) -> SurrealClient {
    let config: SurrealConfig = serde_json::from_value(
        json!({"url": env("BORING_TEST_SURREAL_URL"), "namespace": namespace, "database": "z1"}),
    )
    .unwrap();
    let client = SurrealClient::new(config.settings(&|k| std::env::var(k).ok()).unwrap()).unwrap();
    let retries: u32 = std::env::var("Z1_TX_RETRIES")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(drudge::store::TX_RETRIES);
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
    client.fill_from(&store).await.expect("fill of an empty pg");
    client.with_tx_retries(retries)
}

async fn doc(c: &SurrealClient, path: &str, author: &str) {
    c.run(&format!(
        "UPSERT type::record('document', {p}) CONTENT {{cid: {p}, source_path: {p}, origin: 'personal', \
         project: 'z1', kind: 'note', tags: [], sha: 'sha', extracted_sha: '', updated_us: 0, author: {a}}};",
        p = json!(path),
        a = json!(author)
    ))
    .await
    .expect("document");
}

async fn supersedes(c: &SurrealClient, newer: &str, older: &str) {
    let key = format!("doc:{newer}\u{1f}doc:{older}\u{1f}supersedes");
    c.run(&format!(
        "UPSERT type::record('edge', {k}) CONTENT {{cid: {k}, src: {s}, dst: {d}, kind: 'supersedes'}};",
        k = json!(key),
        s = json!(format!("doc:{newer}")),
        d = json!(format!("doc:{older}"))
    ))
    .await
    .expect("edge");
}

fn emb() -> Vec<f32> {
    let mut v = vec![0.0; DIM];
    v[0] = 1.0;
    v
}

async fn put(
    c: &SurrealClient,
    subject: &str,
    predicate: &str,
    path: &str,
    vf: i64,
    kind: &str,
) -> anyhow::Result<()> {
    let e = emb();
    c.claim_slot_upsert(&ClaimWrite {
        subject,
        predicate,
        value: "a long enough value to count as a statement",
        source_path: path,
        valid_from_us: vf,
        embedding: &e,
        kind,
        confidence: "certain",
        anchor: None,
        anchor_hash: None,
        anchor_symbol: None,
    })
    .await
}

/// `(subject, predicate, source_path, valid_from_us, superseded_us)` of every claim row whose
/// subject starts with `prefix`, in a fixed order.
async fn shape(c: &SurrealClient, prefix: &str) -> Vec<(String, String, i64, Option<i64>)> {
    let answer = c
        .run(&format!(
            "SELECT predicate, source_path, valid_from_us, superseded_us FROM claim_row \
             WHERE string::starts_with(subject, {}) ORDER BY predicate, valid_from_us, source_path;",
            json!(prefix)
        ))
        .await
        .expect("shape");
    answer[0]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            (
                r["predicate"].as_str().unwrap().to_owned(),
                r["source_path"].as_str().unwrap().to_owned(),
                r["valid_from_us"].as_i64().unwrap(),
                r["superseded_us"].as_i64(),
            )
        })
        .collect()
}

async fn open_vector_claims(c: &SurrealClient, prefix: &str) -> i64 {
    let answer = c
        .run(&format!(
            "SELECT VALUE count() FROM claim WHERE string::starts_with(subject, {}) GROUP ALL;",
            json!(prefix)
        ))
        .await
        .expect("count");
    answer[0]
        .as_array()
        .unwrap()
        .first()
        .and_then(Value::as_i64)
        .unwrap_or(0)
}

fn table(place: TxPlace, c: &SurrealClient, runs: usize, same: usize, failures: &[String]) {
    let n = c.tx_count(place);
    println!(
        "Z1 place={} runs={runs} final_equals_serial={same}/{runs} committed={} conflicts={} retries={} exhausted={} failures={}",
        place.name(),
        n.committed,
        n.conflicts,
        n.retries,
        n.exhausted,
        failures.len()
    );
    for f in failures.iter().take(3) {
        println!("Z1   first failure: {f}");
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn one_claim_slot_two_writers() {
    let c = client("z1_slot").await;
    let n = runs();
    let mut same = 0;
    let mut failures = Vec::new();
    for i in 0..n {
        let (conc, ser) = (format!("z1-conc-{i}"), format!("z1-ser-{i}"));
        for s in [&conc, &ser] {
            doc(&c, &format!("/v/a-{s}.md"), "agent").await;
            doc(&c, &format!("/v/b-{s}.md"), "agent").await;
        }
        let (a, b) = tokio::join!(
            async { put(&c, &conc, "goal", &format!("/v/a-{conc}.md"), 1000, "fact").await },
            async { put(&c, &conc, "goal", &format!("/v/b-{conc}.md"), 2000, "fact").await },
        );
        put(&c, &ser, "goal", &format!("/v/a-{ser}.md"), 1000, "fact")
            .await
            .unwrap();
        put(&c, &ser, "goal", &format!("/v/b-{ser}.md"), 2000, "fact")
            .await
            .unwrap();
        let norm = |v: Vec<(String, String, i64, Option<i64>)>| {
            v.into_iter()
                .map(|(p, _, vf, s)| (p, vf, s))
                .collect::<Vec<_>>()
        };
        let (got, want) = (norm(shape(&c, &conc).await), norm(shape(&c, &ser).await));
        let open = open_vector_claims(&c, &conc).await;
        match (a, b) {
            (Ok(()), Ok(())) if got == want && open == 1 => same += 1,
            (a, b) => failures.push(format!(
                "run {i}: a={a:?} b={b:?} got={got:?} want={want:?} open_vectors={open}"
            )),
        }
    }
    table(TxPlace::Slot, &c, n, same, &failures);
    assert!(
        failures.is_empty(),
        "{} of {n} runs differ from the serial run",
        failures.len()
    );
}

async fn note_fixture(c: &SurrealClient, tag: &str, slot_order: [&str; 2]) {
    let (old, new, third) = (
        format!("/v/old-{tag}.md"),
        format!("/v/new-{tag}.md"),
        format!("/v/c-{tag}.md"),
    );
    for p in [&old, &new, &third] {
        doc(c, p, "agent").await;
    }
    for p in slot_order {
        put(c, tag, p, &old, 100, "fact").await.unwrap();
    }
    put(c, tag, "other", &new, 500, "fact").await.unwrap();
    supersedes(c, &new, &old).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn every_slot_of_a_note_against_a_slot_writer() {
    let c = client("z1_note").await;
    let n = runs();
    let mut same = 0;
    let mut failures = Vec::new();
    for i in 0..n {
        let (conc, ser) = (format!("z1-conc-{i}"), format!("z1-ser-{i}"));
        for tag in [&conc, &ser] {
            note_fixture(&c, tag, ["p1", "p2"]).await;
        }
        let (sealed, written) = tokio::join!(
            async {
                c.seal_note(&format!("/v/new-{conc}.md"), &format!("/v/old-{conc}.md"))
                    .await
            },
            async { put(&c, &conc, "p1", &format!("/v/c-{conc}.md"), 900, "fact").await },
        );
        let sealed_serial = c
            .seal_note(&format!("/v/new-{ser}.md"), &format!("/v/old-{ser}.md"))
            .await
            .unwrap();
        put(&c, &ser, "p1", &format!("/v/c-{ser}.md"), 900, "fact")
            .await
            .unwrap();
        let norm = |v: Vec<(String, String, i64, Option<i64>)>| {
            v.into_iter()
                .map(|(p, _, vf, s)| (p, vf, s))
                .collect::<Vec<_>>()
        };
        let (got, want) = (norm(shape(&c, &conc).await), norm(shape(&c, &ser).await));
        let counts_ok = matches!(&sealed, Ok(k) if *k <= sealed_serial);
        match (&sealed, &written) {
            (Ok(_), Ok(())) if got == want && counts_ok => same += 1,
            _ => failures.push(format!(
                "run {i}: seal={sealed:?} put={written:?} got={got:?} want={want:?}"
            )),
        }
    }
    table(TxPlace::Note, &c, n, same, &failures);
    assert!(
        failures.is_empty(),
        "{} of {n} runs differ from the serial run",
        failures.len()
    );
}

/// What `lock_claim_slots_of` (pg, store.rs) keeps from a cycle: an ingest that writes slots in
/// sorted order (p1, p2) while two seals touch the same two slots, one of them from the far end
/// (p2, p1). In pg that is a lock-order cycle; here it is only conflicts, resent until each
/// commits, and the rows end as if the three ran one after another.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_seal_and_an_ingest_that_meet_in_a_cycle_both_finish() {
    let c = client("z1_cycle").await;
    let n = runs().min(30);
    let mut same = 0;
    let mut failures = Vec::new();
    for i in 0..n {
        let (conc, ser) = (format!("z1-conc-{i}"), format!("z1-ser-{i}"));
        for tag in [&conc, &ser] {
            note_fixture(&c, tag, ["p1", "p2"]).await;
            // a second retired note whose rows were written in the opposite slot order
            let (old2, new2) = (format!("/v/old2-{tag}.md"), format!("/v/new2-{tag}.md"));
            doc(&c, &old2, "agent").await;
            doc(&c, &new2, "agent").await;
            put(&c, tag, "p2", &old2, 110, "fact").await.unwrap();
            put(&c, tag, "p1", &old2, 120, "fact").await.unwrap();
            put(&c, tag, "other2", &new2, 600, "fact").await.unwrap();
            supersedes(&c, &new2, &old2).await;
        }
        let ingest = async {
            put(&c, &conc, "p1", &format!("/v/c-{conc}.md"), 900, "fact").await?;
            put(&c, &conc, "p2", &format!("/v/c-{conc}.md"), 900, "fact").await
        };
        let (s1, s2, ing) = tokio::join!(
            async {
                c.seal_note(&format!("/v/new-{conc}.md"), &format!("/v/old-{conc}.md"))
                    .await
            },
            async {
                c.seal_note(&format!("/v/new2-{conc}.md"), &format!("/v/old2-{conc}.md"))
                    .await
            },
            ingest,
        );
        c.seal_note(&format!("/v/new-{ser}.md"), &format!("/v/old-{ser}.md"))
            .await
            .unwrap();
        c.seal_note(&format!("/v/new2-{ser}.md"), &format!("/v/old2-{ser}.md"))
            .await
            .unwrap();
        put(&c, &ser, "p1", &format!("/v/c-{ser}.md"), 900, "fact")
            .await
            .unwrap();
        put(&c, &ser, "p2", &format!("/v/c-{ser}.md"), 900, "fact")
            .await
            .unwrap();
        let norm = |v: Vec<(String, String, i64, Option<i64>)>| {
            v.into_iter()
                .map(|(p, s, vf, sup)| (p, s.rsplit('-').next().map(str::to_owned), vf, sup))
                .collect::<Vec<_>>()
        };
        let (got, want) = (norm(shape(&c, &conc).await), norm(shape(&c, &ser).await));
        match (&s1, &s2, &ing) {
            (Ok(_), Ok(_), Ok(())) if got == want => same += 1,
            _ => failures.push(format!(
                "run {i}: s1={s1:?} s2={s2:?} ingest={ing:?} got={got:?} want={want:?}"
            )),
        }
    }
    table(TxPlace::Note, &c, n, same, &failures);
    assert!(
        failures.is_empty(),
        "{} of {n} cycle runs differ or failed",
        failures.len()
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn one_split_group_two_merges() {
    let c = client("z1_split").await;
    let n = runs();
    let mut same = 0;
    let mut failures = Vec::new();
    for i in 0..n {
        let subject = format!("z1-split-{i}");
        let spellings = [
            format!("Z1 Split {i}"),
            format!("z1-split-{i}"),
            format!("z1_split_{i}"),
        ];
        for (k, spelling) in spellings.iter().enumerate() {
            let path = format!("/v/split-{i}-{k}.md");
            doc(&c, &path, "agent").await;
            put(
                &c,
                spelling,
                &format!("p{k}"),
                &path,
                100 + k as i64,
                "fact",
            )
            .await
            .unwrap();
        }
        let (a, b) = tokio::join!(
            c.merge_split_subject(&subject, SplitStanding::Owner),
            c.merge_split_subject(&subject, SplitStanding::Owner),
        );
        let live = c
            .run(&format!(
                "SELECT VALUE count() FROM claim_row WHERE string::starts_with(string::lowercase(subject), 'z1') \
                 AND source_path INSIDE {} AND superseded_us = NONE GROUP ALL;",
                json!((0..3).map(|k| format!("/v/split-{i}-{k}.md")).collect::<Vec<_>>())
            ))
            .await
            .unwrap();
        let live = live[0]
            .as_array()
            .unwrap()
            .first()
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let blanked = c
            .run(&format!(
                "SELECT VALUE count() FROM document WHERE string::starts_with(source_path, '/v/split-{i}-') AND sha = '' GROUP ALL;"
            ))
            .await
            .unwrap();
        let blanked = blanked[0]
            .as_array()
            .unwrap()
            .first()
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let serial_value = Merged {
            subject: subject.clone(),
            deleted_rows: 3,
            reread_notes: 3,
            owner_held: vec![],
        };
        let ok = match (&a, &b) {
            (Ok(Some(m)), Ok(None)) | (Ok(None), Ok(Some(m))) => *m == serial_value,
            _ => false,
        };
        if ok && live == 0 && blanked == 3 {
            same += 1;
        } else {
            failures.push(format!(
                "run {i}: a={a:?} b={b:?} live={live} blanked={blanked}"
            ));
        }
    }
    table(TxPlace::Split, &c, n, same, &failures);
    assert!(
        failures.is_empty(),
        "{} of {n} runs differ from the serial run",
        failures.len()
    );
}
