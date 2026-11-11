//! Bookkeeping for the incremental `relates_to` projection (`vault::project_links`).
//!
//! `project_note(B)` reads three relations: the top 6 notes sharing >= 2 graph nodes with B, the
//! 4 notes nearest B within cosine distance 0.40, and (when B has < 2 links) the newest notes of
//! B's project. B's output can therefore only change when B changed, when a changed note moved
//! into B's top-k of the graph or semantic relation, when a changed or deleted note WAS one of
//! B's last links, or when B took the project-recency fallback and a note in its project changed.
//! `relates_projection` remembers B's last links, and `relates_projection_bound` B's raw k-th
//! bound of each relation, so the last three cases are answerable without opening any file.
use std::time::SystemTime;

use deadpool_postgres::GenericClient;

use anyhow::{Context, Result};

use super::vector_pg::Pg;
use super::{Store, VectorRoute};
use crate::vector::VectorIndex;

const WATERMARK_KEY: &str = "relates_projection_watermark";

/// Same bound as `project_note`'s `semantic_related_docs(.., 0.40)` call.
const SEMANTIC_MAX_DIST: f64 = 0.40;

/// A note's raw k-th bounds from the queries `project_note` ran (before dedupe and truncate).
/// `None` = fewer than k came back, so any new neighbour inside the old range would land in.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ProjectionBound {
    pub sem_kth_dist: Option<f64>,
    pub graph_kth: Option<(i32, String)>,
}

impl ProjectionBound {
    /// `graph` = the raw `related_docs(.., graph_limit)` rows, `semantic` = the raw
    /// `semantic_related_docs(.., sem_limit, ..)` rows.
    ///
    /// A side's bound is recorded only when every item of its raw top-k was kept by
    /// `project_note` (`is_kept(path)`): it is not a wiki note, or its stem repeats an earlier
    /// neighbour's, ones that never reach `relates_projection`, so `was_linked` cannot see
    /// them move and the bound would go stale-tight. That side stays NULL (the plain range rule).
    #[must_use]
    pub fn from_raw(
        graph: &[(String, i64)],
        graph_limit: usize,
        semantic: &[(String, f64)],
        sem_limit: usize,
        is_kept: impl Fn(&str) -> bool,
    ) -> Self {
        Self {
            sem_kth_dist: sem_limit
                .checked_sub(1)
                .and_then(|i| semantic.get(..=i))
                .filter(|top| top.iter().all(|(p, _)| is_kept(p)))
                .and_then(<[_]>::last)
                .map(|(_, d)| *d),
            // A count that does not fit i32 leaves the bound unset, i.e. the wider old rule.
            graph_kth: graph_limit
                .checked_sub(1)
                .and_then(|i| graph.get(..=i))
                .filter(|top| top.iter().all(|(p, _)| is_kept(p)))
                .and_then(<[_]>::last)
                .and_then(|(p, n)| Some((i32::try_from(*n).ok()?, format!("doc:{p}")))),
        }
    }
}

/// Where a projected link came from; stored in `relates_projection.via`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProjectionVia {
    Graph,
    Semantic,
    Recent,
}

impl ProjectionVia {
    fn column_value(self) -> &'static str {
        match self {
            Self::Graph => "graph",
            Self::Semantic => "semantic",
            Self::Recent => "recent",
        }
    }
}

/// Why a projection run has to visit every wiki note.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FullReason {
    Requested,
    SurrealRoute,
    NoWatermark,
    EmptyProjection,
}

impl FullReason {
    #[must_use]
    pub fn label(self) -> &'static str {
        match self {
            Self::Requested => "requested",
            Self::SurrealRoute => "surreal-route",
            Self::NoWatermark => "no-watermark",
            Self::EmptyProjection => "empty-projection",
        }
    }
}

/// Which notes one projection run visits. Chosen once at entry, folded once by the caller.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProjectionScope {
    Full(FullReason),
    /// `source_path`s whose `relates_to` can differ from the last run's output.
    Changed(Vec<String>),
}

impl Store {
    /// Decide how much of the vault the next projection run has to visit.
    ///
    /// # Errors
    /// A query failed.
    pub async fn projection_scope(&self, requested_full: bool) -> Result<ProjectionScope> {
        if requested_full {
            return Ok(ProjectionScope::Full(FullReason::Requested));
        }
        if matches!(self.vector_route, VectorRoute::Surreal(_)) {
            return Ok(ProjectionScope::Full(FullReason::SurrealRoute));
        }
        let db = self.db().await?;
        let populated: bool = db
            .query_one("SELECT EXISTS (SELECT 1 FROM relates_projection);", &[])
            .await
            .context("relates_projection populated?")?
            .get(0);
        if !populated {
            return Ok(ProjectionScope::Full(FullReason::EmptyProjection));
        }
        let watermark: Option<SystemTime> = db
            .query_opt(
                "SELECT value FROM sync_state WHERE key = $1;",
                &[&WATERMARK_KEY],
            )
            .await
            .context("relates_projection watermark")?
            .map(|r| r.get(0));
        let Some(watermark) = watermark else {
            return Ok(ProjectionScope::Full(FullReason::NoWatermark));
        };
        let rows = db
            .query(DIRTY_SQL, &[&watermark, &SEMANTIC_MAX_DIST])
            .await
            .context("relates_projection dirty set")?;
        Ok(ProjectionScope::Changed(
            rows.iter().map(|r| r.get::<_, String>(0)).collect(),
        ))
    }

    /// `related_docs` with each neighbour's shared-node count. Surreal route: count 0, because
    /// nothing is remembered there (see `replace_projection_links`).
    ///
    /// # Errors
    /// The query failed.
    pub async fn related_docs_scored(&self, src: &str, limit: i64) -> Result<Vec<(String, i64)>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.related_docs_scored_pg(src, limit).await,
            VectorRoute::Surreal(client) => Ok(client
                .related_docs(src, limit)
                .await?
                .into_iter()
                .map(|p| (p, 0))
                .collect()),
        }
    }

    /// `semantic_related_docs` with each neighbour's distance. Surreal route: distance 0.0, as
    /// above.
    ///
    /// # Errors
    /// The query failed.
    pub async fn semantic_related_docs_scored(
        &self,
        src: &str,
        limit: i64,
        max_dist: f64,
    ) -> Result<Vec<(String, f64)>> {
        match &self.vector_route {
            VectorRoute::Pgvector => {
                Pg(self)
                    .semantic_related_docs_scored(src, limit, max_dist)
                    .await
            }
            VectorRoute::Surreal(_) => Ok(self
                .semantic_related_docs(src, limit, max_dist)
                .await?
                .into_iter()
                .map(|p| (p, 0.0))
                .collect()),
        }
    }

    /// Replace the remembered links and k-th bounds of `src` (`dst` = linked note's
    /// `source_path`). Surreal route: nothing is remembered, because that route always runs a
    /// full pass.
    ///
    /// # Errors
    /// The transaction failed.
    pub async fn replace_projection_links(
        &self,
        src: &str,
        links: &[(String, ProjectionVia)],
        bound: &ProjectionBound,
    ) -> Result<()> {
        if matches!(self.vector_route, VectorRoute::Surreal(_)) {
            return Ok(());
        }
        let dsts: Vec<&str> = links.iter().map(|(d, _)| d.as_str()).collect();
        let vias: Vec<&str> = links.iter().map(|(_, v)| v.column_value()).collect();
        let mut db = self.db().await?;
        let tx = db.transaction().await?;
        tx.execute("DELETE FROM relates_projection WHERE src = $1;", &[&src])
            .await
            .context("clear relates_projection rows")?;
        tx.execute(
            "INSERT INTO relates_projection (src, dst, via)
             SELECT $1, t.dst, t.via FROM unnest($2::text[], $3::text[]) AS t(dst, via);",
            &[&src, &dsts, &vias],
        )
        .await
        .context("insert relates_projection rows")?;
        let (graph_shared, graph_src) = match &bound.graph_kth {
            Some((n, s)) => (Some(*n), Some(s.as_str())),
            None => (None, None),
        };
        tx.execute(
            "INSERT INTO relates_projection_bound (src, sem_kth_dist, graph_kth_shared, graph_kth_src)
             VALUES ($1, $2, $3, $4)
             ON CONFLICT (src) DO UPDATE SET sem_kth_dist = EXCLUDED.sem_kth_dist,
                 graph_kth_shared = EXCLUDED.graph_kth_shared,
                 graph_kth_src = EXCLUDED.graph_kth_src;",
            &[&src, &bound.sem_kth_dist, &graph_shared, &graph_src],
        )
        .await
        .context("upsert relates_projection_bound")?;
        tx.commit().await?;
        Ok(())
    }

    /// The database clock, read at the start of a projection run: `document.ingested_at` is
    /// written with the same clock, so the watermark and the rows it is compared with agree.
    ///
    /// # Errors
    /// The query failed.
    pub async fn projection_run_clock(&self) -> Result<SystemTime> {
        Ok(self
            .db()
            .await?
            .query_one("SELECT now();", &[])
            .await
            .context("projection run clock")?
            .get(0))
    }

    /// Mark `path` as re-ingested now although its file mtime did not move.
    ///
    /// # Errors
    /// The update failed.
    pub async fn touch_ingested_at_on(c: &impl GenericClient, path: &str) -> Result<()> {
        c.execute(
            "UPDATE document SET ingested_at = now() WHERE source_path = $1;",
            &[&path],
        )
        .await
        .context("touch ingested_at")?;
        Ok(())
    }

    /// End of a successful run: forget links of notes that left `document` (they would re-enter
    /// the changed set every run), then record the run's start time.
    ///
    /// # Errors
    /// The write failed.
    pub async fn finish_projection_run(&self, started: SystemTime) -> Result<()> {
        if matches!(self.vector_route, VectorRoute::Surreal(_)) {
            return Ok(());
        }
        self.db()
            .await?
            .execute(
                "DELETE FROM relates_projection r
                 WHERE NOT EXISTS (SELECT 1 FROM document d WHERE d.source_path = r.src);",
                &[],
            )
            .await
            .context("drop projection rows of deleted notes")?;
        self.db()
            .await?
            .execute(
                "DELETE FROM relates_projection_bound b
                 WHERE NOT EXISTS (SELECT 1 FROM document d WHERE d.source_path = b.src);",
                &[],
            )
            .await
            .context("drop projection bounds of deleted notes")?;
        self.db()
            .await?
            .execute(
                "INSERT INTO sync_state (key, value) VALUES ($1, $2)
                 ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;",
                &[&WATERMARK_KEY, &started],
            )
            .await
            .context("set relates_projection watermark")?;
        Ok(())
    }
}

/// $1 = watermark (start of the last run), $2 = semantic distance bound. One round trip, set-wise.
const DIRTY_SQL: &str = "
WITH changed AS (
    SELECT source_path AS p FROM document WHERE ingested_at > $1
    UNION
    SELECT g.p FROM (SELECT src AS p FROM relates_projection
                     UNION SELECT dst FROM relates_projection) g
    WHERE NOT EXISTS (SELECT 1 FROM document d WHERE d.source_path = g.p)
),
-- shared / near: project_note(B) links only B's top-k neighbours, so a changed note X dirties B
-- only if it lands at or inside B's remembered raw k-th bound (relates_projection_bound; ties
-- count, the queries' order has no tiebreak for distance). No row or NULL bound = fewer than k
-- came back (or never recorded), so the plain range rule applies. An X already among B's kept
-- links is caught by was_linked; a stale bound needs a non-kept neighbour to move, and anything
-- beyond the old k-th is also beyond position 8, so B's output cannot differ.
shared AS (
    SELECT substr(e2.src, 5) AS p
    FROM edge e1
    JOIN edge e2 ON e2.dst = e1.dst AND e2.src <> e1.src AND e2.src LIKE 'doc:%'
    LEFT JOIN relates_projection_bound b ON b.src = substr(e2.src, 5)
    WHERE e1.src IN (SELECT 'doc:' || p FROM changed)
      AND e1.dst NOT LIKE 'project:%' AND e1.dst NOT LIKE 'topic:%'
    GROUP BY e1.src, e2.src, b.graph_kth_shared, b.graph_kth_src
    HAVING count(*) >= 2
       AND (b.graph_kth_shared IS NULL
            OR count(*) > b.graph_kth_shared
            OR (count(*) = b.graph_kth_shared AND e1.src <= b.graph_kth_src))
),
near AS (
    SELECT DISTINCT c.source_path AS p
    FROM chunk s
    JOIN chunk c ON c.source_path <> s.source_path
    LEFT JOIN relates_projection_bound b ON b.src = c.source_path
    WHERE s.source_path IN (SELECT p FROM changed)
      AND s.embedding IS NOT NULL AND c.embedding IS NOT NULL
      AND (c.embedding <=> s.embedding) <= COALESCE(b.sem_kth_dist, $2::float8)
),
was_linked AS (
    SELECT src AS p FROM relates_projection WHERE dst IN (SELECT p FROM changed)
),
touched AS (
    SELECT DISTINCT project FROM document
    WHERE source_path IN (SELECT p FROM changed) AND project <> ''
),
fallback AS (
    SELECT d.source_path AS p
    FROM document d
    WHERE d.project IN (SELECT project FROM touched)
      AND (EXISTS (SELECT 1 FROM relates_projection r
                   WHERE r.src = d.source_path AND r.via = 'recent')
           OR (SELECT count(*) FROM relates_projection r WHERE r.src = d.source_path) < 2)
)
SELECT p FROM changed
UNION SELECT p FROM shared
UNION SELECT p FROM near
UNION SELECT p FROM was_linked
UNION SELECT p FROM fallback;";
