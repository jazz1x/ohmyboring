//! The five Surreal-backed `VectorIndex` queries return the same ids as an exact brute-force
//! nearest-neighbour scan over the same rows. The oracle is plain cosine in Rust over what pgvector
//! holds, so neither an index nor a SQL planner is trusted on its side.
//!
//! Needs a throwaway pgvector database (`BORING_TEST_DATABASE_URL`), a throwaway SurrealDB server
//! (`BORING_TEST_SURREAL_URL`, password in `BORING_SURREAL_PASSWORD`), both holding fewer than the
//! 40-candidate window so that an HNSW answer and an exact one must agree.
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::too_many_lines)]

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::config::SurrealConfig;
use drudge::frontmatter::FrontMatter;
use drudge::store::{Doc, Store, SurrealClient, VectorRoute};
use drudge::vector::VectorIndex;
use pgvector::Vector;
use serde_json::json;
use tokio_postgres::NoTls;

const DIM: usize = 1024;

/// `salt` puts a tiny per-run component on its own axis, so re-runs on one DB never tie.
fn tilted_vec(tilt_at: usize, cos: f32, salt: usize) -> Vec<f32> {
    let mut v = vec![0.0; DIM];
    v[0] = cos;
    v[tilt_at] = (1.0 - cos * cos).sqrt();
    v[10 + salt % 1000] = 0.001;
    v
}

fn front(path: &str, project: &str, origin: &str) -> FrontMatter {
    FrontMatter {
        origin: origin.to_string(),
        project: project.to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("surreal equivalence fixture".to_string()),
        ..Default::default()
    }
}

fn cosine_distance(a: &[f32], b: &[f32]) -> f64 {
    let dot: f64 = a
        .iter()
        .zip(b)
        .map(|(x, y)| f64::from(*x) * f64::from(*y))
        .sum();
    let na: f64 = a.iter().map(|x| f64::from(*x).powi(2)).sum::<f64>().sqrt();
    let nb: f64 = b.iter().map(|x| f64::from(*x).powi(2)).sum::<f64>().sqrt();
    1.0 - dot / (na * nb)
}

struct ChunkRow {
    id: String,
    source_path: String,
    project: String,
    age_hours: f64,
    embedding: Vec<f32>,
}

struct ClaimRow {
    subject: String,
    origin: String,
    project: String,
    kind: String,
    embedding: Vec<f32>,
}

/// Same ids as the brute-force oracle; on a miss, name only what differs and where.
fn assert_same(method: &str, got: &[String], oracle: &[String]) {
    let missing: Vec<_> = oracle.iter().filter(|id| !got.contains(id)).collect();
    let extra: Vec<_> = got.iter().filter(|id| !oracle.contains(id)).collect();
    assert!(
        missing.is_empty() && extra.is_empty(),
        "{method}: got {} oracle {}; missing {missing:?}; extra {extra:?}",
        got.len(),
        oracle.len()
    );
    assert_eq!(got, oracle, "{method}: same ids, different order");
}

fn nearest<T>(rows: Vec<(f64, T)>, k: usize) -> Vec<T> {
    let mut rows = rows;
    rows.sort_by(|a, b| a.0.total_cmp(&b.0));
    rows.into_iter().take(k).map(|(_, t)| t).collect()
}

// Run (throwaway pg and surreal, at most 7 runs on one pg DB to stay under the 40-candidate window):
//   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//   cargo test --test vector_surreal_equivalence -- --ignored
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command above this test"]
async fn five_methods_match_exact_brute_force() {
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").expect("BORING_TEST_DATABASE_URL");
    let surreal_url = std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL");
    let config: SurrealConfig = serde_json::from_value(json!({
        "url": surreal_url, "namespace": "equiv", "database": "equiv"
    }))
    .unwrap();
    let settings = config.settings(&|n| std::env::var(n).ok()).unwrap();
    let client = SurrealClient::new(settings).unwrap();

    let pg_only = Store::open(&dsn, DIM).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);

    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let project = format!("equiv-{nanos}");
    // Each earlier run on this DB left 5 chunks. Shifting every cosine by a run-specific step keeps
    // runs from tying with each other, so an order difference means a wrong answer and not an
    // f32-versus-f64 coin flip between two near-identical distances.
    let earlier_chunks: i64 = db
        .query_one("SELECT count(*) FROM chunk;", &[])
        .await
        .unwrap()
        .get(0);
    #[allow(clippy::cast_precision_loss)]
    let run_shift = (earlier_chunks / 5) as f32 * 0.004;
    let now = SystemTime::now();
    let day = Duration::from_hours(24);

    // (cos to the query axis, origin, age of the document, claim age in days, claim kind)
    let fixtures = [
        (0.99_f32, "personal", 1_u64, 1_u64, "risk"),
        (0.90, "personal", 1, 10, "blocked"),
        (0.85, "personal", 1, 15, "risk"),
        (0.60, "company", 1, 20, "decision"),
        (0.30, "personal", 100, 25, "risk"),
    ];
    let mut paths = Vec::new();
    for (i, (cos, origin, doc_age_h, claim_age_d, kind)) in fixtures.iter().enumerate() {
        let path = format!("/vault/wiki/surreal-{nanos}-{i}.md");
        let fm = front(&path, &project, origin);
        let emb = tilted_vec(
            [1, 2, 2, 3, 4][i],
            *cos - run_shift,
            usize::try_from(nanos % 1_000_003).unwrap(),
        );
        pg_only
            .upsert_document(&fm, "sha", now - Duration::from_hours(*doc_age_h))
            .await
            .expect("doc");
        pg_only
            .upsert_chunk(&Doc {
                id: format!("{path}#0"),
                content: format!("chunk {i}"),
                embedding: emb.clone(),
                front: fm,
                chunk_idx: 0,
            })
            .await
            .expect("chunk");
        pg_only
            .upsert_claim(
                &format!("subject-{nanos}-{i}"),
                "observed",
                &format!("a long enough value for claim number {i}"),
                &path,
                now - day * u32::try_from(*claim_age_d).unwrap(),
                &emb,
                kind,
                "certain",
            )
            .await
            .expect("claim");
        paths.push(path);
    }

    let tables = client.fill_from(&pg_only).await.expect("fill");
    for t in &tables {
        assert!(t.same(), "fill must copy every row: {}", t.line());
    }
    let store = pg_only.with_vector_route(VectorRoute::Surreal(client));

    let mut query = vec![0.0_f32; DIM];
    query[0] = 1.0;

    // The oracle's rows: everything pgvector holds, read once.
    let chunks: Vec<ChunkRow> = db
        .query(
            "SELECT c.id, c.source_path, c.project,
                    extract(epoch from (now() - d.updated_at))::float8 / 3600.0, c.embedding
             FROM chunk c JOIN document d ON d.source_path = c.source_path
             WHERE c.embedding IS NOT NULL;",
            &[],
        )
        .await
        .unwrap()
        .iter()
        .map(|r| ChunkRow {
            id: r.get(0),
            source_path: r.get(1),
            project: r.get(2),
            age_hours: r.get(3),
            embedding: r.get::<_, Vector>(4).to_vec(),
        })
        .collect();
    assert!(
        chunks.len() < 40,
        "fixture must stay inside the 40-candidate window, got {}",
        chunks.len()
    );
    let claims: Vec<ClaimRow> = db
        .query(
            "SELECT c.subject, d.origin, d.project, c.kind, c.embedding
             FROM claim c JOIN document d ON d.source_path = c.source_path
             WHERE c.superseded_at IS NULL AND c.embedding IS NOT NULL;",
            &[],
        )
        .await
        .unwrap()
        .iter()
        .map(|r| ClaimRow {
            subject: r.get(0),
            origin: r.get(1),
            project: r.get(2),
            kind: r.get(3),
            embedding: r.get::<_, Vector>(4).to_vec(),
        })
        .collect();

    // vector_search
    let oracle = nearest(
        chunks
            .iter()
            .map(|c| (cosine_distance(&c.embedding, &query), c.id.clone()))
            .collect(),
        50,
    );
    let got: Vec<String> = store
        .vector_search(&query, 50)
        .await
        .unwrap()
        .into_iter()
        .map(|h| h.id)
        .collect();
    assert!(!oracle.is_empty(), "vector_search must rank something");
    assert_same("vector_search", &got, &oracle);

    // vector_search_filtered — project + 48h window drops the 100h-old document.
    let oracle = nearest(
        chunks
            .iter()
            .filter(|c| c.project == project && c.age_hours <= 48.0)
            .map(|c| (cosine_distance(&c.embedding, &query), c.id.clone()))
            .collect(),
        50,
    );
    let got: Vec<String> = store
        .vector_search_filtered(&query, 50, Some(&project), Some(48))
        .await
        .unwrap()
        .into_iter()
        .map(|h| h.id)
        .collect();
    assert!(
        !oracle.contains(&format!("{}#0", paths[4])),
        "the 100h-old document must fall outside the window"
    );
    assert_same("vector_search_filtered", &got, &oracle);

    // nearest_document
    let oracle = nearest(
        chunks
            .iter()
            .map(|c| (cosine_distance(&c.embedding, &query), c.source_path.clone()))
            .filter(|(d, _)| *d <= 0.5)
            .collect(),
        1,
    );
    let got: Vec<String> = store
        .nearest_document(&query, 0.5)
        .await
        .unwrap()
        .map(|(p, _)| p)
        .into_iter()
        .collect();
    assert!(!oracle.is_empty(), "a document must be within 0.5");
    assert_same("nearest_document", &got, &oracle);

    // semantic_related_docs — neighbours of document 0, within distance 0.5.
    let sources: Vec<&ChunkRow> = chunks
        .iter()
        .filter(|c| c.source_path == paths[0])
        .collect();
    let mut best: std::collections::HashMap<&str, f64> = std::collections::HashMap::new();
    for c in chunks.iter().filter(|c| c.source_path != paths[0]) {
        for s in &sources {
            let d = cosine_distance(&c.embedding, &s.embedding);
            let e = best.entry(c.source_path.as_str()).or_insert(f64::INFINITY);
            *e = e.min(d);
        }
    }
    let oracle = nearest(
        best.into_iter()
            .filter(|(_, d)| *d <= 0.5)
            .map(|(p, d)| (d, p.to_string()))
            .collect(),
        10,
    );
    let got = store
        .semantic_related_docs(&paths[0], 10, 0.5)
        .await
        .unwrap();
    assert!(!oracle.is_empty(), "need a ranking to compare");
    assert_same("semantic_related_docs", &got, &oracle);

    // current_claims — company origin excluded, kinds narrowed to risk.
    let exclude = vec!["company".to_string()];
    let kinds = vec!["risk".to_string()];
    let oracle = nearest(
        claims
            .iter()
            .filter(|c| {
                c.project == project && !exclude.contains(&c.origin) && kinds.contains(&c.kind)
            })
            .map(|c| (cosine_distance(&c.embedding, &query), c.subject.clone()))
            .collect(),
        50,
    );
    let got: Vec<String> = store
        .current_claims(
            &query,
            50,
            &exclude,
            Some(&project),
            Some(kinds.as_slice()),
            None,
            false,
        )
        .await
        .unwrap()
        .into_iter()
        .map(|a| a.claim.subject)
        .collect();
    for filtered_out in [1, 3] {
        assert!(
            !oracle.contains(&format!("subject-{nanos}-{filtered_out}")),
            "kinds+origin must filter subject {filtered_out}"
        );
    }
    assert_same("current_claims", &got, &oracle);

    // An anchor prefix no claim satisfies (the fixture claims carry no anchor) filters all of them.
    let anchored = store
        .current_claims(&query, 50, &[], Some(&project), None, Some("src/"), true)
        .await
        .unwrap();
    assert!(
        anchored.is_empty(),
        "anchor_path must exclude anchor-less claims"
    );

    // recurrences is pinned to pgvector on a Surreal route: it still answers.
    store
        .recurrences(30, Some(&project), 10)
        .await
        .expect("recurrences stays on pgvector");
}
