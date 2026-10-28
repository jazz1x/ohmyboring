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
        pg.upsert_claim(
            &format!("subject-{nanos}-{i}"),
            "observed",
            &format!("a long enough value for claim number {i}"),
            &path,
            now - Duration::from_hours(24),
            &emb,
            "risk",
            "certain",
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
