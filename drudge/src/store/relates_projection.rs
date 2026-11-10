//! Bookkeeping for the incremental `relates_to` projection (`vault::project_links`).
//!
//! `project_note(B)` reads three relations: notes sharing >= 2 graph nodes with B, notes within
//! cosine distance 0.40 of B, and (when B has < 2 links) the newest notes of B's project. B's
//! output can therefore only change when B changed, when a changed note became B's neighbour,
//! when a changed or deleted note WAS one of B's last links, or when B took the project-recency
//! fallback and a note in its project changed. `relates_projection` remembers B's last links so
//! the third and fourth cases are answerable without opening any file.
use std::time::SystemTime;

use deadpool_postgres::GenericClient;

use anyhow::{Context, Result};

use super::{Store, VectorRoute};

const WATERMARK_KEY: &str = "relates_projection_watermark";

/// Same bound as `project_note`'s `semantic_related_docs(.., 0.40)` call.
const SEMANTIC_MAX_DIST: f64 = 0.40;

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

    /// Replace the remembered links of `src` with `links` (`dst` = linked note's `source_path`).
    /// Surreal route: nothing is remembered, because that route always runs a full pass.
    ///
    /// # Errors
    /// The transaction failed.
    pub async fn replace_projection_links(
        &self,
        src: &str,
        links: &[(String, ProjectionVia)],
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
shared AS (
    SELECT substr(e2.src, 5) AS p
    FROM edge e1
    JOIN edge e2 ON e2.dst = e1.dst AND e2.src <> e1.src AND e2.src LIKE 'doc:%'
    WHERE e1.src IN (SELECT 'doc:' || p FROM changed)
      AND e1.dst NOT LIKE 'project:%' AND e1.dst NOT LIKE 'topic:%'
    GROUP BY e1.src, e2.src
    HAVING count(*) >= 2
),
near AS (
    SELECT DISTINCT c.source_path AS p
    FROM chunk s
    JOIN chunk c ON c.source_path <> s.source_path
    WHERE s.source_path IN (SELECT p FROM changed)
      AND s.embedding IS NOT NULL AND c.embedding IS NOT NULL
      AND (c.embedding <=> s.embedding) <= $2::float8
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
