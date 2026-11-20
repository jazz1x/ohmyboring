//! C-SURALL2 Z3 z: seals notes in a Surreal that already holds a copy (`vector-fill`) through the
//! engine's own `SurrealClient::seal_note`, so the Python read path can be shown to see the seal.
//! `Z3_SEAL_PAIRS` is `newer_path|older_path` pairs joined by `;`; each pair seals every open
//! claim of `older`. Prints one `Z3 seal newer=… older=… sealed=N` line per pair. Run:
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   BORING_Z3_NAMESPACE=z3 Z3_SEAL_PAIRS='/a.md|/b.md;/c.md|/d.md' \
//!   cargo test --test vector_surreal_seal_note_driver -- --ignored --nocapture
#![allow(clippy::expect_used)]

use drudge::config::SurrealConfig;
use drudge::store::{GraphOp, SurrealClient};
use serde_json::json;

fn env(name: &str) -> String {
    std::env::var(name).expect(name)
}

#[tokio::test]
#[ignore = "needs a filled throwaway surreal: see the run command in this file's header"]
async fn seal_the_older_note_of_each_pair() {
    let config: SurrealConfig = serde_json::from_value(
        json!({"url": env("BORING_TEST_SURREAL_URL"), "namespace": env("BORING_Z3_NAMESPACE"), "database": env("BORING_Z3_NAMESPACE")}),
    )
    .expect("surreal config");
    let client = SurrealClient::new(
        config
            .settings(&|k| std::env::var(k).ok())
            .expect("settings"),
    )
    .expect("client");
    for pair in env("Z3_SEAL_PAIRS").split(';') {
        let (newer, older) = pair.split_once('|').expect("newer|older");
        // remember writes the supersedes edge first; the seal retires only what an edge names.
        client
            .graph_write(&[GraphOp::edge(
                &format!("doc:{newer}"),
                &format!("doc:{older}"),
                "supersedes",
                Some("agent:z3"),
            )])
            .await
            .expect("graph_write");
        let sealed = client.seal_note(newer, older).await.expect("seal_note");
        println!("Z3 seal newer={newer} older={older} sealed={sealed}");
        assert!(sealed > 0, "nothing sealed for {older}");
    }
}

/// Z3 r1: the engine's note delete (`delete_note`, what an ingest of a vanished note runs) taken
/// over notes until `Z3_DELETE_CLAIMS` open vector claims are gone. Run as above with
/// `Z3_DELETE_CLAIMS=421`; the lead samples the server's memory from outside.
#[tokio::test]
#[ignore = "needs a filled throwaway surreal: see the run command in this file's header"]
async fn delete_notes_until_the_claim_count_is_gone() {
    let config: SurrealConfig = serde_json::from_value(
        json!({"url": env("BORING_TEST_SURREAL_URL"), "namespace": env("BORING_Z3_NAMESPACE"), "database": env("BORING_Z3_NAMESPACE")}),
    )
    .expect("surreal config");
    let client = SurrealClient::new(
        config
            .settings(&|k| std::env::var(k).ok())
            .expect("settings"),
    )
    .expect("client");
    let want: usize = env("Z3_DELETE_CLAIMS").parse().expect("count");
    let answer = client
        .run("SELECT source_path, count() AS n FROM claim GROUP BY source_path ORDER BY source_path;")
        .await
        .expect("claims per note");
    let mut gone = 0_usize;
    let mut notes = 0_usize;
    let started = std::time::Instant::now();
    for row in answer[0].as_array().expect("rows") {
        if gone >= want {
            break;
        }
        let path = row["source_path"].as_str().expect("path");
        client.delete_note(path).await.expect("delete_note");
        gone += usize::try_from(row["n"].as_u64().expect("n")).expect("n fits");
        notes += 1;
    }
    println!(
        "Z3 r1 want={want} claims_deleted={gone} notes_deleted={notes} wall_ms={}",
        started.elapsed().as_millis()
    );
    let left = client
        .run("SELECT VALUE count() FROM claim GROUP ALL;")
        .await
        .expect("count");
    println!("Z3 r1 vector_claims_left={}", left[0][0]);
    assert!(gone >= want);
}
