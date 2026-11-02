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
    clippy::too_many_lines,
    clippy::cast_possible_wrap,
    clippy::cast_possible_truncation,
    clippy::cast_precision_loss
)]

use drudge::config::SurrealConfig;
use drudge::split_subjects::Merged;
use drudge::store::{ClaimWrite, SplitStanding, Store, SurrealClient, TxPlace, VectorRoute};
use drudge::vector::VectorIndex;
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

fn plain_client(namespace: &str) -> SurrealClient {
    let config: SurrealConfig = serde_json::from_value(
        json!({"url": env("BORING_TEST_SURREAL_URL"), "namespace": namespace, "database": "z1"}),
    )
    .unwrap();
    SurrealClient::new(config.settings(&|k| std::env::var(k).ok()).unwrap()).unwrap()
}

/// The client that races: its retry count is `Z1_TX_RETRIES`, and its counters hold only the
/// racing requests. Fixtures and the serial run use `client`, with the default retries, because
/// the vector index of `claim` can conflict with the request before it even with no other writer.
fn race_client(namespace: &str) -> SurrealClient {
    let retries: u32 = std::env::var("Z1_TX_RETRIES")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(drudge::store::TX_RETRIES);
    plain_client(namespace).with_tx_retries(retries)
}

async fn client(namespace: &str) -> SurrealClient {
    let client = plain_client(namespace);
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
    client
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
    println!(
        "Z1 test_place={} runs={runs} final_equals_serial={same}/{runs} failures={}",
        place.name(),
        failures.len()
    );
    for p in TxPlace::ALL {
        let n = c.tx_count(p);
        if n.committed + n.conflicts > 0 {
            println!(
                "Z1   counted place={} committed={} conflicts={} retries={} exhausted={}",
                p.name(),
                n.committed,
                n.conflicts,
                n.retries,
                n.exhausted
            );
        }
    }
    for f in failures.iter().take(3) {
        println!("Z1   first failure: {f}");
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn one_claim_slot_two_writers() {
    let c = client("z1_slot").await;
    let race = race_client("z1_slot");
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
            async {
                put(
                    &race,
                    &conc,
                    "goal",
                    &format!("/v/a-{conc}.md"),
                    1000,
                    "fact",
                )
                .await
            },
            async {
                put(
                    &race,
                    &conc,
                    "goal",
                    &format!("/v/b-{conc}.md"),
                    2000,
                    "fact",
                )
                .await
            },
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
    table(TxPlace::Slot, &race, n, same, &failures);
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
    let race = race_client("z1_note");
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
                race.seal_note(&format!("/v/new-{conc}.md"), &format!("/v/old-{conc}.md"))
                    .await
            },
            async { put(&race, &conc, "p1", &format!("/v/c-{conc}.md"), 900, "fact").await },
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
    table(TxPlace::Note, &race, n, same, &failures);
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
    let race = race_client("z1_cycle");
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
            put(&race, &conc, "p1", &format!("/v/c-{conc}.md"), 900, "fact").await?;
            put(&race, &conc, "p2", &format!("/v/c-{conc}.md"), 900, "fact").await
        };
        let (s1, s2, ing) = tokio::join!(
            async {
                race.seal_note(&format!("/v/new-{conc}.md"), &format!("/v/old-{conc}.md"))
                    .await
            },
            async {
                race.seal_note(&format!("/v/new2-{conc}.md"), &format!("/v/old2-{conc}.md"))
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
    table(TxPlace::Note, &race, n, same, &failures);
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
    let race = race_client("z1_split");
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
            race.merge_split_subject(&subject, SplitStanding::Owner),
            race.merge_split_subject(&subject, SplitStanding::Owner),
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
    table(TxPlace::Split, &race, n, same, &failures);
    assert!(
        failures.is_empty(),
        "{} of {n} runs differ from the serial run",
        failures.len()
    );
}

/// Control: two writers on two different slots share no guard row. Whatever conflicts this shows
/// is not the slot's; it is the vector index of `claim` that every claim write touches.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn control_two_different_slots_share_no_guard() {
    let c = client("z1_control").await;
    let race = race_client("z1_control");
    let n = runs();
    let mut failures = Vec::new();
    for i in 0..n {
        let (x, y) = (format!("z1-x-{i}"), format!("z1-y-{i}"));
        doc(&c, &format!("/v/x-{i}.md"), "agent").await;
        doc(&c, &format!("/v/y-{i}.md"), "agent").await;
        let (a, b) = tokio::join!(
            async { put(&race, &x, "goal", &format!("/v/x-{i}.md"), 1000, "fact").await },
            async { put(&race, &y, "goal", &format!("/v/y-{i}.md"), 1000, "fact").await },
        );
        let open = open_vector_claims(&c, &format!("z1-x-{i}")).await
            + open_vector_claims(&c, &format!("z1-y-{i}")).await;
        if a.is_err() || b.is_err() || open != 2 {
            failures.push(format!("run {i}: a={a:?} b={b:?} open_vectors={open}"));
        }
    }
    table(TxPlace::Slot, &race, n, n - failures.len(), &failures);
    assert!(
        failures.is_empty() || std::env::var("Z1_TX_RETRIES").as_deref() == Ok("0"),
        "{} of {n} control runs failed",
        failures.len()
    );
}

/// pg's lock matches `canon(c.subject) = canon($1)`: `Foo Bar` and `foo-bar` are one slot. Two
/// writers that spell one slot two ways must meet on one guard, so one of them is resent. A
/// guard keyed by the exact spelling sees no conflict at all; the rows end the same either way
/// (the seal reads the exact spelling, as pg's does), so what this counts is the runs in which
/// the two writers were serialised.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn two_spellings_of_one_slot_meet_on_one_guard() {
    let c = client("z1_spelling").await;
    let race = race_client("z1_spelling");
    let n = runs();
    let mut failures = Vec::new();
    let mut serialised = 0;
    for i in 0..n {
        let (loud, canon) = (format!("Z1 Spell {i}"), format!("z1-spell-{i}"));
        doc(&c, &format!("/v/l-{i}.md"), "agent").await;
        doc(&c, &format!("/v/c-{i}.md"), "agent").await;
        let before = race.tx_count(TxPlace::Slot).conflicts;
        let (a, b) = tokio::join!(
            async { put(&race, &loud, "goal", &format!("/v/l-{i}.md"), 1000, "fact").await },
            async { put(&race, &canon, "goal", &format!("/v/c-{i}.md"), 2000, "fact").await },
        );
        if race.tx_count(TxPlace::Slot).conflicts > before {
            serialised += 1;
        }
        if a.is_err() || b.is_err() {
            failures.push(format!("run {i}: a={a:?} b={b:?}"));
        }
    }
    println!("Z1 spelling runs={n} runs_with_a_conflict={serialised}");
    table(TxPlace::Slot, &race, n, n - failures.len(), &failures);
    assert!(
        serialised * 10 >= n * 8,
        "only {serialised} of {n} runs put the two spellings on one guard"
    );
    assert!(failures.is_empty(), "{failures:?}");
}

/// A split merge on Surreal deletes rows pg still holds. A tombstone keeps `catch_up` (which copies
/// pg's rows over Surreal's) from putting them back, and the blanked `document.sha` from being
/// copied over again. Rows come from pg first, so they are the ones the merge must keep deleted.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_split_merge_is_not_undone_by_catch_up() {
    let c = client("z1_tomb").await;
    let pg = Store::open(&env("BORING_TEST_DATABASE_URL"), DIM)
        .await
        .expect("open store");
    let n = runs().min(20);
    let e = emb();
    for i in 0..n {
        let spellings = [
            format!("Z1 Gone {i}"),
            format!("z1-gone-{i}"),
            format!("z1_gone_{i}"),
        ];
        for (k, spelling) in spellings.iter().enumerate() {
            let path = format!("/v/gone-{i}-{k}.md");
            pg.upsert_document(&front(&path), "sha", micros(1))
                .await
                .expect("document");
            pg.upsert_claim(
                spelling,
                &format!("p{k}"),
                "a long enough value to count as a statement",
                &path,
                micros(1_700_000_000_000_000 + k as u64),
                &e,
                "fact",
                "certain",
            )
            .await
            .expect("pg claim");
        }
    }
    c.catch_up(&pg).await.expect("first copy from pg");
    let rows = "SELECT VALUE count() FROM claim_row WHERE string::starts_with(string::lowercase(subject), 'z1') AND string::contains(string::lowercase(subject), 'gone') GROUP ALL;";
    let vectors = "SELECT VALUE count() FROM claim WHERE string::contains(string::lowercase(subject), 'gone') GROUP ALL;";
    let shas = "SELECT VALUE count() FROM document WHERE string::starts_with(source_path, '/v/gone-') AND sha != '' GROUP ALL;";
    let copied = count(&c, rows).await;
    let mut merged = 0;
    for i in 0..n {
        let m = c
            .merge_split_subject(&format!("z1-gone-{i}"), SplitStanding::Owner)
            .await
            .expect("merge");
        merged += i64::try_from(m.map_or(0, |m| m.deleted_rows)).unwrap();
    }
    let after_merge = count(&c, rows).await;
    for lap in 0..2 {
        c.catch_up(&pg).await.expect("catch_up");
        println!(
            "Z1 tombstone lap={lap} copied={copied} merged={merged} rows_after_merge={after_merge} resurrected_rows={}/{merged} resurrected_vectors={} resurrected_sha={}/{}",
            count(&c, rows).await,
            count(&c, vectors).await,
            count(&c, shas).await,
            3 * n
        );
    }
    // The mirror of a pg write to these notes reads pg's rows and documents too.
    let paths: Vec<String> = (0..n)
        .flat_map(|i| (0..3).map(move |k| format!("/v/gone-{i}-{k}.md")))
        .collect();
    c.mirror(&pg, &drudge::store::Scope::notes(paths))
        .await
        .expect("mirror");
    println!(
        "Z1 tombstone after_mirror resurrected_rows={}/{merged} resurrected_vectors={} resurrected_sha={}/{}",
        count(&c, rows).await,
        count(&c, vectors).await,
        count(&c, shas).await,
        3 * n
    );
    let in_pg: i64 = {
        let (db, conn) =
            tokio_postgres::connect(&env("BORING_TEST_DATABASE_URL"), tokio_postgres::NoTls)
                .await
                .expect("connect");
        tokio::spawn(conn);
        db.query_one(
            "SELECT count(*) FROM claim WHERE subject ILIKE '%gone%'",
            &[],
        )
        .await
        .unwrap()
        .get(0)
    };
    assert_eq!(copied, 3 * n as i64, "the first copy missed rows");
    assert_eq!(merged, 3 * n as i64, "the merge deleted {merged}");
    assert_eq!(in_pg, 3 * n as i64, "the merge wrote to pg");
    assert_eq!(
        count(&c, rows).await,
        0,
        "catch_up put deleted claim rows back"
    );
    assert_eq!(
        count(&c, vectors).await,
        0,
        "catch_up put deleted vector claims back"
    );
    assert_eq!(
        count(&c, shas).await,
        0,
        "a blanked document.sha was copied over again"
    );
}

fn micros(us: u64) -> std::time::SystemTime {
    std::time::UNIX_EPOCH + std::time::Duration::from_micros(us)
}

fn front(path: &str) -> drudge::frontmatter::FrontMatter {
    drudge::frontmatter::FrontMatter {
        origin: "personal".to_string(),
        project: "z1".to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("z1".to_string()),
        ..Default::default()
    }
}

async fn count(c: &SurrealClient, sql: &str) -> i64 {
    let answer = c.run(sql).await.expect("count");
    answer[0]
        .as_array()
        .unwrap()
        .first()
        .and_then(Value::as_i64)
        .unwrap_or(0)
}

/// backend=surreal: `remember`'s writer (`Store::upsert_claim`) goes through the Surreal
/// transaction, not pg, and one `catch_up` pass afterwards neither deletes those rows nor undoes
/// the seal they made (the pass copies pg's rows over Surreal's and used to delete what pg lacked).
/// The claims Python wrote to pg only reach Surreal in the same pass.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn surreal_written_claims_survive_catch_up_and_pg_written_ones_arrive() {
    let c = client("z1_wire").await;
    let dsn = env("BORING_TEST_DATABASE_URL");
    let pg = Store::open(&dsn, DIM).await.expect("open store");
    let routed = Store::open(&dsn, DIM)
        .await
        .expect("open store")
        .with_vector_route(drudge::store::VectorRoute::Surreal(c.clone()));
    let n = runs().min(20);
    let e = emb();
    for i in 0..n {
        let path = format!("/v/w1-{i}.md");
        routed
            .upsert_document(&front(&path), "sha", micros(1))
            .await
            .expect("document");
        for (k, vf) in [
            (0_u64, 1_700_000_000_000_000_u64),
            (1, 1_700_000_100_000_000),
        ] {
            routed
                .upsert_claim(
                    &format!("z1-w1-{i}"),
                    "goal",
                    &format!("a long enough value to count as a statement {k}"),
                    &path,
                    micros(vf),
                    &e,
                    "fact",
                    "certain",
                )
                .await
                .expect("remember-style claim on the surreal route");
        }
    }
    for i in 0..n {
        let path = format!("/v/w2-{i}.md");
        pg.upsert_document(&front(&path), "sha", micros(1))
            .await
            .expect("document");
        pg.upsert_claim(
            &format!("z1-w2-{i}"),
            "goal",
            "a long enough value to count as a statement",
            &path,
            micros(1_700_000_000_000_000),
            &e,
            "fact",
            "certain",
        )
        .await
        .expect("pg claim");
    }
    let (db, conn) = tokio_postgres::connect(&dsn, tokio_postgres::NoTls)
        .await
        .expect("connect");
    tokio::spawn(conn);
    let in_pg: i64 = db
        .query_one(
            "SELECT count(*) FROM claim WHERE subject LIKE 'z1-w1-%'",
            &[],
        )
        .await
        .unwrap()
        .get(0);
    let rows = |p: &str| {
        format!(
            "SELECT VALUE count() FROM claim_row WHERE string::starts_with(subject, '{p}') GROUP ALL;"
        )
    };
    let sealed = "SELECT VALUE count() FROM claim_row WHERE string::starts_with(subject, 'z1-w1-') AND superseded_us = 1700000100000000 GROUP ALL;";
    let open_vectors =
        "SELECT VALUE count() FROM claim WHERE string::starts_with(subject, 'z1-w1-') GROUP ALL;";
    let before = (
        count(&c, &rows("z1-w1-")).await,
        count(&c, &rows("z1-w2-")).await,
    );
    for lap in 0..2 {
        c.catch_up(&pg).await.expect("catch_up");
        println!(
            "Z1 wire lap={lap} surreal_written_rows={}/{} sealed={}/{n} open_vector_claims={}/{n} pg_has_surreal_written={in_pg} pg_written_rows_in_surreal={}/{n}",
            count(&c, &rows("z1-w1-")).await,
            2 * n,
            count(&c, sealed).await,
            count(&c, open_vectors).await,
            count(&c, &rows("z1-w2-")).await,
        );
    }
    println!(
        "Z1 wire before_first_catch_up surreal_written={}/{} pg_written={}/{n}",
        before.0,
        2 * n,
        before.1
    );
    assert_eq!(in_pg, 0, "the surreal route wrote to pg");
    assert_eq!(
        count(&c, &rows("z1-w1-")).await,
        2 * n as i64,
        "a surreal-written claim vanished"
    );
    assert_eq!(count(&c, sealed).await, n as i64, "a seal was undone");
    assert_eq!(count(&c, open_vectors).await, n as i64);
    assert_eq!(
        count(&c, &rows("z1-w2-")).await,
        n as i64,
        "a pg-written claim did not arrive"
    );
}

/// One row of an unseal scenario: `(subject, predicate, note, valid_from_us, sealed)`.
type Seed = (&'static str, &'static str, &'static str, i64, bool);

/// C-SURALL2 Z2a: retiring note `a` hands each fact slot it held to the latest live row of the
/// slot's canon spelling group, a canon-spelled row first (pg: `seal_superseded_claims`). Each
/// scenario lays the same rows into pg and Surreal, sets every row open or sealed by hand (so a
/// row sealed across spellings can exist, as old data has them), retires `a` with a newer note
/// `b`, and compares the open rows. The scenarios whose touched slot is spelled differently from
/// the row pg promotes are the ones an exact-spelling unseal gets wrong.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn unseal_promotes_within_the_canon_spelling_group_as_pg_does() {
    let c = client("z2a_unseal").await;
    let dsn = env("BORING_TEST_DATABASE_URL");
    let pg = Store::open(&dsn, DIM).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, tokio_postgres::NoTls)
        .await
        .expect("connect");
    tokio::spawn(conn);
    let e = emb();
    db.batch_execute("DELETE FROM edge WHERE src LIKE 'doc:/v/z2a-%'")
        .await
        .expect("clear edges of an earlier run");
    c.run("DELETE edge WHERE string::starts_with(src, 'doc:/v/z2a-');")
        .await
        .expect("clear surreal edges of an earlier run");
    let scenarios: [(&str, Vec<Seed>); 5] = [
        (
            "exact spelling only",
            vec![
                ("g1", "goal", "c", 1000, true),
                ("g1", "goal", "a", 2000, false),
            ],
        ),
        (
            "two spellings, canon-spelled row outranks a later old-spelling row",
            vec![
                ("G2 X", "goal", "c", 5000, true),
                ("g2-x", "goal", "d", 3000, true),
                ("G2 X", "goal", "a", 6000, false),
            ],
        ),
        (
            "two spellings, the only live row has the other spelling",
            vec![
                ("g3_x", "goal", "c", 1000, true),
                ("G3 X", "goal", "a", 2000, false),
            ],
        ),
        (
            "no candidate to promote",
            vec![("g4", "goal", "a", 2000, false)],
        ),
        (
            "two old spellings, the later valid_from wins",
            vec![
                ("g5_x", "goal", "c", 2000, true),
                ("G5-X", "goal", "d", 4000, true),
                ("G5 X", "goal", "a", 6000, false),
            ],
        ),
    ];
    let mut same = 0;
    for (n, (name, rows)) in scenarios.iter().enumerate() {
        let path = |note: &str| format!("/v/z2a-{n}-{note}.md");
        let mut notes: Vec<&str> = rows.iter().map(|r| r.2).collect();
        notes.push("b");
        notes.sort_unstable();
        notes.dedup();
        for note in &notes {
            pg.upsert_document(&front(&path(note)), "sha", micros(1))
                .await
                .expect("pg document");
            doc(&c, &path(note), "agent").await;
        }
        for (subject, predicate, note, vf, _) in rows {
            pg.upsert_claim(
                subject,
                predicate,
                "a long enough value to count as a statement",
                &path(note),
                micros(vf.cast_unsigned()),
                &e,
                "fact",
                "certain",
            )
            .await
            .expect("pg claim");
            put(&c, subject, predicate, &path(note), *vf, "fact")
                .await
                .expect("surreal claim");
        }
        for (subject, predicate, note, vf, sealed) in rows {
            db.execute(
                "UPDATE claim SET superseded_at = CASE WHEN $3 THEN now() END \
                 WHERE subject = $1 AND predicate = $2 AND source_path = $4 \
                   AND valid_from = (timestamptz 'epoch' + $5::bigint * interval '1 microsecond')",
                &[subject, predicate, sealed, &path(note), vf],
            )
            .await
            .expect("pg seal state");
            c.run(&format!(
                "UPDATE claim_row SET superseded_us = {} WHERE subject = {} AND predicate = {} \
                 AND source_path = {} AND valid_from_us = {vf};",
                if *sealed { "1" } else { "NONE" },
                json!(subject),
                json!(predicate),
                json!(path(note))
            ))
            .await
            .expect("surreal seal state");
        }
        pg.supersedes_in_tx(&[[path("b"), path("a")]], None)
            .await
            .expect("pg supersedes");
        supersedes(&c, &path("b"), &path("a")).await;
        c.seal_note(&path("b"), &path("a"))
            .await
            .expect("surreal seal");
        let like = format!("/v/z2a-{n}-%");
        let mut in_pg: Vec<String> = db
            .query(
                "SELECT subject || '|' || predicate || '|' || source_path || '|' || \
                        (extract(epoch FROM valid_from) * 1000000)::bigint \
                 FROM claim WHERE source_path LIKE $1 AND superseded_at IS NULL",
                &[&like],
            )
            .await
            .unwrap()
            .iter()
            .map(|r| r.get(0))
            .collect();
        in_pg.sort();
        let answer = c
            .run(&format!(
                "SELECT subject, predicate, source_path, valid_from_us FROM claim_row \
                 WHERE string::starts_with(source_path, '/v/z2a-{n}-') AND superseded_us = NONE;"
            ))
            .await
            .expect("open rows");
        let mut in_surreal: Vec<String> = answer[0]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| {
                format!(
                    "{}|{}|{}|{}",
                    r["subject"].as_str().unwrap(),
                    r["predicate"].as_str().unwrap(),
                    r["source_path"].as_str().unwrap(),
                    r["valid_from_us"].as_i64().unwrap()
                )
            })
            .collect();
        in_surreal.sort();
        let ok = in_pg == in_surreal;
        println!("Z2a unseal {n} [{name}] pg={in_pg:?} surreal={in_surreal:?} same={ok}");
        if ok {
            same += 1;
        }
    }
    println!("Z2a unseal parity {same}/{}", scenarios.len());
    assert_eq!(same, scenarios.len(), "open rows differ from pg");
}

/// The group of one unseal race, numbered `tag`: note `a` (retired by `b`) holds the open row of
/// spelling `T X`, note `c` a sealed row of spelling `t_x`, note `d` a sealed, older row of spelling
/// `t__x`, all one canon slot.
async fn spelling_swap_fixture(c: &SurrealClient, tag: &str) {
    let path = |note: &str| format!("/v/z2a-swap-{tag}-{note}.md");
    for note in ["a", "b", "c", "d"] {
        doc(c, &path(note), "agent").await;
    }
    let rows = [
        (format!("T X{tag}"), "a", 9000, "NONE"),
        (format!("t_x{tag}"), "c", 1000, "1"),
        (format!("t__x{tag}"), "d", 500, "1"),
    ];
    for (subject, note, vf, sealed) in &rows {
        put(c, subject, "goal", &path(note), *vf, "fact")
            .await
            .expect("claim");
        c.run(&format!(
            "UPDATE claim_row SET superseded_us = {sealed} WHERE subject = {} AND source_path = {};",
            json!(subject),
            json!(path(note))
        ))
        .await
        .expect("seal state");
    }
    supersedes(c, &path("b"), &path("a")).await;
}

/// The request that moves the sealed `t_x` row to another spelling of the same canon slot: one
/// spelling goes, another comes, and the count of spellings stays what it was.
async fn swap_the_spelling(c: &SurrealClient, tag: &str) {
    c.run(&format!(
        "BEGIN; UPDATE claim_row SET subject = {} WHERE subject = {} AND source_path = {}; COMMIT;",
        json!(format!("t x{tag}")),
        json!(format!("t_x{tag}")),
        json!(format!("/v/z2a-swap-{tag}-c.md"))
    ))
    .await
    .expect("swap");
}

async fn open_after_swap(c: &SurrealClient, tag: &str) -> Vec<String> {
    let answer = c
        .run(&format!(
            "SELECT subject, source_path, valid_from_us FROM claim_row \
             WHERE string::starts_with(source_path, '/v/z2a-swap-{tag}-') AND superseded_us = NONE;"
        ))
        .await
        .expect("open rows");
    let mut rows: Vec<String> = answer[0]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            format!(
                "{}|{}|{}",
                r["subject"].as_str().unwrap().replace(tag, "#"),
                r["source_path"].as_str().unwrap().replace(tag, "#"),
                r["valid_from_us"].as_i64().unwrap()
            )
        })
        .collect();
    rows.sort();
    rows
}

/// A seal plans its groups from the spellings it read; a request that swaps one spelling of the
/// group for another between that read and the seal's commit leaves the count of spellings as it
/// was. The seal must notice and plan again: the open rows must be what a seal run before the swap
/// and a seal run after it both leave (here the same rows), not what the stale plan promotes.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_seal_replans_when_a_spelling_of_its_group_is_swapped_under_it() {
    let c = client("z2a_swap").await;
    let race = race_client("z2a_swap");
    let (before, after) = ("sb", "sa");
    spelling_swap_fixture(&c, before).await;
    c.seal_note(
        &format!("/v/z2a-swap-{before}-b.md"),
        &format!("/v/z2a-swap-{before}-a.md"),
    )
    .await
    .expect("serial seal");
    swap_the_spelling(&c, before).await;
    let first = open_after_swap(&c, before).await;
    spelling_swap_fixture(&c, after).await;
    swap_the_spelling(&c, after).await;
    c.seal_note(
        &format!("/v/z2a-swap-{after}-b.md"),
        &format!("/v/z2a-swap-{after}-a.md"),
    )
    .await
    .expect("serial seal");
    let second = open_after_swap(&c, after).await;
    assert_eq!(first, second, "the two serial orders disagree");
    let n = runs();
    let mut failures = Vec::new();
    let mut same = 0;
    for i in 0..n {
        let tag = format!("r{i}");
        spelling_swap_fixture(&c, &tag).await;
        let wait = std::time::Duration::from_micros((i % 5) as u64 * 700);
        let (newer, older) = (
            format!("/v/z2a-swap-{tag}-b.md"),
            format!("/v/z2a-swap-{tag}-a.md"),
        );
        let (sealed, ()) = tokio::join!(race.seal_note(&newer, &older), async {
            tokio::time::sleep(wait).await;
            swap_the_spelling(&c, &tag).await;
        });
        let got = open_after_swap(&c, &tag).await;
        if sealed.is_ok() && got == first {
            same += 1;
        } else {
            failures.push(format!("run {i}: sealed={sealed:?} open={got:?}"));
        }
    }
    println!(
        "Z2a swap runs={n} same_as_serial={same} serial={first:?} first_failure={:?}",
        failures.first()
    );
    assert_eq!(
        same,
        n,
        "{} of {n} runs: {:?}",
        failures.len(),
        failures.first()
    );
}

/// A vector no two values of `seed` share: the promoted row must come back with its own floats,
/// not with the fixed `emb()` every other fixture writes.
fn emb_of(seed: f32) -> Vec<f32> {
    (0..DIM).map(|i| ((i as f32) * 0.37 + seed).sin()).collect()
}

async fn put_with(
    c: &SurrealClient,
    subject: &str,
    path: &str,
    vf: i64,
    e: &[f32],
) -> anyhow::Result<()> {
    c.claim_slot_upsert(&ClaimWrite {
        subject,
        predicate: "goal",
        value: "a long enough value to count as a statement",
        source_path: path,
        valid_from_us: vf,
        embedding: e,
        kind: "fact",
        confidence: "certain",
        anchor: None,
        anchor_hash: None,
        anchor_symbol: None,
    })
    .await
}

/// Z2② (a)(b): a row sealed by a newer note comes back when that note is retired, with the
/// embedding it was written with, and the claim vector search finds it. pg keeps the embedding on
/// the sealed row (`store.rs` claim table), so promotion there only clears `superseded_at`.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_sealed_row_promoted_again_is_in_the_vector_claim_search() {
    let c = client("z2b_promote").await;
    let tag = "z2b-promote";
    let (a, b, third) = (
        format!("/v/a-{tag}.md"),
        format!("/v/b-{tag}.md"),
        format!("/v/c-{tag}.md"),
    );
    for p in [&a, &b, &third] {
        doc(&c, p, "agent").await;
    }
    let (ea, eb) = (emb_of(1.0), emb_of(2.0));
    put_with(&c, tag, &a, 100, &ea).await.unwrap();
    put_with(&c, tag, &b, 200, &eb).await.unwrap();
    let sealed = shape(&c, tag).await;
    assert_eq!(sealed[0].3, Some(200), "older row sealed by the newer one");
    assert_eq!(
        open_vector_claims(&c, tag).await,
        1,
        "only the newer row is open"
    );

    supersedes(&c, &third, &b).await;
    c.seal_note(&third, &b).await.unwrap();
    let after = shape(&c, tag).await;
    assert_eq!(after[0].3, None, "older row promoted");
    assert!(after[1].3.is_some(), "retired note's row sealed");

    let store = Store::open(&env("BORING_TEST_DATABASE_URL"), DIM)
        .await
        .expect("open store")
        .with_vector_route(VectorRoute::Surreal(c.clone()));
    let hits = store
        .current_claims(&ea, 50, &[], None, None, None, false)
        .await
        .unwrap();
    let ours: Vec<_> = hits
        .iter()
        .filter(|h| h.claim.subject == tag)
        .map(|h| (h.claim.predicate.clone(), h.claim.value.clone()))
        .collect();
    assert_eq!(
        ours.len(),
        1,
        "claim vector search returns the promoted row: {ours:?}"
    );

    let answer = c
        .run(&format!(
            "SELECT VALUE embedding FROM claim WHERE subject = {} AND valid_from_us = 100;",
            json!(tag)
        ))
        .await
        .unwrap();
    let got: Vec<f32> = answer[0]
        .get(0)
        .and_then(Value::as_array)
        .map(|a| a.iter().map(|v| v.as_f64().unwrap() as f32).collect())
        .unwrap_or_default();
    println!("Z2b promoted_embedding_equal={}/1", usize::from(got == ea));
    assert_eq!(got, ea, "promoted embedding equals the one written");
}
