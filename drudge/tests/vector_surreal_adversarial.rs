//! Two attacks on the Surreal route.
//!
//! A. The Surreal copy is frozen at fill time. A row whose origin, project or window membership
//!    changes in pgvector afterwards must not come back through the stale copy.
//! B. Text that is hostile to SurrealQL (quotes, backslash, a record-id shape, an injection
//!    attempt) used as a filter value must match exactly what it says, return no extra rows,
//!    and leave the tables intact.
//!
//! Needs a throwaway pgvector (its document and claim tables are emptied on each run) and a
//! throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test vector_surreal_adversarial -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::too_many_lines)]

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::config::SurrealConfig;
use drudge::frontmatter::FrontMatter;
use drudge::store::{Doc, Store, SurrealClient, VectorRoute};
use drudge::vector::VectorIndex;
use serde_json::json;
use tokio_postgres::NoTls;

const DIM: usize = 1024;
/// A record-id shape closing a string and adding a clause; the run id keeps it unique per run so
/// earlier runs on the same DB cannot match it.
fn hostile_record(nanos: u128) -> String {
    format!(r#"mimir:sat{nanos}" OR true OR ""#)
}
const HOSTILE_INJECTION: &str = r#"a\" ; DELETE chunk; --"#;
/// Every fixture claim is anchored under this directory (`code:<dir><i>.rs:10`).
const ANCHOR_DIR: &str = "src/adv/";

fn tilted_vec(tilt_at: usize, cos: f32, salt: usize) -> Vec<f32> {
    let mut v = vec![0.0; DIM];
    v[0] = cos;
    v[tilt_at] = (1.0 - cos * cos).sqrt();
    v[10 + salt % 1000] = 0.001;
    v
}

struct Fixture {
    store: Store,
    client: SurrealClient,
    db: tokio_postgres::Client,
    project: String,
    nanos: u128,
    paths: Vec<String>,
}

/// Four documents in one project (cos .99 / .90 / .85 / .80), plus one document whose project is
/// the hostile record-id text itself. Each has one chunk and one open risk claim. The Surreal copy
/// is filled from pgvector after all of it is written.
async fn fixture(namespace: &str, hostile_project_doc: bool) -> Fixture {
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").expect("BORING_TEST_DATABASE_URL");
    let url = std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL");
    let config: SurrealConfig =
        serde_json::from_value(json!({"url": url, "namespace": namespace, "database": namespace}))
            .unwrap();
    let client = SurrealClient::new(config.settings(&|n| std::env::var(n).ok()).unwrap()).unwrap();
    let pg = Store::open(&dsn, DIM).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);

    // Throwaway DB, tests run one at a time: start empty so earlier runs cannot crowd the
    // 40-candidate window.
    db.batch_execute("DELETE FROM claim; DELETE FROM document;")
        .await
        .unwrap();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let project = format!("adv-{nanos}");
    let shift = 0.0_f32;
    let now = SystemTime::now();
    let mut paths = Vec::new();
    let mut rows: Vec<(f32, String)> = [0.99_f32, 0.90, 0.85, 0.80]
        .iter()
        .map(|c| (*c, project.clone()))
        .collect();
    if hostile_project_doc {
        rows.push((0.70, hostile_record(nanos)));
    }
    for (i, (cos, doc_project)) in rows.iter().enumerate() {
        let path = format!("/vault/wiki/adv-{nanos}-{i}.md");
        let fm = FrontMatter {
            origin: "personal".to_string(),
            project: doc_project.clone(),
            kind: "note".to_string(),
            source_path: path.clone(),
            title: Some("adversarial fixture".to_string()),
            ..Default::default()
        };
        let emb = tilted_vec(
            1 + i % 3,
            cos - shift,
            usize::try_from(nanos % 1_000_003).unwrap(),
        );
        pg.upsert_document(&fm, "sha", now - Duration::from_hours(1))
            .await
            .expect("doc");
        pg.upsert_chunk(&Doc {
            id: format!("{path}#0"),
            content: format!("chunk {i}"),
            embedding: emb.clone(),
            front: fm,
            chunk_idx: 0,
        })
        .await
        .expect("chunk");
        pg.upsert_claim_with_anchor(
            &format!("subject-{nanos}-{i}"),
            "observed",
            &format!("a long enough value for claim number {i}"),
            &path,
            now - Duration::from_hours(24),
            &emb,
            "risk",
            "certain",
            Some(&format!("code:{ANCHOR_DIR}{i}.rs:10")),
            None,
            None,
        )
        .await
        .expect("claim");
        paths.push(path);
    }
    let tables = client.fill_from(&pg).await.expect("fill");
    assert!(tables.iter().all(drudge::store::FillTable::same), "fill");
    let store = pg.with_vector_route(VectorRoute::Surreal(client.clone()));
    Fixture {
        store,
        client,
        db,
        project,
        nanos,
        paths,
    }
}

fn query_vec() -> Vec<f32> {
    let mut q = vec![0.0_f32; DIM];
    q[0] = 1.0;
    q
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_row_changed_in_pg_after_the_fill_does_not_come_back() {
    let f = fixture("adv_a", false).await;
    let q = query_vec();
    let company = vec!["company".to_string()];

    // before the change the copy and pg agree: all four claims, all four chunks in the window
    let claims = f
        .store
        .current_claims(&q, 50, &company, Some(&f.project), None, None, false)
        .await
        .unwrap();
    assert_eq!(claims.len(), 4, "baseline claims");

    // after the fill: doc 0 turns company, chunk 1 moves project, doc 2 ages out of the window
    f.db.execute(
        "UPDATE document SET origin = 'company' WHERE source_path = $1",
        &[&f.paths[0]],
    )
    .await
    .unwrap();
    f.db.execute(
        "UPDATE chunk SET project = 'moved-away' WHERE id = $1",
        &[&format!("{}#0", f.paths[1])],
    )
    .await
    .unwrap();
    f.db.execute(
        "UPDATE document SET updated_at = now() - interval '100 hours' WHERE source_path = $1",
        &[&f.paths[2]],
    )
    .await
    .unwrap();

    let subjects: Vec<String> = f
        .store
        .current_claims(&q, 50, &company, Some(&f.project), None, None, false)
        .await
        .unwrap()
        .into_iter()
        .map(|a| a.claim.subject)
        .collect();
    assert!(
        !subjects.contains(&format!("subject-{}-0", f.nanos)),
        "claim of a document turned company must be gone: {subjects:?}"
    );
    assert_eq!(subjects.len(), 3, "the other three stay: {subjects:?}");

    let ids: Vec<String> = f
        .store
        .vector_search_filtered(&q, 50, Some(&f.project), Some(48))
        .await
        .unwrap()
        .into_iter()
        .map(|h| h.id)
        .collect();
    assert_eq!(
        ids,
        vec![format!("{}#0", f.paths[0]), format!("{}#0", f.paths[3])],
        "moved-project chunk and out-of-window chunk must be gone"
    );
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn hostile_text_in_filters_matches_exactly_and_breaks_nothing() {
    let f = fixture("adv_b", true).await;
    let q = query_vec();
    let count = |table: &'static str| {
        let client = f.client.clone();
        async move {
            client
                .run(&format!("SELECT VALUE count() FROM {table} GROUP ALL;"))
                .await
                .unwrap()
        }
    };
    let (chunks_before, claims_before) = (count("chunk").await, count("claim").await);
    let hostile_project = hostile_record(f.nanos);
    let hostile = vec![hostile_project.clone(), HOSTILE_INJECTION.to_string()];
    let hostile_subject = format!("subject-{}-4", f.nanos);

    // project = the hostile record-id text: exactly the one document that carries it
    let ids: Vec<String> = f
        .store
        .vector_search_filtered(&q, 50, Some(hostile_project.as_str()), None)
        .await
        .unwrap()
        .into_iter()
        .map(|h| h.id)
        .collect();
    assert_eq!(
        ids,
        vec![format!("{}#0", f.paths[4])],
        "hostile project, chunks"
    );
    let claims = f
        .store
        .current_claims(
            &q,
            50,
            &[],
            Some(hostile_project.as_str()),
            None,
            None,
            false,
        )
        .await
        .unwrap();
    assert_eq!(
        claims
            .into_iter()
            .map(|a| a.claim.subject)
            .collect::<Vec<_>>(),
        vec![hostile_subject.clone()],
        "hostile project, claims"
    );

    // a project that is only the injection text matches nothing
    let ids = f
        .store
        .vector_search_filtered(&q, 50, Some(HOSTILE_INJECTION), None)
        .await
        .unwrap();
    assert!(ids.is_empty(), "injection as project must match nothing");

    // as excluded origins: nothing is excluded (no document has such an origin), so all five stay
    let subjects = f
        .store
        .current_claims(&q, 50, &hostile, None, None, None, false)
        .await
        .unwrap();
    let in_run = subjects
        .iter()
        .filter(|a| {
            a.claim
                .subject
                .starts_with(&format!("subject-{}-", f.nanos))
        })
        .count();
    assert_eq!(
        in_run, 5,
        "hostile exclude_origins must exclude nothing and add nothing"
    );

    // as kinds and as an anchor prefix: no claim has them
    let none = f
        .store
        .current_claims(&q, 50, &[], None, Some(hostile.as_slice()), None, false)
        .await
        .unwrap();
    assert!(none.is_empty(), "hostile kinds must match nothing");
    let none = f
        .store
        .current_claims(&q, 50, &[], None, None, Some(HOSTILE_INJECTION), true)
        .await
        .unwrap();
    assert!(none.is_empty(), "hostile anchor prefix must match nothing");

    assert_eq!(count("chunk").await, chunks_before, "chunk table intact");
    assert_eq!(count("claim").await, claims_before, "claim table intact");
    let pg_chunks: i64 =
        f.db.query_one("SELECT count(*) FROM chunk;", &[])
            .await
            .unwrap()
            .get(0);
    assert!(pg_chunks > 0);
}

async fn subjects_of(
    store: &Store,
    project: &str,
    kinds: Option<&[String]>,
    anchor_path: Option<&str>,
    include_stale: bool,
) -> Vec<String> {
    store
        .current_claims(
            &query_vec(),
            50,
            &["company".to_string()],
            Some(project),
            kinds,
            anchor_path,
            include_stale,
        )
        .await
        .unwrap()
        .into_iter()
        .map(|a| a.claim.subject)
        .collect()
}

/// One claim of a filled fixture stops matching a predicate other than origin/project/window after
/// the fill. The Surreal-backed answer must drop it exactly as the pgvector-backed one does.
async fn claim_stops_matching(
    case: &str,
    change: impl FnOnce(&Fixture) -> String,
    kinds: Option<Vec<String>>,
    anchor_path: Option<&str>,
    include_stale: bool,
) -> Fixture {
    let f = fixture(&format!("adv_{case}"), false).await;
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").expect("BORING_TEST_DATABASE_URL");
    let oracle = Store::open(&dsn, DIM).await.expect("pg-only store");
    let kinds = kinds.as_deref();
    let baseline = subjects_of(&f.store, &f.project, kinds, anchor_path, include_stale).await;
    assert_eq!(baseline.len(), 4, "{case}: baseline");

    f.db.batch_execute(&change(&f)).await.unwrap();

    let gone = format!("subject-{}-0", f.nanos);
    let mut from_pg = subjects_of(&oracle, &f.project, kinds, anchor_path, include_stale).await;
    let mut from_surreal =
        subjects_of(&f.store, &f.project, kinds, anchor_path, include_stale).await;
    from_pg.sort();
    from_surreal.sort();
    assert!(
        !from_pg.contains(&gone),
        "{case}: the oracle itself must drop it"
    );
    assert_eq!(
        from_surreal, from_pg,
        "{case}: Surreal-backed answer must match pg's"
    );
    assert_eq!(from_surreal.len(), 3, "{case}: the other three stay");
    f
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_claim_whose_kind_changed_in_pg_after_the_fill_does_not_come_back() {
    claim_stops_matching(
        "kind",
        |f| {
            format!(
                "UPDATE claim SET kind = 'decision' WHERE subject = 'subject-{}-0'",
                f.nanos
            )
        },
        Some(vec!["risk".to_string()]),
        None,
        false,
    )
    .await;
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_claim_marked_stale_in_pg_after_the_fill_does_not_come_back() {
    let f = claim_stops_matching(
        "stale",
        |f| {
            format!(
                "UPDATE claim SET stale_at = now(), stale_reason = 'gone' WHERE subject = 'subject-{}-0'",
                f.nanos
            )
        },
        None,
        None,
        false,
    )
    .await;
    // control: the stale mark is what dropped it — asking for stale claims too brings it back
    let all = f
        .store
        .current_claims(
            &query_vec(),
            50,
            &["company".to_string()],
            Some(&f.project),
            None,
            None,
            true,
        )
        .await
        .unwrap();
    assert_eq!(all.len(), 4, "include_stale keeps the stale claim");
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_claim_re_anchored_in_pg_after_the_fill_does_not_come_back() {
    claim_stops_matching(
        "anchor",
        |f| {
            format!(
                "UPDATE claim SET anchor = 'code:elsewhere/0.rs:10' WHERE subject = 'subject-{}-0'",
                f.nanos
            )
        },
        None,
        Some(ANCHOR_DIR),
        false,
    )
    .await;
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_note_that_became_a_brief_in_pg_after_the_fill_does_not_come_back() {
    // NOT_USER_MEMORY_RE: eval-/daily-brief-/weekly-brief- notes are never user memory. The
    // document's primary key moves, so its chunk (FK, no ON UPDATE CASCADE) is removed first.
    claim_stops_matching(
        "brief",
        |f| {
            let old = &f.paths[0];
            let new = format!("/vault/wiki/daily-brief-{}.md", f.nanos);
            format!(
                "DELETE FROM chunk WHERE source_path = '{old}';
                 UPDATE document SET source_path = '{new}' WHERE source_path = '{old}';
                 UPDATE claim SET source_path = '{new}' WHERE source_path = '{old}';"
            )
        },
        None,
        None,
        false,
    )
    .await;
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_key_surreal_returns_that_pg_deleted_after_the_fill_is_an_error_not_a_silent_drop() {
    let f = fixture("adv_gone", false).await;
    let q = query_vec();
    f.db.execute(
        "DELETE FROM claim WHERE subject = $1",
        &[&format!("subject-{}-0", f.nanos)],
    )
    .await
    .unwrap();
    f.db.execute(
        "DELETE FROM chunk WHERE id = $1",
        &[&format!("{}#0", f.paths[0])],
    )
    .await
    .unwrap();

    let claims = f
        .store
        .current_claims(&q, 50, &[], Some(&f.project), None, None, false)
        .await
        .expect_err("a claim Surreal still has and pg lost must not be dropped silently");
    assert!(
        claims.to_string().contains("pg 에 없음"),
        "claim drift error, got: {claims:#}"
    );
    let chunks = f
        .store
        .vector_search(&q, 50)
        .await
        .expect_err("a chunk Surreal still has and pg lost must not be dropped silently");
    assert!(
        chunks.to_string().contains("pg 에 없음"),
        "chunk drift error, got: {chunks:#}"
    );
}
