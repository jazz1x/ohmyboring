//! Bookkeeping for the incremental `relates_to` projection (`vault::project_links`).
//!
//! `project_note(B)` reads three relations: the top 6 notes sharing >= 2 graph nodes with B, the
//! 4 notes nearest B within cosine distance 0.40, and (when B has < 2 links) the newest notes of
//! B's project. B's output can therefore only change when B changed, when a changed note moved
//! into B's top-k of the graph or semantic relation, when a changed or deleted note WAS one of
//! B's last links, or when B took the project-recency fallback and a note in its project changed.
//! `relates_projection` remembers B's last links, and `relates_projection_bound` B's raw k-th
//! bound of each relation, so the last three cases are answerable without opening any file.
use std::collections::{BTreeSet, HashMap};
use std::time::SystemTime;

use deadpool_postgres::GenericClient;

use anyhow::{Context, Result};

use super::vector_pg::Pg;
use super::vector_surreal::{SurrealClient, SurrealView, micros_to_time};
use super::{ChangedDocs, Store, VectorRoute};

const WATERMARK_KEY: &str = "relates_projection_watermark";
/// Surreal's watermark is read off Surreal's clock, so it never shares a row with pg's.
const SURREAL_WATERMARK_KEY: &str = "relates_projection_watermark_surreal";
/// Set by a full Surreal pass, cleared by any pg pass: the ledger holds Surreal's rows.
const SURREAL_FULL_KEY: &str = "relates_projection_full_surreal";

/// Same bound as `project_note`'s `semantic_related_docs(.., 0.40)` call.
const SEMANTIC_MAX_DIST: f64 = 0.40;

/// A Surreal run reads `document.ingested_us` from this far before the last watermark. A note's
/// document commit and its edge commits are separate transactions, and a run can start between
/// them; the overlap catches the stamp that landed just before the watermark. A note handled
/// twice gives the same output, so the overlap costs time, not correctness. (C-SURREALCUT2 B1.)
const WATERMARK_OVERLAP_US: i64 = 360_000_000;

/// More changed chunks than this and a Surreal run is a full pass: each changed chunk is one
/// nearest-neighbour scan. A placeholder until the copy's distribution sets it. (C-SURREALCUT2 C.)
const MAX_CHANGED_CHUNKS: usize = 500;

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
    /// The ledger was last written by a pg pass (or never by a full Surreal one): its rows are
    /// not Surreal's, so no incremental run may trust them.
    NoSurrealFull,
    NoWatermark,
    EmptyProjection,
    /// More chunks changed than an incremental Surreal run is worth.
    TooManyChanged,
}

impl FullReason {
    #[must_use]
    pub fn label(self) -> &'static str {
        match self {
            Self::Requested => "requested",
            Self::NoSurrealFull => "no-surreal-full",
            Self::NoWatermark => "no-watermark",
            Self::EmptyProjection => "empty-projection",
            Self::TooManyChanged => "too-many-changed",
        }
    }
}

/// Where an incremental run may start from, or why it may not.
enum Resume {
    From(SystemTime),
    Full(FullReason),
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
        match &self.vector_route {
            VectorRoute::Pgvector => self.projection_scope_pg().await,
            VectorRoute::Surreal(client) => self.projection_scope_surreal(client).await,
        }
    }

    async fn projection_scope_pg(&self) -> Result<ProjectionScope> {
        let watermark = match self.resume_point(WATERMARK_KEY).await? {
            Resume::Full(reason) => return Ok(ProjectionScope::Full(reason)),
            Resume::From(watermark) => watermark,
        };
        let rows = self
            .db()
            .await?
            .query(DIRTY_SQL, &[&watermark, &SEMANTIC_MAX_DIST])
            .await
            .context("relates_projection dirty set")?;
        Ok(ProjectionScope::Changed(
            rows.iter().map(|r| r.get::<_, String>(0)).collect(),
        ))
    }

    /// The Surreal half of `DIRTY_SQL`: the notes written since the watermark, the notes they
    /// reach through the graph and the embeddings, and the notes of their projects come from
    /// Surreal (`projection_dirty.rs`); the ledger tables (`was_linked`, the bounds, the project
    /// fallback) are pg's and are read here.
    async fn projection_scope_surreal(&self, client: &SurrealClient) -> Result<ProjectionScope> {
        let full_marked: bool = self
            .db()
            .await?
            .query_one(
                "SELECT EXISTS (SELECT 1 FROM sync_state WHERE key = $1);",
                &[&SURREAL_FULL_KEY],
            )
            .await
            .context("relates_projection surreal full marker")?
            .get(0);
        if !full_marked {
            return Ok(ProjectionScope::Full(FullReason::NoSurrealFull));
        }
        let watermark = match self.resume_point(SURREAL_WATERMARK_KEY).await? {
            Resume::Full(reason) => return Ok(ProjectionScope::Full(reason)),
            Resume::From(watermark) => watermark,
        };
        let since_us = micros_of(watermark)? - WATERMARK_OVERLAP_US;
        let written = match client.changed_docs(since_us, MAX_CHANGED_CHUNKS).await? {
            ChangedDocs::TooMany => return Ok(ProjectionScope::Full(FullReason::TooManyChanged)),
            ChangedDocs::Within(paths) => paths,
        };
        let live = client.all_doc_paths().await?;
        let bounds = self.projection_bounds().await?;
        let db = self.db().await?;
        let gone: Vec<String> = db
            .query(
                "SELECT p FROM (SELECT src AS p FROM relates_projection
                                UNION SELECT dst FROM relates_projection) g
                 WHERE NOT (p = ANY($1::text[]));",
                &[&live],
            )
            .await
            .context("relates_projection notes that left")?
            .iter()
            .map(|r| r.get(0))
            .collect();
        let changed: Vec<String> = written.iter().chain(&gone).cloned().collect();
        let shared = client.shared_reach(&written, &bounds).await?;
        let near = client
            .near_reach(&written, &bounds, SEMANTIC_MAX_DIST)
            .await?;
        let was_linked: Vec<String> = db
            .query(
                "SELECT DISTINCT src FROM relates_projection WHERE dst = ANY($1::text[]);",
                &[&changed],
            )
            .await
            .context("relates_projection was_linked")?
            .iter()
            .map(|r| r.get(0))
            .collect();
        let neighbours = client.docs_in_projects_of(&written).await?;
        let fallback: Vec<String> = db
            .query(
                "SELECT n.p FROM unnest($1::text[]) AS n(p)
                 WHERE EXISTS (SELECT 1 FROM relates_projection r WHERE r.src = n.p AND r.via = 'recent')
                    OR (SELECT count(*) FROM relates_projection r WHERE r.src = n.p) < 2;",
                &[&neighbours],
            )
            .await
            .context("relates_projection project fallback")?
            .iter()
            .map(|r| r.get(0))
            .collect();
        let dirty: BTreeSet<String> = changed
            .into_iter()
            .chain(shared)
            .chain(near)
            .chain(was_linked)
            .chain(fallback)
            .collect();
        Ok(ProjectionScope::Changed(dirty.into_iter().collect()))
    }

    /// Where the last run of the ledger under `watermark_key` ended, or why an incremental run
    /// has no such place.
    async fn resume_point(&self, watermark_key: &str) -> Result<Resume> {
        let db = self.db().await?;
        let populated: bool = db
            .query_one("SELECT EXISTS (SELECT 1 FROM relates_projection);", &[])
            .await
            .context("relates_projection populated?")?
            .get(0);
        if !populated {
            return Ok(Resume::Full(FullReason::EmptyProjection));
        }
        let watermark: Option<SystemTime> = db
            .query_opt(
                "SELECT value FROM sync_state WHERE key = $1;",
                &[&watermark_key],
            )
            .await
            .context("relates_projection watermark")?
            .map(|r| r.get(0));
        Ok(watermark.map_or(Resume::Full(FullReason::NoWatermark), Resume::From))
    }

    /// Every remembered k-th bound, by the note it belongs to.
    async fn projection_bounds(&self) -> Result<HashMap<String, ProjectionBound>> {
        Ok(self
            .db()
            .await?
            .query(
                "SELECT src, sem_kth_dist, graph_kth_shared, graph_kth_src FROM relates_projection_bound;",
                &[],
            )
            .await
            .context("relates_projection bounds")?
            .iter()
            .map(|r| {
                let shared: Option<i32> = r.get(2);
                let src: Option<String> = r.get(3);
                (
                    r.get::<_, String>(0),
                    ProjectionBound {
                        sem_kth_dist: r.get(1),
                        graph_kth: shared.zip(src),
                    },
                )
            })
            .collect())
    }

    /// `related_docs` with each neighbour's shared-node count.
    ///
    /// # Errors
    /// The query failed.
    pub async fn related_docs_scored(&self, src: &str, limit: i64) -> Result<Vec<(String, i64)>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.related_docs_scored_pg(src, limit).await,
            VectorRoute::Surreal(client) => client.related_docs_scored(src, limit).await,
        }
    }

    /// `semantic_related_docs` with each neighbour's cosine distance.
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
            VectorRoute::Surreal(client) => {
                SurrealView { client }
                    .semantic_related_docs_scored(src, limit, max_dist)
                    .await
            }
        }
    }

    /// Replace the remembered links and k-th bounds of `src` (`dst` = linked note's
    /// `source_path`). Both routes keep this ledger in pg (it outlives the switch-over).
    ///
    /// # Errors
    /// The transaction failed.
    pub async fn replace_projection_links(
        &self,
        src: &str,
        links: &[(String, ProjectionVia)],
        bound: &ProjectionBound,
    ) -> Result<()> {
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

    /// The clock of the database that holds `document`, read at the start of a projection run:
    /// `ingested_at` (pg) and `ingested_us` (Surreal) are written with that same clock, so the
    /// watermark and the rows it is compared with agree.
    ///
    /// # Errors
    /// The query failed.
    pub async fn projection_run_clock(&self) -> Result<SystemTime> {
        match &self.vector_route {
            VectorRoute::Pgvector => Ok(self
                .db()
                .await?
                .query_one("SELECT now();", &[])
                .await
                .context("projection run clock")?
                .get(0)),
            VectorRoute::Surreal(client) => {
                micros_to_time(client.clock_micros().await?, "surreal clock")
            }
        }
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
    pub async fn finish_projection_run(
        &self,
        started: SystemTime,
        scope: &ProjectionScope,
    ) -> Result<()> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.finish_projection_run_pg(started).await,
            VectorRoute::Surreal(client) => {
                self.finish_projection_run_surreal(client, started, scope)
                    .await
            }
        }
    }

    /// pg's `document` is not Surreal's: the notes still alive come from Surreal and are matched
    /// against the pg ledger here. `SURREAL_FULL_KEY` is set only by a full pass, so an
    /// incremental run can tell the ledger was last rebuilt from Surreal's rows.
    async fn finish_projection_run_surreal(
        &self,
        client: &SurrealClient,
        started: SystemTime,
        scope: &ProjectionScope,
    ) -> Result<()> {
        let live = client.all_doc_paths().await?;
        let db = self.db().await?;
        db.execute(
            "DELETE FROM relates_projection WHERE NOT (src = ANY($1::text[]));",
            &[&live],
        )
        .await
        .context("drop projection rows of deleted notes (surreal)")?;
        db.execute(
            "DELETE FROM relates_projection_bound WHERE NOT (src = ANY($1::text[]));",
            &[&live],
        )
        .await
        .context("drop projection bounds of deleted notes (surreal)")?;
        let mut keys = vec![SURREAL_WATERMARK_KEY];
        if matches!(scope, ProjectionScope::Full(_)) {
            keys.push(SURREAL_FULL_KEY);
        }
        for key in keys {
            db.execute(
                "INSERT INTO sync_state (key, value) VALUES ($1, $2)
                 ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;",
                &[&key, &started],
            )
            .await
            .context("set relates_projection surreal watermark")?;
        }
        Ok(())
    }

    async fn finish_projection_run_pg(&self, started: SystemTime) -> Result<()> {
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
        // A pg run rewrote rows the Surreal ledger would trust; the next Surreal run starts full.
        self.db()
            .await?
            .execute(
                "DELETE FROM sync_state WHERE key = $1;",
                &[&SURREAL_FULL_KEY],
            )
            .await
            .context("clear relates_projection surreal full marker")?;
        Ok(())
    }
}

fn micros_of(t: SystemTime) -> Result<i64> {
    let since = t
        .duration_since(SystemTime::UNIX_EPOCH)
        .context("watermark is before 1970")?;
    i64::try_from(since.as_micros()).context("watermark does not fit i64 microseconds")
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
