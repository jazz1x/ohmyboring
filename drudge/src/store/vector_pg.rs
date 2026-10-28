//! pgvector implementation of `VectorIndex` — the SQL that used to be `Store` methods, moved as-is.

use std::collections::HashMap;
use std::time::SystemTime;

use anyhow::{Context, Result};
use pgvector::Vector;

use super::{
    AnchoredClaim, ClaimRef, DistKind, Hit, INFORMATIVE_VALUE_CHARS, NOT_USER_MEMORY_RE,
    RECURRENCE_MAX_DISTANCE, RECURRENCE_MIN_DAYS_APART, Recurrence, Store, TAUTOLOGICAL_PREDICATES,
    row_to_hit,
};
use crate::frontmatter::Claim;
use crate::vector::VectorIndex;

/// The pgvector side of the route (`vector_route`): derefs to the `Store` so the SQL below reads
/// as it always did.
pub(super) struct Pg<'a>(pub(super) &'a Store);

impl std::ops::Deref for Pg<'_> {
    type Target = Store;
    fn deref(&self) -> &Store {
        self.0
    }
}

impl VectorIndex for Pg<'_> {
    /// Documents semantically nearest to `source_path` by chunk-embedding cosine — the MEANING-based
    /// complement to `related_docs`. `related_docs` only links docs sharing >=2 EXACT concept/tool slugs,
    /// so it misses notes about the same thing in DIFFERENT words (and older / cross-project notes). For
    /// each other doc this takes its single closest chunk to any of this doc's chunks, keeps docs within
    /// `max_dist` (pgvector cosine DISTANCE = 1 - cosine_sim), and returns the nearest `limit`, first.
    async fn semantic_related_docs(
        &self,
        source_path: &str,
        limit: i64,
        max_dist: f64,
    ) -> Result<Vec<String>> {
        let rows = self
            .db()
            .await?
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
                &[&source_path, &max_dist, &limit],
            )
            .await
            .context("semantic related docs")?;
        Ok(rows.iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// Recurrence register: current risk/blocked claims from the last `days` whose value is
    /// semantically near an older current risk/blocked claim from a different note — "the
    /// same mistake again". Read-only, one SQL round-trip; the pairing and the per-newer
    /// grouping happen here.
    ///
    /// The gates reuse the register rules instead of inventing new numbers: a value under
    /// `INFORMATIVE_VALUE_CHARS` reads as a tag, and a predicate that restates its kind
    /// carries no information — such rows still answer, flagged `label_only`.
    async fn recurrences(
        &self,
        days: i64,
        project: Option<&str>,
        limit: i64,
    ) -> Result<Vec<Recurrence>> {
        let rows = self
            .db()
            .await?
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
                    &days,
                    &RECURRENCE_MIN_DAYS_APART,
                    &INFORMATIVE_VALUE_CHARS,
                    &RECURRENCE_MAX_DISTANCE,
                    &project,
                    &TAUTOLOGICAL_PREDICATES,
                ],
            )
            .await
            .context("recurrences")?;
        // Pairs arrive nearest-first, so the first row seen for a newer claim carries that
        // group's smallest distance; the group order below is exactly the sort the register
        // promises. `limit` counts newers, not pairs. Rows of one newer claim are NOT
        // adjacent — the sort interleaves them with other claims' pairs — so grouping goes
        // through a key→index map, not "same as the previous row" (live: 6 newers came out
        // as 16 rows that way).
        let mut out: Vec<Recurrence> = Vec::new();
        let mut index: HashMap<(String, String, String, SystemTime), usize> = HashMap::new();
        for r in &rows {
            let newer = ClaimRef {
                source_path: r.get(0),
                subject: r.get(1),
                predicate: r.get(2),
                value: r.get(3),
                kind: r.get(4),
                valid_from: r.get(5),
            };
            let older = ClaimRef {
                source_path: r.get(6),
                subject: r.get(7),
                predicate: r.get(8),
                value: r.get(9),
                kind: r.get(10),
                valid_from: r.get(11),
            };
            let distance: f32 = r.get(12);
            let days_apart: i64 = r.get(13);
            let label_only: bool = r.get(14);
            let key = (
                newer.source_path.clone(),
                newer.subject.clone(),
                newer.predicate.clone(),
                newer.valid_from,
            );
            if let Some(&i) = index.get(&key) {
                out[i].older.push(older);
            } else {
                index.insert(key, out.len());
                out.push(Recurrence {
                    newer,
                    older: vec![older],
                    distance,
                    days_apart,
                    label_only,
                });
            }
        }
        for row in &mut out {
            row.older.sort_by_key(|c| c.valid_from);
        }
        if limit >= 0 {
            out.truncate(usize::try_from(limit).unwrap_or(out.len()));
        }
        Ok(out)
    }

    /// Query embedding → top-k **current** claims (superseded_at IS NULL). Authority retrieval.
    #[allow(clippy::too_many_arguments)]
    async fn current_claims(
        &self,
        query_emb: &[f32],
        k: i64,
        exclude_origins: &[String],
        project: Option<&str>,
        kinds: Option<&[String]>,
        anchor_path: Option<&str>,
        include_stale: bool,
    ) -> Result<Vec<AnchoredClaim>> {
        let vec = Vector::from(query_emb.to_vec());
        // Honor the SAME origin boundary the recall path applies (retrieve::merge_hits filters by
        // exclude_origins). Claims carry no origin column, but their parent document does — JOIN and
        // filter on it so an injected/cross-origin claim cannot bypass an exclusion that the recalled
        // chunks in the same answer respect (Layer 1: one answer, one consistent origin boundary).
        // `origin = ANY('{}')` is false, so an empty exclusion passes every claim (no behavior change).
        let rows = self
            .db()
            .await?
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
                    &vec,
                    &k,
                    &exclude_origins,
                    &project,
                    &kinds,
                    &NOT_USER_MEMORY_RE,
                    &anchor_path,
                    &include_stale,
                ],
            )
            .await
            .context("current claims")?;
        Ok(rows
            .iter()
            .map(|r| AnchoredClaim {
                claim: Claim {
                    subject: r.get(0),
                    predicate: r.get(1),
                    value: r.get(2),
                    kind: r.get(3),
                    confidence: r.get(4),
                    said_by: None,
                },
                anchor: r.get(5),
                era: r.get(6),
                stale_at: r.get(7),
                stale_reason: r.get(8),
            })
            .collect())
    }

    async fn vector_search(&self, vec: &[f32], k: usize) -> Result<Vec<Hit>> {
        let qvec = Vector::from(vec.to_vec());
        let k_i64 = i64::try_from(k).unwrap_or(i64::MAX);
        let rows = self.db().await?
                        .query(
                "SELECT id, content, origin, project, source_path, (embedding <=> $1)::float4 AS dist
                 FROM chunk ORDER BY embedding <=> $1 LIMIT $2;",
                &[&qvec, &k_i64],
            )
            .await?;
        Ok(rows
            .iter()
            .map(|r| row_to_hit(r, DistKind::VectorCosine))
            .collect())
    }

    /// Vector search with optional project and recency filters.
    /// `since_hours` restricts to chunks whose parent document was updated within the window.
    async fn vector_search_filtered(
        &self,
        vec: &[f32],
        k: usize,
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Result<Vec<Hit>> {
        let qvec = Vector::from(vec.to_vec());
        let k_i64 = i64::try_from(k).unwrap_or(i64::MAX);
        let mut conn = self.db().await?;
        let filtered = project.is_some() || since_hours.is_some();
        let rows = if filtered {
            filtered_rows(&mut conn, &qvec, k_i64, project, since_hours).await?
        } else {
            conn.query(
                "SELECT c.id, c.content, c.origin, c.project, c.source_path,
                        (c.embedding <=> $1)::float4 AS dist
                 FROM chunk c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE ($3::text IS NULL OR c.project = $3)
                   AND ($4::int IS NULL OR d.updated_at >= now() - make_interval(hours => $4))
                 ORDER BY c.embedding <=> $1
                 LIMIT $2;",
                &[&qvec, &k_i64, &project, &since_hours],
            )
            .await?
        };
        Ok(rows
            .iter()
            .map(|r| row_to_hit(r, DistKind::VectorCosine))
            .collect())
    }

    /// Find the single nearest document by mean chunk distance. Used at the remember write gate
    /// to skip near-duplicate session notes. Returns `(source_path, distance)` if within `max_dist`.
    async fn nearest_document(&self, vec: &[f32], max_dist: f64) -> Result<Option<(String, f64)>> {
        let qvec = Vector::from(vec.to_vec());
        let rows = self
            .db()
            .await?
            .query(
                "SELECT d.source_path, MIN(c.embedding <=> $1)::float8 AS dist
                 FROM document d
                 JOIN chunk c ON c.source_path = d.source_path
                 WHERE c.embedding IS NOT NULL
                 GROUP BY d.source_path
                 HAVING MIN(c.embedding <=> $1) <= $2
                 ORDER BY dist ASC
                 LIMIT 1;",
                &[&qvec, &max_dist],
            )
            .await
            .context("nearest document")?;
        Ok(rows.first().map(|r| (r.get(0), r.get(1))))
    }
}

/// The filtered branch of `vector_search_filtered`. The HNSW index returns only ef_search
/// candidates and the filter runs after it, so a filter whose rows are all farther than those
/// candidates got none back. relaxed_order keeps scanning until the pool fills (pgvector 0.8.2
/// README, "Iterative index scans"); it may return rows slightly out of order, so the outer SELECT
/// sorts again. SET LOCAL only takes effect inside a transaction, hence the explicit one.
async fn filtered_rows(
    conn: &mut deadpool_postgres::Object,
    qvec: &Vector,
    k_i64: i64,
    project: Option<&str>,
    since_hours: Option<i32>,
) -> Result<Vec<tokio_postgres::Row>> {
    let tx = conn.transaction().await?;
    tx.batch_execute("SET LOCAL hnsw.iterative_scan = relaxed_order;")
        .await?;
    let mode: String = tx.query_one("SHOW hnsw.iterative_scan;", &[]).await?.get(0);
    anyhow::ensure!(
        mode == "relaxed_order",
        "hnsw.iterative_scan reads {mode:?} after SET LOCAL, expected relaxed_order"
    );
    let rows = tx
        .query(
            "SELECT id, content, origin, project, source_path, dist FROM (
                 SELECT c.id, c.content, c.origin, c.project, c.source_path,
                        (c.embedding <=> $1)::float4 AS dist
                 FROM chunk c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE ($3::text IS NULL OR c.project = $3)
                   AND ($4::int IS NULL OR d.updated_at >= now() - make_interval(hours => $4))
                 ORDER BY c.embedding <=> $1
                 LIMIT $2
             ) s ORDER BY dist LIMIT $2;",
            &[qvec, &k_i64, &project, &since_hours],
        )
        .await?;
    tx.commit().await?;
    Ok(rows)
}
