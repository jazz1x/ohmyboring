//! The six `VectorIndex` methods return the same ids, in the same order, as the SQL they replaced.
//! The oracle below is a verbatim copy of the pre-trait SQL (56bad67 `store.rs`), run straight on a
//! client; the trait side goes through `Store`. Needs `BORING_TEST_DATABASE_URL` (a throwaway DB).
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::too_many_lines)]

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::frontmatter::FrontMatter;
use drudge::store::{Doc, Store};
use drudge::vector::VectorIndex;
use pgvector::Vector;
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
        title: Some("equivalence fixture".to_string()),
        ..Default::default()
    }
}

#[tokio::test]
async fn six_methods_match_the_pre_trait_sql() {
    let Some(dsn) = std::env::var("BORING_TEST_DATABASE_URL").ok() else {
        return;
    };
    let store = Store::open(&dsn, DIM).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    // An HNSW scan counts dead entries (rows other runs and suites deleted) against its 40-candidate
    // window until VACUUM, so a nearest-k query on a reused DB can come back short. Clear them first.
    db.batch_execute("VACUUM chunk, claim;")
        .await
        .expect("vacuum");

    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let project = format!("equiv-{nanos}");
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
        let path = format!("/vault/wiki/equiv-{nanos}-{i}.md");
        let fm = front(&path, &project, origin);
        let emb = tilted_vec(
            [1, 2, 2, 3, 4][i],
            *cos,
            usize::try_from(nanos % 1_000_003).unwrap(),
        );
        store
            .upsert_document(&fm, "sha", now - Duration::from_hours(*doc_age_h))
            .await
            .expect("doc");
        store
            .upsert_chunk(&Doc {
                id: format!("{path}#0"),
                content: format!("chunk {i}"),
                embedding: emb.clone(),
                front: fm,
                chunk_idx: 0,
            })
            .await
            .expect("chunk");
        store
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

    let mut query = vec![0.0_f32; DIM];
    query[0] = 1.0;
    let qvec = Vector::from(query.clone());

    // vector_search — oracle: ORDER BY cosine, all rows.
    let oracle: Vec<String> = db
        .query(
            "SELECT id, content, origin, project, source_path, (embedding <=> $1)::float4 AS dist
                 FROM chunk ORDER BY embedding <=> $1 LIMIT $2;",
            &[&qvec, &50_i64],
        )
        .await
        .unwrap()
        .iter()
        .map(|r| r.get(0))
        .collect();
    let got: Vec<String> = store
        .vector_search(&query, 50)
        .await
        .unwrap()
        .into_iter()
        .map(|h| h.id)
        .collect();
    assert!(!oracle.is_empty(), "vector_search must rank something");
    assert_eq!(got, oracle, "vector_search");

    // vector_search_filtered — project + 48h window drops the 100h-old document.
    let (since, k): (Option<i32>, i64) = (Some(48), 50);
    let oracle: Vec<String> = db
        .query(
            "SELECT c.id, c.content, c.origin, c.project, c.source_path,
                        (c.embedding <=> $1)::float4 AS dist
                 FROM chunk c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE ($3::text IS NULL OR c.project = $3)
                   AND ($4::int IS NULL OR d.updated_at >= now() - make_interval(hours => $4))
                 ORDER BY c.embedding <=> $1
                 LIMIT $2;",
            &[&qvec, &k, &Some(project.as_str()), &since],
        )
        .await
        .unwrap()
        .iter()
        .map(|r| r.get(0))
        .collect();
    let got: Vec<String> = store
        .vector_search_filtered(&query, 50, Some(&project), since)
        .await
        .unwrap()
        .into_iter()
        .map(|h| h.id)
        .collect();
    assert!(
        !oracle.contains(&format!("{}#0", paths[4])),
        "the 100h-old document must fall outside the window"
    );
    assert_eq!(got, oracle, "vector_search_filtered");

    // nearest_document
    let oracle: Option<String> = db
        .query(
            "SELECT d.source_path, MIN(c.embedding <=> $1)::float8 AS dist
                 FROM document d
                 JOIN chunk c ON c.source_path = d.source_path
                 WHERE c.embedding IS NOT NULL
                 GROUP BY d.source_path
                 HAVING MIN(c.embedding <=> $1) <= $2
                 ORDER BY dist ASC
                 LIMIT 1;",
            &[&qvec, &0.5_f64],
        )
        .await
        .unwrap()
        .first()
        .map(|r| r.get(0));
    let got = store
        .nearest_document(&query, 0.5)
        .await
        .unwrap()
        .map(|(p, _)| p);
    assert!(oracle.is_some(), "a document must be within 0.5");
    assert_eq!(got, oracle, "nearest_document");

    // semantic_related_docs — neighbours of document 0, within distance 0.5.
    let oracle: Vec<String> = db
        .query(
            "WITH src AS (
                     SELECT embedding FROM chunk WHERE source_path = $1 AND embedding IS NOT NULL
                 )
                 SELECT c.source_path, MIN(c.embedding <=> s.embedding)::float8 AS dist
                 FROM chunk c, src s
                 WHERE c.source_path <> $1 AND c.embedding IS NOT NULL
                 GROUP BY c.source_path
                 HAVING MIN(c.embedding <=> s.embedding) <= $2
                 ORDER BY dist ASC
                 LIMIT $3;",
            &[&paths[0], &0.5_f64, &10_i64],
        )
        .await
        .unwrap()
        .iter()
        .map(|r| r.get(0))
        .collect();
    let got = store
        .semantic_related_docs(&paths[0], 10, 0.5)
        .await
        .unwrap();
    assert!(!oracle.is_empty(), "need a ranking to compare");
    assert_eq!(got, oracle, "semantic_related_docs");

    // current_claims — company origin excluded, kinds narrowed to risk.
    let exclude = vec!["company".to_string()];
    let kinds = vec!["risk".to_string()];
    let not_user_memory = r"(^|/)(eval-|daily-brief-|weekly-brief-)[^/]*\.md$";
    let oracle: Vec<String> = db
        .query(
            "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence, c.anchor, c.era,
                        c.stale_at, c.stale_reason
                 FROM claim c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE c.superseded_at IS NULL AND c.embedding IS NOT NULL
                   AND NOT (d.origin = ANY($3))
                   AND ($4::text IS NULL OR d.project = $4)
                   AND ($5::text[] IS NULL OR c.kind = ANY($5))
                   AND d.source_path !~ $6
                   AND ($7::text IS NULL OR split_part(c.anchor, ':', 2) LIKE replace(replace($7, '%', '\\%'), '_', '\\_') || '%' ESCAPE '\\')
                   AND ($8::bool OR c.stale_at IS NULL)
                 ORDER BY c.embedding <=> $1
                 LIMIT $2;",
            &[
                &qvec,
                &50_i64,
                &exclude,
                &Some(project.as_str()),
                &Some(kinds.as_slice()),
                &not_user_memory,
                &None::<&str>,
                &false,
            ],
        )
        .await
        .unwrap()
        .iter()
        .map(|r| r.get(0))
        .collect();
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
    assert_eq!(got, oracle, "current_claims");

    // recurrences — newer claim 0 against older 1 and 2 (distance <= 0.2, >= 3 days apart).
    let oracle_rows = db
        .query(
            "SELECT a.source_path, a.subject, a.predicate, a.value, a.kind, a.valid_from,
                        b.source_path, b.subject, b.predicate, b.value, b.kind, b.valid_from,
                        (a.embedding <=> b.embedding)::float4 AS distance,
                        (a.valid_from::date - b.valid_from::date)::bigint AS days_apart,
                        (a.predicate ~* $6) AS label_only
                   FROM claim a
                   JOIN claim b ON b.source_path <> a.source_path
                   JOIN document da ON da.source_path = a.source_path
                   JOIN document db ON db.source_path = b.source_path
                  WHERE a.superseded_at IS NULL
                    AND b.superseded_at IS NULL
                    AND a.kind IN ('risk', 'blocked')
                    AND b.kind IN ('risk', 'blocked')
                    AND a.embedding IS NOT NULL
                    AND b.embedding IS NOT NULL
                    AND length(a.value) >= $3
                    AND length(b.value) >= $3
                    AND a.valid_from >= (NOW() - INTERVAL '1 day' * ($1::bigint))
                    AND (a.valid_from::date - b.valid_from::date) >= ($2::bigint)
                    AND (a.embedding <=> b.embedding) <= $4::real
                    AND ($5::text IS NULL OR da.project = $5)
                    AND ($5::text IS NULL OR db.project = $5)
                  ORDER BY distance ASC, a.valid_from DESC;",
            &[
                &30_i64,
                &3_i64,
                &25_i32,
                &0.2_f32,
                &Some(project.as_str()),
                &"^(incident|decision|status|state|상태|결정|next[-_ ]?(step|action)|action|다음 작업)$",
            ],
        )
        .await
        .unwrap();
    // The SQL order is nearest pair first; a newer claim's group sits where its first pair does.
    let mut oracle: Vec<(String, Vec<String>)> = Vec::new();
    for r in &oracle_rows {
        let newer: String = r.get(0);
        let older: String = r.get(6);
        match oracle.iter_mut().find(|(n, _)| *n == newer) {
            Some((_, olders)) => olders.push(older),
            None => oracle.push((newer, vec![older])),
        }
    }
    let got: Vec<(String, Vec<String>)> = store
        .recurrences(30, Some(&project), 10)
        .await
        .unwrap()
        .into_iter()
        .map(|r| {
            let mut olders: Vec<_> = r.older;
            olders.sort_by_key(|c| c.valid_from);
            (
                r.newer.source_path,
                olders.into_iter().map(|c| c.source_path).collect(),
            )
        })
        .collect();
    let mut oracle_sorted = oracle;
    for (_, olders) in &mut oracle_sorted {
        // the trait sorts a group's olders oldest first; claim ages grow with the fixture index
        olders.sort_by_key(|p| std::cmp::Reverse(paths.iter().position(|q| q == p)));
    }
    assert!(oracle_sorted.len() >= 2, "need more than one newer claim");
    assert_eq!(got, oracle_sorted, "recurrences");

    // The DB is shared with other suites: leave nothing a global nearest-neighbour could hit.
    for path in &paths {
        store.delete_document(path).await.expect("cleanup");
    }
}
