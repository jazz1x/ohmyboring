//! `/repairs/split-subjects*` against a throwaway Postgres (`BORING_TEST_DATABASE_URL`; skipped
//! without it, run with `--test-threads=1`). Expected status and bytes in
//! `tests/fixtures/split_subjects_python.json` were recorded from the Python door on the same
//! fixture rows (`tests/fixtures/split_subjects_fixture.sql`), 2026-10-28.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use axum::http::StatusCode;
use tower::ServiceExt;

use crate::serve::{AppState, build_router};
use crate::store::Store;

const FIXTURE_SQL: &str = include_str!("../../tests/fixtures/split_subjects_fixture.sql");
const PYTHON_BYTES: &str = include_str!("../../tests/fixtures/split_subjects_python.json");
const OWNER_TOKEN: &str = "tok";

pub(super) fn dsn() -> Option<String> {
    std::env::var("BORING_TEST_DATABASE_URL").ok()
}

pub(super) fn state_with(store: Option<Store>, owner_token: Option<&str>) -> AppState {
    let cfg = crate::config::BoringConfig::default();
    AppState {
        store: store.map(Arc::new),
        code_index: None,
        llm: Arc::new(crate::llm::Llm::from_config(&cfg)),
        vault_dir: Arc::new(None),
        pii: Arc::new(None),
        cfg: Arc::new(cfg),
        cfg_path: Arc::new(None),
        sync_lock: Arc::default(),
        remember_gate: Arc::default(),
        deferred: std::sync::Arc::default(),
        note_index: Arc::default(),
        wiki_index: Arc::default(),
        last_compact: Arc::default(),
        compact_failure: Arc::default(),
        db_healthy_last: Arc::default(),
        owner_token: Arc::new(owner_token.map(str::to_owned)),
    }
}

pub(super) async fn seeded_store(dsn: &str) -> Store {
    let store = Store::open(dsn, 1024).await.expect("open store");
    let (client, conn) = tokio_postgres::connect(dsn, tokio_postgres::NoTls)
        .await
        .expect("connect");
    tokio::spawn(conn);
    client.batch_execute(FIXTURE_SQL).await.expect("fixture");
    store
}

pub(super) async fn call(
    router: &axum::Router,
    method: &str,
    uri: &str,
    headers: &[(&str, &str)],
    body: &str,
) -> (StatusCode, Vec<u8>) {
    let mut req = axum::extract::Request::builder().method(method).uri(uri);
    for (k, v) in headers {
        req = req.header(*k, *v);
    }
    let req = req
        .header("content-type", "application/json")
        .body(axum::body::Body::from(body.to_owned()))
        .unwrap();
    let res = router.clone().oneshot(req).await.unwrap();
    let status = res.status();
    let bytes = axum::body::to_bytes(res.into_body(), 1 << 20)
        .await
        .unwrap();
    (status, bytes.to_vec())
}

#[tokio::test]
async fn reads_answer_the_python_bytes() {
    let Some(dsn) = dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let router = build_router(state_with(
        Some(seeded_store(&dsn).await),
        Some(OWNER_TOKEN),
    ));
    let cases: Vec<serde_json::Value> = serde_json::from_str(PYTHON_BYTES).unwrap();
    let mut wrong = Vec::new();
    for case in &cases {
        let name = case[0].as_str().unwrap();
        let uri = case[2].as_str().unwrap();
        let (status, body) = call(&router, case[1].as_str().unwrap(), uri, &[], "").await;
        let want_status = u16::try_from(case[5].as_u64().unwrap()).unwrap();
        let want_body = case[6].as_str().unwrap();
        if status.as_u16() != want_status || body != want_body.as_bytes() {
            wrong.push(format!(
                "{name}: got {status} {} want {want_status} {want_body}",
                String::from_utf8_lossy(&body)
            ));
        }
    }
    assert!(
        wrong.is_empty(),
        "{} of {} differ:\n{}",
        wrong.len(),
        cases.len(),
        wrong.join("\n")
    );
}

const POST_PYTHON: &str = include_str!("../../tests/fixtures/split_subjects_post_python.json");

pub(super) async fn raw_client(dsn: &str) -> tokio_postgres::Client {
    let (client, conn) = tokio_postgres::connect(dsn, tokio_postgres::NoTls)
        .await
        .unwrap();
    tokio::spawn(conn);
    client
}

/// `(claim rows, notes whose sha is blank)`.
async fn state_counts(dsn: &str) -> (i64, i64) {
    let client = raw_client(dsn).await;
    let claims: i64 = client
        .query_one("select count(*) from claim", &[])
        .await
        .unwrap()
        .get(0);
    let blanked: i64 = client
        .query_one("select count(*) from document where sha = ''", &[])
        .await
        .unwrap()
        .get(0);
    (claims, blanked)
}

/// The door's POST answers (202 cut to the engine's four keys) and the rows left behind, each
/// case from the fixture afresh. Recorded from the Python door, 2026-10-28.
#[tokio::test]
async fn merge_answers_the_python_bytes_and_leaves_the_python_rows() {
    let Some(dsn) = dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let cases: Vec<serde_json::Value> = serde_json::from_str(POST_PYTHON).unwrap();
    let mut wrong = Vec::new();
    for case in &cases {
        let name = case[0].as_str().unwrap();
        let store = seeded_store(&dsn).await;
        raw_client(&dsn)
            .await
            .batch_execute("update document set sha = 'x'")
            .await
            .unwrap();
        let router = build_router(state_with(Some(store), Some(OWNER_TOKEN)));
        let headers: Vec<(&str, &str)> = if case[4].as_object().unwrap().is_empty() {
            vec![]
        } else {
            vec![("x-boring-owner-token", OWNER_TOKEN)]
        };
        let (status, body) = call(
            &router,
            "POST",
            "/repairs/split-subjects",
            &headers,
            case[3].as_str().unwrap(),
        )
        .await;
        let want_status = u16::try_from(case[5].as_u64().unwrap()).unwrap();
        let python: serde_json::Value = serde_json::from_str(case[6].as_str().unwrap()).unwrap();
        let want_body = if want_status == 202 {
            serde_json::to_string(&crate::split_subjects::Merged {
                subject: python["subject"].as_str().unwrap().to_owned(),
                deleted_rows: python["deleted_rows"].as_u64().unwrap(),
                reread_notes: python["reread_notes"].as_u64().unwrap(),
                owner_held: python["owner_held"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|v| v.as_str().unwrap().to_owned())
                    .collect(),
            })
            .unwrap()
        } else {
            case[6].as_str().unwrap().to_owned()
        };
        let want_state = (case[7][0].as_i64().unwrap(), case[7][1].as_i64().unwrap());
        let got_state = state_counts(&dsn).await;
        if status.as_u16() != want_status || body != want_body.as_bytes() || got_state != want_state
        {
            wrong.push(format!(
                "{name}: got {status} {} {got_state:?} want {want_status} {want_body} {want_state:?}",
                String::from_utf8_lossy(&body)
            ));
        }
    }
    assert!(
        wrong.is_empty(),
        "{} of {} differ:\n{}",
        wrong.len(),
        cases.len(),
        wrong.join("\n")
    );
}

const ROUNDS: usize = 40;
const SLOTS: usize = 12;

struct ZeroEmbed;

impl crate::ingest::Embed for ZeroEmbed {
    fn embed(
        &self,
        _text: &str,
    ) -> impl std::future::Future<Output = anyhow::Result<Vec<f32>>> + Send {
        std::future::ready(Ok(vec![0.0_f32; 1024]))
    }
}

/// A note whose claims fill every slot `(subject, p00..)` of the group.
fn slot_note(subject: &str) -> String {
    let claims = (0..SLOTS)
        .map(|i| {
            format!(
                "  - subject: {subject}\n    predicate: p{i:02}\n    value: v-new\n    kind: fact\n    confidence: certain"
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    format!(
        "---\norigin: personal\nproject: \"\"\nkind: note\ntags: []\ntools: []\nconcepts: []\nclaims:\n{claims}\n---\n\nnew\n"
    )
}

/// One group of two spellings over `SLOTS` slots. The canon-spelled rows were written back half
/// first, so a scan walks the slots 6..11 then 0..5 while an ingest seals them 0..11.
async fn seed_group(db: &tokio_postgres::Client, t: &str, x: &str, y: &str) {
    let order: Vec<usize> = (SLOTS / 2..SLOTS).chain(0..SLOTS / 2).collect();
    for (subject, path, slots) in [
        (format!("{t}s"), x, order),
        (format!("{t}S"), y, (0..SLOTS).collect()),
    ] {
        db.execute(
            "insert into document (source_path) values ($1) on conflict do nothing",
            &[&path],
        )
        .await
        .unwrap();
        for i in slots {
            db.execute(
                "insert into claim (subject, predicate, value, source_path, valid_from) values ($1, $2, 'v-old', $3, '2026-01-01')",
                &[&subject, &format!("p{i:02}"), &path],
            )
            .await
            .unwrap();
        }
    }
}

async fn forget_group(db: &tokio_postgres::Client, t: &str) {
    let like = format!("%{t}%");
    db.execute("delete from claim where subject ilike $1", &[&like])
        .await
        .unwrap();
    db.execute("delete from document where source_path like $1", &[&like])
        .await
        .unwrap();
    db.execute(
        "delete from edge where src like $1 or dst like $1",
        &[&like],
    )
    .await
    .unwrap();
    db.execute("delete from node where id like $1", &[&like])
        .await
        .unwrap();
}

/// A split merge against an ingest sealing the same slots, `ROUNDS` times. Returns the rounds in
/// which a request was aborted (deadlock) — the merge locks the slots in the order ingest does.
async fn aborted_rounds(dsn: &str) -> usize {
    let store = Store::open(dsn, 1024).await.expect("open store");
    let db = raw_client(dsn).await;
    let cfg = crate::config::BoringConfig::default();
    let mut aborted = 0;
    for round in 0..ROUNDS {
        let dir = tempfile::tempdir().unwrap();
        let t = format!(
            "c4lock{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let (x, y) = (format!("/c4/{t}-x.md"), format!("/c4/{t}-y.md"));
        seed_group(&db, &t, &x, &y).await;
        let newer = dir.path().join(format!("{t}-new.md"));
        std::fs::write(&newer, slot_note(&format!("{t}s"))).unwrap();
        let newer = newer.to_string_lossy().into_owned();
        let delay = std::time::Duration::from_millis((round % 8) as u64);
        let subject = format!("{t}s");
        let (ingested, merged) = tokio::join!(
            crate::ingest::ingest_file_with::<
                crate::ingest::DefaultChunker,
                crate::ingest::FrontmatterGraphExtractor,
                _,
            >(&store, &ZeroEmbed, &cfg, &newer),
            async {
                tokio::time::sleep(delay).await;
                store
                    .merge_split_subject(&subject, crate::serve::owner::Standing::Owner)
                    .await
            }
        );
        let failure = ingested
            .err()
            .map(|e| format!("ingest: {e:#}"))
            .or(merged.err().map(|e| format!("merge: {e:#}")));
        if let Some(failure) = failure {
            eprintln!("round {round}: {failure}");
            aborted += 1;
        }
        store.delete_document(&newer).await.unwrap();
        forget_group(&db, &t).await;
    }
    aborted
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn merge_and_ingest_lock_one_slots_rows_in_one_order() {
    let Some(dsn) = dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let aborted = aborted_rounds(&dsn).await;
    assert_eq!(aborted, 0, "{aborted}/{ROUNDS} rounds aborted");
}

/// A failure after the claim delete (an UPDATE on `document` that raises) leaves every row as it
/// was: nothing deleted, nothing blanked.
#[tokio::test]
async fn merge_that_fails_midway_changes_no_row() {
    let Some(dsn) = dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = seeded_store(&dsn).await;
    let db = raw_client(&dsn).await;
    db.batch_execute("update document set sha = 'x'")
        .await
        .unwrap();
    db.batch_execute(
        "CREATE OR REPLACE FUNCTION c4_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'c4 injected'; END $$;
         DROP TRIGGER IF EXISTS c4_fail_update ON document;
         CREATE TRIGGER c4_fail_update BEFORE UPDATE ON document FOR EACH ROW EXECUTE FUNCTION c4_fail();",
    )
    .await
    .unwrap();
    let merged = store
        .merge_split_subject("foo-bar", crate::serve::owner::Standing::Owner)
        .await;
    db.batch_execute("DROP TRIGGER c4_fail_update ON document")
        .await
        .unwrap();
    assert!(
        merged.is_err(),
        "the injected failure must reach the caller"
    );
    assert_eq!(
        state_counts(&dsn).await,
        (18, 0),
        "claim rows, blanked notes"
    );
}
