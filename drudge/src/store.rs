//! Store — pgvector (document/chunk/embedding/FTS) + graph (node/edge tables + recursive CTE).
//!
//! Cross-reference: ENFORCEMENT.md §A (error ADTs) · design decision D5 (claim temporal authority).
//!
//! ## Layers (engine-agnostic graph model)
//! - **pgvector** (`document`, `chunk`): vector (HNSW) + FTS (tsvector) + frontmatter columns.
//! - **graph** (`node`, `edge`): semantic ontology. node = entity, edge = typed relation.
//!   - node id convention: `doc:<source_path>` · `project:<name>` · `topic:<tag>`
//!     · `problem|solution|tool|concept:<slug>` · `attempt:<path>#<idx>`.
//!   - the `document` table is the SSOT for documents; the graph references them by `doc:<path>` id (no duplicate storage).
//! - **traversal**: recursive CTE (`neighbors_khop`) — k-hop works even when the engine is not a graph DB.
//!   If the CTE proves insufficient, lift-and-shift to AGE/SurrealDB (schema is identical).
//!
//! ## Advantage over AGE
//! Every value goes through `tokio-postgres` parameter binding ($1,$2…) → eliminates the cypher string-escaping footgun.
use std::time::SystemTime;

use anyhow::{Context, Result};
use pgvector::Vector;
use serde_json::{Value, json};
use tokio_postgres::types::Json as PgJson;
use tokio_postgres::{Client, NoTls};

use crate::frontmatter::{Claim, FrontMatter};

/// Ingest input (one chunk).
pub struct Doc {
    pub id: String, // "{source_path}#{idx}"
    pub content: String,
    pub embedding: Vec<f32>,
    pub front: FrontMatter,
    pub chunk_idx: usize,
}

#[derive(Debug)]
#[allow(dead_code)] // some fields are for retrieve / display only
pub struct Hit {
    pub id: String,
    pub content: String,
    pub origin: String,
    pub project: String,
    pub source_path: String,
    pub dist: f32,
}

#[derive(Debug)]
pub struct Meta {
    pub origin: String,
    pub project: String,
    pub kind: String,
    pub source_path: String,
}

/// One recency-ordered retrieval — the full body of a single document (chunks joined). Returned by `updated_at` descending.
#[derive(Debug)]
pub struct RecentDoc {
    pub source_path: String,
    pub project: String,
    pub content: String,
    pub tags: Vec<String>,
}

/// Graph size summary (for audit).
#[derive(Debug, Default)]
pub struct GraphStats {
    pub documents: usize,
    pub chunks: usize,
    pub projects: usize,
    pub topics: usize,
    pub claims: usize,
    pub decisions: usize,
    pub risks: usize,
    pub edges: usize,
}

/// GC deletion stats.
#[derive(Debug, Default)]
pub struct GcStats {
    pub tool: usize,
    pub concept: usize,
}

impl GcStats {
    pub const fn total(&self) -> usize {
        self.tool + self.concept
    }
}

/// One query/retrieval event — used for memory utility analytics.
#[derive(Debug)]
pub struct QueryLogRow {
    pub id: i32,
    pub created_at: SystemTime,
    pub endpoint: String,
    pub query: String,
    pub hit_paths: Vec<String>,
    pub sources: Vec<String>,
    pub answer_snippet: String,
    pub latency_ms: Option<i32>,
}

/// One structured workflow event stored in Postgres. Shape follows the OpenTelemetry log model
/// while preserving the legacy adapter keys used by readiness (`component`, `event`, `status`, etc.).
#[derive(Debug)]
pub struct EventLogRow {
    pub id: i64,
    pub observed_at: SystemTime,
    pub time_unix_nano: Option<i64>,
    pub severity_text: String,
    pub severity_number: i32,
    pub service_name: String,
    pub component: String,
    pub event_name: String,
    pub status: String,
    pub trace_id: Option<String>,
    pub span_id: Option<String>,
    pub run_id: Option<String>,
    pub session_id: Option<String>,
    pub workflow: Option<String>,
    pub workflow_node: Option<String>,
    pub workflow_outcome: Option<String>,
    pub body: Value,
    pub attributes: Value,
    pub resource: Value,
}

/// Read filter for the queryable event log.
pub struct EventLogFilter<'a> {
    pub limit: i64,
    pub component: Option<&'a str>,
    pub event_name: Option<&'a str>,
    pub status: Option<&'a str>,
    pub run_id: Option<&'a str>,
    pub workflow: Option<&'a str>,
    pub since_hours: Option<i32>,
}

/// Result of a maintenance compact pass.
#[derive(Debug, Default)]
pub struct CompactReport {
    pub vacuum_ms: u128,
    pub reindex_ms: u128,
    pub prune_query_log: usize,
    pub gc_tool: usize,
    pub gc_concept: usize,
}

/// Compact report with an overall elapsed time.
#[derive(Debug, Default)]
pub struct CompactSummary {
    pub report: CompactReport,
    pub total_ms: u128,
}

/// Semantic graph stats (for audit).
#[derive(Debug, Default)]
pub struct SemanticStats {
    pub tools: usize,
    pub concepts: usize,
    pub uses: usize,
    pub about: usize,
}

pub struct Store {
    db: Client,
    /// Embedding dimension (= `boring.json` `embed_dim`; bge-m3 = 1024). Enforced at every embedding
    /// upsert via `checked_vector` and mirrored by the `vector(dim)` DDL columns created in `open`.
    dim: usize,
}

/// Semantic edge kinds (doc→entity) — the SSOT shared by clear/stats.
/// Kernel A: graph is tool/concept only (`uses`/`about`). Narrative (problem/attempt/solution) lives in
/// the note body markdown, not as graph nodes — so those edge kinds are gone.
const SEMANTIC_EDGE_KINDS: [&str; 3] = ["uses", "about", "claims"];
/// Internal eval fixtures must remain searchable while `make eval` is running, but they are not
/// user memory. Recency and claim surfaces feed briefings/status, so exclude that fixture namespace.
const INTERNAL_EVAL_FIXTURE_RE: &str = r"(^|/)eval-[^/]*\.md$";

/// chunk id ("path#idx") → graph document node id ("doc:path").
fn doc_node_id(chunk_or_path: &str) -> String {
    let path = chunk_or_path
        .rsplit_once('#')
        .map_or(chunk_or_path, |(p, _)| p);
    format!("doc:{path}")
}

async fn pg_count(db: &Client, sql: &str) -> Result<usize> {
    let row = db.query_one(sql, &[]).await?;
    let n: i64 = row.get(0);
    Ok(usize::try_from(n).unwrap_or(0))
}

async fn count_node_kind(db: &Client, kind: &str) -> Result<usize> {
    let row = db
        .query_one("SELECT count(*) FROM node WHERE kind = $1;", &[&kind])
        .await?;
    let n: i64 = row.get(0);
    Ok(usize::try_from(n).unwrap_or(0))
}

async fn count_edge_kind(db: &Client, kind: &str) -> Result<usize> {
    let row = db
        .query_one("SELECT count(*) FROM edge WHERE kind = $1;", &[&kind])
        .await?;
    let n: i64 = row.get(0);
    Ok(usize::try_from(n).unwrap_or(0))
}

impl Store {
    // ── connect + ensure schema ───────────────────────────────────────────────

    /// PostgreSQL connect + pgvector + node/edge graph schema initialization.
    /// `dim` = the embedding dimension (`boring.json` `embed_dim`) → the `vector(dim)` columns.
    #[allow(clippy::too_many_lines)] // schema DDL grows with features; splitting only obscures the one migration block.
    pub async fn open(dsn: &str, dim: usize) -> Result<Self> {
        // connect retry (IO boundary, graceful) — when postgres is started separately via profile
        // drudge waits up to ~10s even if it comes up first (depends_on removed → absorbs startup race).
        let (client, conn) = {
            let mut tries = 0_u32;
            loop {
                match tokio_postgres::connect(dsn, NoTls).await {
                    Ok(pair) => break pair,
                    Err(e) if tries < 9 => {
                        tries += 1;
                        eprintln!("[store] postgres connect retry {tries}/10 … ({e})");
                        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                    }
                    Err(e) => {
                        return Err(anyhow::Error::new(e).context(
                            "postgres connect (retries exhausted) — is Postgres up? \
                             vector mode needs `BORING_VECTOR=on make up` (starts pgvector); \
                             or run wiki-first with BORING_VECTOR unset",
                        ));
                    }
                }
            }
        };
        // spawn the background connection driver.
        tokio::spawn(async move {
            if let Err(e) = conn.await {
                eprintln!("postgres connection error: {e}");
            }
        });

        // DDL parameterized by the embedding dim (`embed_dim`). `vector({dim})` is the only interpolation;
        // dim is a parsed integer (no injection surface). `'{{}}'` escapes the literal empty-array default.
        client
            .batch_execute(&format!(
                "CREATE EXTENSION IF NOT EXISTS vector;
                 CREATE TABLE IF NOT EXISTS document (
                     source_path text PRIMARY KEY,
                     origin      text NOT NULL DEFAULT '',
                     project     text NOT NULL DEFAULT '',
                     kind        text NOT NULL DEFAULT '',
                     title       text,
                     tags        text[] NOT NULL DEFAULT '{{}}',
                     sha         text NOT NULL DEFAULT '',
                     extracted_sha text NOT NULL DEFAULT '',
                     updated_at  timestamptz NOT NULL DEFAULT now()
                 );
                 CREATE TABLE IF NOT EXISTS chunk (
                     id          text PRIMARY KEY,
                     source_path text NOT NULL REFERENCES document(source_path) ON DELETE CASCADE,
                     content     text NOT NULL DEFAULT '',
                     embedding   vector({dim}), -- = boring.json embed_dim (guarded in upsert_chunk)
                     origin      text NOT NULL DEFAULT '',
                     project     text NOT NULL DEFAULT '',
                     kind        text NOT NULL DEFAULT '',
                     chunk_idx   int  NOT NULL DEFAULT 0,
                     tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
                 );
                 CREATE INDEX IF NOT EXISTS chunk_hnsw ON chunk USING hnsw (embedding vector_cosine_ops);
                 CREATE INDEX IF NOT EXISTS chunk_gin  ON chunk USING gin (tsv);
                 CREATE TABLE IF NOT EXISTS node (
                     id      text PRIMARY KEY,
                     kind    text NOT NULL,
                     label   text NOT NULL DEFAULT '',
                     outcome text
                 );
                 CREATE TABLE IF NOT EXISTS edge (
                     src  text NOT NULL,
                     dst  text NOT NULL,
                     kind text NOT NULL,
                     PRIMARY KEY (src, dst, kind)
                 );
                 CREATE INDEX IF NOT EXISTS edge_src ON edge(src);
                 CREATE INDEX IF NOT EXISTS edge_dst ON edge(dst);
                 ALTER TABLE document ADD COLUMN IF NOT EXISTS extracted_sha text NOT NULL DEFAULT '';
                 ALTER TABLE document ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
                 CREATE INDEX IF NOT EXISTS document_updated ON document(updated_at DESC);
                 -- claim: temporal fact authority (Graphiti-style invalidation, scaled down for personal use).
                 --   current value of (subject,predicate) = superseded_at IS NULL. A new value seals the old.
                 CREATE TABLE IF NOT EXISTS claim (
                     subject       text NOT NULL,
                     predicate     text NOT NULL,
                     value         text NOT NULL,
                     source_path   text NOT NULL,
                     valid_from    timestamptz NOT NULL,
                     superseded_at timestamptz,
                     embedding     vector({dim}), -- = boring.json embed_dim
                     kind          text NOT NULL DEFAULT 'fact',
                     confidence    text NOT NULL DEFAULT 'certain',
                     PRIMARY KEY (subject, predicate, valid_from)
                 );
                 ALTER TABLE claim ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'fact';
                 ALTER TABLE claim ADD COLUMN IF NOT EXISTS confidence text NOT NULL DEFAULT 'certain';
                 CREATE INDEX IF NOT EXISTS claim_current ON claim(subject, predicate)
                     WHERE superseded_at IS NULL;
                 CREATE INDEX IF NOT EXISTS claim_kind ON claim(kind)
                     WHERE superseded_at IS NULL;
                 CREATE INDEX IF NOT EXISTS claim_hnsw ON claim USING hnsw (embedding vector_cosine_ops)
                     WHERE superseded_at IS NULL;
                 CREATE TABLE IF NOT EXISTS query_log (
                     id            serial PRIMARY KEY,
                     created_at    timestamptz NOT NULL DEFAULT now(),
                     endpoint      text NOT NULL,
                     query         text NOT NULL DEFAULT '',
                     hit_paths     text[] NOT NULL DEFAULT '{{}}',
                     sources       text[] NOT NULL DEFAULT '{{}}',
                     answer_snippet text NOT NULL DEFAULT '',
                     latency_ms    int
                 );
                 CREATE INDEX IF NOT EXISTS query_log_created ON query_log(created_at DESC);
                 CREATE TABLE IF NOT EXISTS event_log (
                     id               bigserial PRIMARY KEY,
                     observed_at      timestamptz NOT NULL DEFAULT now(),
                     time_unix_nano   bigint,
                     severity_text    text NOT NULL DEFAULT 'INFO',
                     severity_number  int NOT NULL DEFAULT 9,
                     service_name     text NOT NULL DEFAULT '',
                     component        text NOT NULL DEFAULT '',
                     event_name       text NOT NULL DEFAULT '',
                     status           text NOT NULL DEFAULT '',
                     trace_id         text,
                     span_id          text,
                     run_id           text,
                     session_id       text,
                     workflow         text,
                     workflow_node    text,
                     workflow_outcome text,
                     body             jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                     attributes       jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                     resource         jsonb NOT NULL DEFAULT '{{}}'::jsonb
                 );
                 CREATE INDEX IF NOT EXISTS event_log_observed ON event_log(observed_at DESC, id DESC);
                 CREATE INDEX IF NOT EXISTS event_log_component ON event_log(component, observed_at DESC);
                 CREATE INDEX IF NOT EXISTS event_log_event ON event_log(event_name, observed_at DESC);
                 CREATE INDEX IF NOT EXISTS event_log_status ON event_log(status, observed_at DESC);
                 CREATE INDEX IF NOT EXISTS event_log_run_id ON event_log(run_id, observed_at DESC);"
            ))
            .await
            .context("pgvector + graph schema")?;

        Ok(Self { db: client, dim })
    }

    /// Build a pgvector `Vector` after checking the dimension matches the `vector(dim)` columns.
    /// Single boundary guard shared by every embedding insert (chunk + claim) — parse-don't-validate:
    /// a model whose output dim ≠ `embed_dim` fails loud here with an actionable message, not with a
    /// cryptic Postgres error deep in the fire-and-forget scheduler.
    fn checked_vector(&self, embedding: &[f32]) -> Result<Vector> {
        if embedding.len() != self.dim {
            anyhow::bail!(
                "embedding dim mismatch: got {}, expected {}. boring.json embed_model must output \
                 {}-dim vectors (embed_dim), or change embed_dim + `make reset`.",
                embedding.len(),
                self.dim,
                self.dim
            );
        }
        Ok(Vector::from(embedding.to_vec()))
    }

    // ── document ─────────────────────────────────────────────────────────────

    pub async fn get_doc_sha(&self, path: &str) -> Result<Option<String>> {
        let rows = self
            .db
            .query("SELECT sha FROM document WHERE source_path = $1;", &[&path])
            .await?;
        Ok(rows.first().map(|r| r.get::<_, String>(0)))
    }

    pub async fn all_doc_paths(&self) -> Result<Vec<String>> {
        let rows = self
            .db
            .query("SELECT source_path FROM document;", &[])
            .await?;
        Ok(rows.iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// Document mtime — the `valid_from` of a claim (temporal sort key). Falls back to now() if absent (graceful).
    pub async fn doc_updated_at(&self, path: &str) -> Result<SystemTime> {
        let rows = self
            .db
            .query(
                "SELECT updated_at FROM document WHERE source_path = $1;",
                &[&path],
            )
            .await?;
        Ok(rows
            .first()
            .map_or_else(SystemTime::now, |r| r.get::<_, SystemTime>(0)))
    }

    /// Update only the recency of documents whose content is unchanged (same sha) — mtime backfill without re-embedding.
    /// Ensures the recency-first sort key (`updated_at`) is also populated for existing documents.
    pub async fn set_updated_at(&self, path: &str, updated_at: SystemTime) -> Result<()> {
        self.db
            .execute(
                "UPDATE document SET updated_at = $2
                 WHERE source_path = $1 AND updated_at IS DISTINCT FROM $2;",
                &[&path, &updated_at],
            )
            .await
            .context("touch updated_at")?;
        Ok(())
    }

    /// Top-N documents by recency — full body (chunks joined). Retrieval for the recency-first/supersede briefing.
    /// Ordered by `updated_at` descending rather than semantic similarity = "most recently changed knowledge on top".
    /// If `since_hours` is Some, only documents updated within that window are returned.
    pub async fn recent_docs(
        &self,
        limit: i64,
        exclude_origins: &[String],
        since_hours: Option<i32>,
        project: Option<&str>,
    ) -> Result<Vec<RecentDoc>> {
        let rows = match since_hours {
            Some(hours) => self
                .db
                .query(
                    "SELECT d.source_path, d.project, d.tags,
                                string_agg(c.content, E'\n' ORDER BY c.chunk_idx) AS content
                         FROM document d
                         JOIN chunk c ON c.source_path = d.source_path
                         WHERE NOT (d.origin = ANY($2))
                           AND d.updated_at >= now() - make_interval(hours => $3)
                           AND ($4::text IS NULL OR d.project = $4)
                           AND d.source_path !~ $5
                         GROUP BY d.source_path, d.project, d.tags, d.updated_at
                         ORDER BY d.updated_at DESC
                         LIMIT $1;",
                    &[
                        &limit,
                        &exclude_origins,
                        &hours,
                        &project,
                        &INTERNAL_EVAL_FIXTURE_RE,
                    ],
                )
                .await
                .context("recent docs with time window")?,
            None => self
                .db
                .query(
                    "SELECT d.source_path, d.project, d.tags,
                                string_agg(c.content, E'\n' ORDER BY c.chunk_idx) AS content
                         FROM document d
                         JOIN chunk c ON c.source_path = d.source_path
                         WHERE NOT (d.origin = ANY($2))
                           AND ($3::text IS NULL OR d.project = $3)
                           AND d.source_path !~ $4
                         GROUP BY d.source_path, d.project, d.tags, d.updated_at
                         ORDER BY d.updated_at DESC
                         LIMIT $1;",
                    &[
                        &limit,
                        &exclude_origins,
                        &project,
                        &INTERNAL_EVAL_FIXTURE_RE,
                    ],
                )
                .await
                .context("recent docs")?,
        };
        Ok(rows
            .iter()
            .map(|r| RecentDoc {
                source_path: r.get(0),
                project: r.get(1),
                tags: r.get(2),
                content: r.get(3),
            })
            .collect())
    }

    /// Document↔document relations — other documents that share **concrete** semantic nodes
    /// (concept·tool·problem·solution), ordered by shared count descending. The basis for the Obsidian relates_to projection.
    /// 2-hop over the graph (edge): doc → (shared dst) ← otherDoc.
    /// `project:`/`topic:` are excluded — the same project / common tags would link everything and create a hairball.
    /// Requires at least 2 shared nodes to link (cuts the noise of an accidental single overlap).
    pub async fn related_docs(&self, source_path: &str, limit: i64) -> Result<Vec<String>> {
        let doc_id = doc_node_id(source_path);
        let rows = self
            .db
            .query(
                "WITH self_nodes AS (
                     SELECT dst FROM edge WHERE src = $1
                     AND dst NOT LIKE 'project:%' AND dst NOT LIKE 'topic:%'
                 )
                 SELECT e.src, count(*) AS shared
                 FROM edge e JOIN self_nodes sn ON e.dst = sn.dst
                 WHERE e.src <> $1 AND e.src LIKE 'doc:%'
                 GROUP BY e.src
                 HAVING count(*) >= 2
                 ORDER BY shared DESC, e.src ASC
                 LIMIT $2;",
                &[&doc_id, &limit],
            )
            .await
            .context("related docs")?;
        // 'doc:<source_path>' → restore source_path
        Ok(rows
            .iter()
            .map(|r| {
                let id: String = r.get(0);
                id.strip_prefix("doc:").unwrap_or(&id).to_owned()
            })
            .collect())
    }

    /// Documents semantically nearest to `source_path` by chunk-embedding cosine — the MEANING-based
    /// complement to `related_docs`. `related_docs` only links docs sharing >=2 EXACT concept/tool slugs,
    /// so it misses notes about the same thing in DIFFERENT words (and older / cross-project notes). For
    /// each other doc this takes its single closest chunk to any of this doc's chunks, keeps docs within
    /// `max_dist` (pgvector cosine DISTANCE = 1 - cosine_sim), and returns the nearest `limit`, first.
    pub async fn semantic_related_docs(
        &self,
        source_path: &str,
        limit: i64,
        max_dist: f64,
    ) -> Result<Vec<String>> {
        let rows = self
            .db
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

    /// GraphRAG retrieval: the body of the top-N connected documents that **share a concrete concept/tool** with a document.
    /// Surfaces, via the graph (concept links), the right answer that the vector buried in noise. project/topic excluded.
    pub async fn related_doc_content(
        &self,
        source_path: &str,
        limit: i64,
    ) -> Result<Vec<RecentDoc>> {
        let doc_id = doc_node_id(source_path);
        let rows = self
            .db
            .query(
                "WITH self_nodes AS (
                     SELECT dst FROM edge WHERE src = $1
                     AND dst NOT LIKE 'project:%' AND dst NOT LIKE 'topic:%'
                 ),
                 ranked AS (
                     SELECT e.src AS doc_node, count(*) AS shared
                     FROM edge e JOIN self_nodes sn ON e.dst = sn.dst
                     WHERE e.src <> $1 AND e.src LIKE 'doc:%'
                     GROUP BY e.src HAVING count(*) >= 2 ORDER BY shared DESC, e.src ASC LIMIT $2
                 )
                 SELECT d.source_path, d.project, d.tags,
                        string_agg(c.content, E'\n' ORDER BY c.chunk_idx) AS content
                 FROM ranked r
                 JOIN document d ON ('doc:' || d.source_path) = r.doc_node
                 JOIN chunk c ON c.source_path = d.source_path
                 GROUP BY d.source_path, d.project, d.tags, r.shared
                 ORDER BY r.shared DESC;",
                &[&doc_id, &limit],
            )
            .await
            .context("related doc content")?;
        Ok(rows
            .iter()
            .map(|r| RecentDoc {
                source_path: r.get(0),
                project: r.get(1),
                tags: r.get(2),
                content: r.get(3),
            })
            .collect())
    }

    /// The most recent other documents in the same project — fallback links for isolated documents (0 concept overlap).
    /// Supplements only when there are no concept-based links to prevent orphans, but only a few so it doesn't become a mesh.
    pub async fn recent_project_docs(&self, source_path: &str, limit: i64) -> Result<Vec<String>> {
        let rows = self
            .db
            .query(
                "SELECT d2.source_path FROM document d1
                 JOIN document d2 ON d2.project = d1.project
                     AND d2.source_path <> d1.source_path
                 WHERE d1.source_path = $1 AND d1.project <> ''
                 ORDER BY d2.updated_at DESC
                 LIMIT $2;",
                &[&source_path, &limit],
            )
            .await
            .context("recent project docs")?;
        Ok(rows.iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// Temporal fact claim upsert + supersede. For the same `(subject,predicate)`, old values are
    /// sealed via `superseded_at`, and only the latest `valid_from` row is current (NULL). Idempotent (re-ingesting the same row is harmless).
    /// 0 extra gemma calls — takes the claims that extract already produced as-is.
    #[allow(clippy::too_many_arguments)]
    pub async fn upsert_claim(
        &self,
        subject: &str,
        predicate: &str,
        value: &str,
        source_path: &str,
        valid_from: SystemTime,
        embedding: &[f32],
        kind: &str,
        confidence: &str,
    ) -> Result<()> {
        let vec = self.checked_vector(embedding)?; // dim guard (shared with upsert_chunk)
        self.db
            .execute(
                "INSERT INTO claim (subject, predicate, value, source_path, valid_from, embedding, kind, confidence)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                 ON CONFLICT (subject, predicate, valid_from) DO UPDATE SET
                     value = EXCLUDED.value, source_path = EXCLUDED.source_path,
                     embedding = EXCLUDED.embedding, kind = EXCLUDED.kind, confidence = EXCLUDED.confidence;",
                &[
                    &subject,
                    &predicate,
                    &value,
                    &source_path,
                    &valid_from,
                    &vec,
                    &kind,
                    &confidence,
                ],
            )
            .await
            .context("insert claim")?;
        // seal everything below the latest valid_from, leaving only the single latest row as current.
        self.db
            .execute(
                "UPDATE claim c SET superseded_at = m.mx
                 FROM (SELECT subject, predicate, max(valid_from) AS mx FROM claim
                       WHERE subject = $1 AND predicate = $2 GROUP BY subject, predicate) m
                 WHERE c.subject = m.subject AND c.predicate = m.predicate
                   AND c.valid_from < m.mx AND c.superseded_at IS DISTINCT FROM m.mx;",
                &[&subject, &predicate],
            )
            .await
            .context("seal old claims")?;
        self.db
            .execute(
                "UPDATE claim SET superseded_at = NULL
                 WHERE subject = $1 AND predicate = $2 AND superseded_at IS NOT NULL
                   AND valid_from = (SELECT max(valid_from) FROM claim
                                     WHERE subject = $1 AND predicate = $2);",
                &[&subject, &predicate],
            )
            .await
            .context("unseal latest claim")?;
        Ok(())
    }

    /// Upsert graph nodes/edges for a claim: `doc —claims→ claim:{subject}:{predicate}` and,
    /// for non-fact claims, a typed node (`decision:|risk:...`) plus an `is_a` edge.
    /// Also links the claim node to `project:{project}` when a project is present.
    pub async fn upsert_claim_node(
        &self,
        path: &str,
        project: &str,
        claim: &crate::frontmatter::Claim,
    ) -> Result<()> {
        let claim_id = format!("claim:{}:{}", claim.subject, claim.predicate);
        let label = format!("{}: {}", claim.predicate, claim.value);
        let kind = claim.kind();
        let confidence = claim.confidence();

        // claim node
        self.upsert_node(&claim_id, "claim", &label, Some(kind))
            .await?;

        // typed node for decisions/risks/etc.
        if kind != "fact" {
            let typed_id = format!("{}:{}:{}", kind, claim.subject, claim.predicate);
            let typed_label = format!("{} — {}", claim.subject, claim.value);
            self.upsert_node(&typed_id, kind, &typed_label, Some(confidence))
                .await?;
            self.upsert_edge(&claim_id, &typed_id, "is_a").await?;
        }

        // doc -> claim edge
        let doc_id = doc_node_id(path);
        self.upsert_edge(&doc_id, &claim_id, "claims").await?;

        // claim -> project edge
        if !project.is_empty() {
            let project_id = format!("project:{project}");
            self.upsert_edge(&claim_id, &project_id, "about").await?;
        }

        Ok(())
    }

    /// Top-k **current** claims (superseded_at IS NULL) by recency (valid_from desc). For injecting authority into the briefing.
    pub async fn recent_claims(
        &self,
        k: i64,
        project: Option<&str>,
        kinds: Option<&[String]>,
        exclude_origins: &[String],
    ) -> Result<Vec<Claim>> {
        let rows = self
            .db
            .query(
                "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence FROM claim c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE c.superseded_at IS NULL
                   AND ($2::text IS NULL OR d.project = $2)
                   AND ($3::text[] IS NULL OR c.kind = ANY($3))
                   AND NOT (d.origin = ANY($4))
                   AND d.source_path !~ $5
                 ORDER BY c.valid_from DESC
                 LIMIT $1;",
                &[
                    &k,
                    &project,
                    &kinds,
                    &exclude_origins,
                    &INTERNAL_EVAL_FIXTURE_RE,
                ],
            )
            .await
            .context("recent claims")?;
        Ok(rows
            .iter()
            .map(|r| Claim {
                subject: r.get(0),
                predicate: r.get(1),
                value: r.get(2),
                kind: r.get(3),
                confidence: r.get(4),
            })
            .collect())
    }

    /// Stalled claims: current claims whose valid_from is older than `older_than_days`.
    /// Ordered oldest-first so the longest-frozen items surface first.
    pub async fn stalled_claims(
        &self,
        k: i64,
        project: Option<&str>,
        kinds: Option<&[String]>,
        exclude_origins: &[String],
        older_than_days: i64,
    ) -> Result<Vec<Claim>> {
        let rows = self
            .db
            .query(
                "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence FROM claim c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE c.superseded_at IS NULL
                   AND c.valid_from < (NOW() - INTERVAL '1 day' * ($5::bigint))
                   AND ($2::text IS NULL OR d.project = $2)
                   AND ($3::text[] IS NULL OR c.kind = ANY($3))
                   AND NOT (d.origin = ANY($4))
                   AND d.source_path !~ $6
                 ORDER BY c.valid_from ASC
                 LIMIT $1;",
                &[
                    &k,
                    &project,
                    &kinds,
                    &exclude_origins,
                    &older_than_days,
                    &INTERNAL_EVAL_FIXTURE_RE,
                ],
            )
            .await
            .context("stalled claims")?;
        Ok(rows
            .iter()
            .map(|r| Claim {
                subject: r.get(0),
                predicate: r.get(1),
                value: r.get(2),
                kind: r.get(3),
                confidence: r.get(4),
            })
            .collect())
    }

    /// Query embedding → top-k **current** claims (superseded_at IS NULL). Authority retrieval.
    pub async fn current_claims(
        &self,
        query_emb: &[f32],
        k: i64,
        exclude_origins: &[String],
        project: Option<&str>,
        kinds: Option<&[String]>,
    ) -> Result<Vec<Claim>> {
        let vec = Vector::from(query_emb.to_vec());
        // Honor the SAME origin boundary the recall path applies (retrieve::merge_hits filters by
        // exclude_origins). Claims carry no origin column, but their parent document does — JOIN and
        // filter on it so an injected/cross-origin claim cannot bypass an exclusion that the recalled
        // chunks in the same answer respect (Layer 1: one answer, one consistent origin boundary).
        // `origin = ANY('{}')` is false, so an empty exclusion passes every claim (no behavior change).
        let rows = self
            .db
            .query(
                "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence FROM claim c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE c.superseded_at IS NULL AND c.embedding IS NOT NULL
                   AND NOT (d.origin = ANY($3))
                   AND ($4::text IS NULL OR d.project = $4)
                   AND ($5::text[] IS NULL OR c.kind = ANY($5))
                   AND d.source_path !~ $6
                 ORDER BY c.embedding <=> $1
                 LIMIT $2;",
                &[
                    &vec,
                    &k,
                    &exclude_origins,
                    &project,
                    &kinds,
                    &INTERNAL_EVAL_FIXTURE_RE,
                ],
            )
            .await
            .context("current claims")?;
        Ok(rows
            .iter()
            .map(|r| Claim {
                subject: r.get(0),
                predicate: r.get(1),
                value: r.get(2),
                kind: r.get(3),
                confidence: r.get(4),
            })
            .collect())
    }

    /// document upsert + project/topic nodes + in_project/tagged edge regeneration (idempotent).
    /// `updated_at` = source file mtime (the true recency signal) — the sort key for recency-first retrieval.
    pub async fn upsert_document(
        &self,
        front: &FrontMatter,
        sha: &str,
        updated_at: SystemTime,
    ) -> Result<()> {
        let path = &front.source_path;
        let title_ref: Option<&str> = front.title.as_deref();
        self.db
            .execute(
                "INSERT INTO document (source_path, origin, project, kind, title, tags, sha, updated_at)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                 ON CONFLICT (source_path) DO UPDATE SET
                     origin = EXCLUDED.origin, project = EXCLUDED.project, kind = EXCLUDED.kind,
                     title = EXCLUDED.title, tags = EXCLUDED.tags, sha = EXCLUDED.sha,
                     updated_at = EXCLUDED.updated_at;",
                &[
                    path,
                    &front.origin,
                    &front.project,
                    &front.kind,
                    &title_ref,
                    &front.tags,
                    &sha,
                    &updated_at,
                ],
            )
            .await
            .context("upsert document")?;

        let doc_id = doc_node_id(path);

        // project node + in_project edge
        if !front.project.is_empty() {
            let pid = format!("project:{}", front.project);
            self.upsert_node(&pid, "project", &front.project, None)
                .await?;
            self.upsert_edge(&doc_id, &pid, "in_project").await?;
        }

        // tagged: remove existing then regenerate (idempotent)
        self.db
            .execute(
                "DELETE FROM edge WHERE src = $1 AND kind = 'tagged';",
                &[&doc_id],
            )
            .await?;
        for tag in &front.tags {
            let tid = format!("topic:{tag}");
            self.upsert_node(&tid, "topic", tag, None).await?;
            self.upsert_edge(&doc_id, &tid, "tagged").await?;
        }
        Ok(())
    }

    /// Prune chunks at or beyond `from_idx` for a document — the stale tail left when a re-ingested
    /// note has FEWER chunks than before. Used by the upsert-then-prune re-ingest so a reader never
    /// sees an empty/half-deleted chunk set (no delete-first window). `from_idx == new chunk count`.
    pub async fn prune_chunks_from(&self, path: &str, from_idx: usize) -> Result<()> {
        let from = i32::try_from(from_idx).unwrap_or(i32::MAX);
        self.db
            .execute(
                "DELETE FROM chunk WHERE source_path = $1 AND chunk_idx >= $2;",
                &[&path, &from],
            )
            .await?;
        Ok(())
    }

    /// Remove (prune) document + chunks (CASCADE) + graph edges + claims (explicit; claim has no FK).
    pub async fn delete_document(&self, path: &str) -> Result<()> {
        self.db
            .execute("DELETE FROM document WHERE source_path = $1;", &[&path])
            .await?;
        let doc_id = doc_node_id(path);
        self.db
            .execute("DELETE FROM edge WHERE src = $1 OR dst = $1;", &[&doc_id])
            .await?;
        // claim has NO FK to document (unlike chunk's ON DELETE CASCADE) so the document delete does not
        // cascade here — mirror the explicit edge delete above. Provenance is single-valued (source_path
        // is overwritten last-writer-wins on conflict and is not part of the PK), so every claim row
        // carrying this path is owned by THIS document → remove it (current + its own sealed history).
        // Caveat: if this doc owned the latest value of a (subject,predicate) while an OLDER row from
        // another doc stays sealed, that pair loses its current pointer (a MISSING claim, never an
        // orphaned/WRONG one) — inherent to single-valued provenance; the remedy is a re-seal pass.
        self.db
            .execute("DELETE FROM claim WHERE source_path = $1;", &[&path])
            .await?;
        Ok(())
    }

    // ── chunk (embedding) ─────────────────────────────────────────────────────

    pub async fn upsert_chunk(&self, d: &Doc) -> Result<()> {
        let vec = self.checked_vector(&d.embedding)?; // dim guard (shared with upsert_claim)
        let idx = i32::try_from(d.chunk_idx).unwrap_or(i32::MAX);
        self.db
            .execute(
                "INSERT INTO chunk (id, source_path, content, embedding, origin, project, kind, chunk_idx)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                 ON CONFLICT (id) DO UPDATE SET
                     content = EXCLUDED.content, embedding = EXCLUDED.embedding, origin = EXCLUDED.origin,
                     project = EXCLUDED.project, kind = EXCLUDED.kind, chunk_idx = EXCLUDED.chunk_idx;",
                &[
                    &d.id, &d.front.source_path, &d.content, &vec,
                    &d.front.origin, &d.front.project, &d.front.kind, &idx,
                ],
            )
            .await
            .context("upsert chunk")?;
        Ok(())
    }

    // ── retrieval ─────────────────────────────────────────────────────────────

    pub async fn vector_search(&self, vec: &[f32], k: usize) -> Result<Vec<Hit>> {
        let qvec = Vector::from(vec.to_vec());
        let k_i64 = i64::try_from(k).unwrap_or(i64::MAX);
        let rows = self
            .db
            .query(
                "SELECT id, content, origin, project, source_path, (embedding <=> $1)::float4 AS dist
                 FROM chunk ORDER BY embedding <=> $1 LIMIT $2;",
                &[&qvec, &k_i64],
            )
            .await?;
        Ok(rows.iter().map(row_to_hit).collect())
    }

    pub async fn text_search(&self, query: &str, k: usize) -> Result<Vec<Hit>> {
        let k_i64 = i64::try_from(k).unwrap_or(i64::MAX);
        let rows = self
            .db
            .query(
                "SELECT id, content, origin, project, source_path,
                        ts_rank(tsv, plainto_tsquery('simple', $1))::float4 AS dist
                 FROM chunk WHERE tsv @@ plainto_tsquery('simple', $1)
                 ORDER BY dist DESC LIMIT $2;",
                &[&query, &k_i64],
            )
            .await?;
        Ok(rows.iter().map(row_to_hit).collect())
    }

    /// Vector search with optional project and recency filters.
    /// `since_hours` restricts to chunks whose parent document was updated within the window.
    pub async fn vector_search_filtered(
        &self,
        vec: &[f32],
        k: usize,
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Result<Vec<Hit>> {
        let qvec = Vector::from(vec.to_vec());
        let k_i64 = i64::try_from(k).unwrap_or(i64::MAX);
        let rows = self
            .db
            .query(
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
            .await?;
        Ok(rows.iter().map(row_to_hit).collect())
    }

    /// Find the single nearest document by mean chunk distance. Used at the remember write gate
    /// to skip near-duplicate session notes. Returns `(source_path, distance)` if within `max_dist`.
    pub async fn nearest_document(
        &self,
        vec: &[f32],
        max_dist: f64,
    ) -> Result<Option<(String, f64)>> {
        let qvec = Vector::from(vec.to_vec());
        let rows = self
            .db
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

    /// Full-text search with optional project and recency filters.
    pub async fn text_search_filtered(
        &self,
        query: &str,
        k: usize,
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Result<Vec<Hit>> {
        let k_i64 = i64::try_from(k).unwrap_or(i64::MAX);
        let rows = self
            .db
            .query(
                "SELECT c.id, c.content, c.origin, c.project, c.source_path,
                        ts_rank(c.tsv, plainto_tsquery('simple', $1))::float4 AS dist
                 FROM chunk c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE c.tsv @@ plainto_tsquery('simple', $1)
                   AND ($3::text IS NULL OR c.project = $3)
                   AND ($4::int IS NULL OR d.updated_at >= now() - make_interval(hours => $4))
                 ORDER BY dist DESC
                 LIMIT $2;",
                &[&query, &k_i64, &project, &since_hours],
            )
            .await?;
        Ok(rows.iter().map(row_to_hit).collect())
    }

    pub async fn count(&self) -> Result<usize> {
        pg_count(&self.db, "SELECT count(*) FROM chunk;").await
    }

    pub async fn all_meta(&self) -> Result<Vec<Meta>> {
        let rows = self
            .db
            .query("SELECT origin, project, kind, source_path FROM chunk;", &[])
            .await?;
        Ok(rows
            .into_iter()
            .map(|r| Meta {
                origin: r.get(0),
                project: r.get(1),
                kind: r.get(2),
                source_path: r.get(3),
            })
            .collect())
    }

    // ── query log (memory usage analytics) ────────────────────────────────────

    #[allow(clippy::needless_borrow)] // tokio-postgres params need &&str to coerce to &dyn ToSql.
    pub async fn log_query(
        &self,
        endpoint: &str,
        query: &str,
        hit_paths: &[String],
        sources: &[String],
        answer_snippet: &str,
        latency_ms: Option<i32>,
    ) -> Result<()> {
        // Scrub secrets a user may have pasted into a question/answer BEFORE they persist — the same
        // leak-boundary the remember path applies. query_log is exported by backup-db and served by
        // /query-log, so storing raw Q&A would leak tokens outside the redaction guarantee.
        let (query, answer_snippet) = match crate::redact::build_secret_re() {
            Ok(re) => (
                crate::redact::redact(re, query),
                crate::redact::redact(re, answer_snippet),
            ),
            Err(_) => (query.to_owned(), answer_snippet.to_owned()),
        };
        self.db
            .execute(
                "INSERT INTO query_log (endpoint, query, hit_paths, sources, answer_snippet, latency_ms)
                 VALUES ($1, $2, $3, $4, $5, $6);",
                &[
                    &endpoint,
                    &query,
                    &hit_paths,
                    &sources,
                    &answer_snippet,
                    &latency_ms,
                ],
            )
            .await
            .context("log query")?;
        Ok(())
    }

    pub async fn recent_queries(&self, limit: i64) -> Result<Vec<QueryLogRow>> {
        let rows = self
            .db
            .query(
                "SELECT id, created_at, endpoint, query, hit_paths, sources, answer_snippet, latency_ms
                 FROM query_log ORDER BY created_at DESC LIMIT $1;",
                &[&limit],
            )
            .await
            .context("recent queries")?;
        Ok(rows
            .into_iter()
            .map(|r| QueryLogRow {
                id: r.get(0),
                created_at: r.get(1),
                endpoint: r.get(2),
                query: r.get(3),
                hit_paths: r.get(4),
                sources: r.get(5),
                answer_snippet: r.get(6),
                latency_ms: r.get(7),
            })
            .collect())
    }

    /// Store adapter/workflow events in the local DB using an OpenTelemetry-shaped log row.
    ///
    /// The local event DB is the queryable primary sink for ops dashboards, HTTP, and MCP.
    /// Host adapters may keep an NDJSON fallback spool when the engine is unavailable.
    pub async fn log_event(&self, event: &Value) -> Result<()> {
        let event = redact_json_value(event);
        let otel = event.get("otel").and_then(Value::as_object);
        let component = text_field(&event, "component").unwrap_or_default();
        let event_name = otel
            .and_then(|o| o.get("event_name"))
            .and_then(Value::as_str)
            .map(str::to_owned)
            .or_else(|| text_field(&event, "event"))
            .unwrap_or_default();
        let status = text_field(&event, "status").unwrap_or_default();
        let severity_text = otel
            .and_then(|o| o.get("severity_text"))
            .and_then(Value::as_str)
            .map_or_else(|| severity_text_for_status(&status), str::to_owned);
        let severity_number = otel
            .and_then(|o| o.get("severity_number"))
            .and_then(Value::as_i64)
            .and_then(|n| i32::try_from(n).ok())
            .unwrap_or_else(|| severity_number_for_text(&severity_text));
        let time_unix_nano = otel
            .and_then(|o| o.get("time_unix_nano"))
            .and_then(Value::as_i64);
        let trace_id = otel
            .and_then(|o| o.get("trace_id"))
            .and_then(Value::as_str)
            .map(str::to_owned);
        let span_id = otel
            .and_then(|o| o.get("span_id"))
            .and_then(Value::as_str)
            .map(str::to_owned);
        let body = otel
            .and_then(|o| o.get("body"))
            .cloned()
            .unwrap_or_else(|| json!({"event.name": event_name, "status": status}));
        let attributes = otel
            .and_then(|o| o.get("attributes"))
            .cloned()
            .unwrap_or_else(|| event.clone());
        let resource = otel
            .and_then(|o| o.get("resource"))
            .cloned()
            .unwrap_or_else(|| {
                json!({"attributes": {
                    "service.name": component,
                    "service.namespace": "oh-my-boring"
                }})
            });
        let service_name = resource
            .get("attributes")
            .and_then(|a| a.get("service.name"))
            .and_then(Value::as_str)
            .map_or_else(|| component.clone(), str::to_owned);

        let run_id = text_field(&event, "run_id");
        let session_id = text_field(&event, "session_id");
        let workflow = text_field(&event, "workflow");
        let workflow_node = text_field(&event, "workflow_node");
        let workflow_outcome = text_field(&event, "workflow_outcome");

        self.db
            .execute(
                "INSERT INTO event_log (
                     time_unix_nano, severity_text, severity_number, service_name,
                     component, event_name, status, trace_id, span_id, run_id, session_id,
                     workflow, workflow_node, workflow_outcome, body, attributes, resource
                 )
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17);",
                &[
                    &time_unix_nano,
                    &severity_text,
                    &severity_number,
                    &service_name,
                    &component,
                    &event_name,
                    &status,
                    &trace_id,
                    &span_id,
                    &run_id,
                    &session_id,
                    &workflow,
                    &workflow_node,
                    &workflow_outcome,
                    &PgJson(&body),
                    &PgJson(&attributes),
                    &PgJson(&resource),
                ],
            )
            .await
            .context("log event")?;
        Ok(())
    }

    pub async fn recent_events(&self, filter: EventLogFilter<'_>) -> Result<Vec<EventLogRow>> {
        let rows = self
            .db
            .query(
                "SELECT id, observed_at, time_unix_nano, severity_text, severity_number,
                        service_name, component, event_name, status, trace_id, span_id,
                        run_id, session_id, workflow, workflow_node, workflow_outcome,
                        body, attributes, resource
                 FROM event_log
                 WHERE ($2::text IS NULL OR component = $2)
                   AND ($3::text IS NULL OR event_name = $3)
                   AND ($4::text IS NULL OR status = $4)
                   AND ($5::text IS NULL OR run_id = $5)
                   AND ($6::text IS NULL OR workflow = $6)
                   AND ($7::int IS NULL OR observed_at >= now() - make_interval(hours => $7))
                 ORDER BY observed_at DESC, id DESC
                 LIMIT $1;",
                &[
                    &filter.limit,
                    &filter.component,
                    &filter.event_name,
                    &filter.status,
                    &filter.run_id,
                    &filter.workflow,
                    &filter.since_hours,
                ],
            )
            .await
            .context("recent events")?;
        Ok(rows
            .into_iter()
            .map(|r| EventLogRow {
                id: r.get(0),
                observed_at: r.get(1),
                time_unix_nano: r.get(2),
                severity_text: r.get(3),
                severity_number: r.get(4),
                service_name: r.get(5),
                component: r.get(6),
                event_name: r.get(7),
                status: r.get(8),
                trace_id: r.get(9),
                span_id: r.get(10),
                run_id: r.get(11),
                session_id: r.get(12),
                workflow: r.get(13),
                workflow_node: r.get(14),
                workflow_outcome: r.get(15),
                body: r.get::<_, PgJson<Value>>(16).0,
                attributes: r.get::<_, PgJson<Value>>(17).0,
                resource: r.get::<_, PgJson<Value>>(18).0,
            })
            .collect())
    }

    /// Maintenance compact: VACUUM ANALYZE + REINDEX TABLE CONCURRENTLY + old query_log
    /// pruning + orphan semantic-node GC. Returns a report of what happened.
    ///
    /// VACUUM and REINDEX CONCURRENTLY cannot run inside a transaction block. We send each
    /// statement through its own `batch_execute` call so the simple-query protocol keeps them
    /// in autocommit mode rather than wrapping multiple statements in an implicit transaction.
    pub async fn compact(&self) -> Result<CompactSummary> {
        let mut report = CompactReport::default();
        let started = std::time::Instant::now();

        let t0 = std::time::Instant::now();
        for table in [
            "document",
            "chunk",
            "node",
            "edge",
            "claim",
            "query_log",
            "event_log",
        ] {
            self.db
                .batch_execute(&format!("VACUUM ANALYZE {table};"))
                .await
                .with_context(|| format!("vacuum analyze {table}"))?;
        }
        report.vacuum_ms = t0.elapsed().as_millis();

        let t0 = std::time::Instant::now();
        for table in [
            "document",
            "chunk",
            "node",
            "edge",
            "claim",
            "query_log",
            "event_log",
        ] {
            self.db
                .batch_execute(&format!("REINDEX TABLE CONCURRENTLY {table};"))
                .await
                .with_context(|| format!("reindex table {table}"))?;
        }
        report.reindex_ms = t0.elapsed().as_millis();

        let pruned = self
            .db
            .execute(
                "DELETE FROM query_log WHERE created_at < now() - interval '90 days';",
                &[],
            )
            .await
            .context("prune query_log")?;
        report.prune_query_log = usize::try_from(pruned).unwrap_or(0);

        let gc = self.gc_orphans().await.context("gc orphans")?;
        report.gc_tool = gc.tool;
        report.gc_concept = gc.concept;

        Ok(CompactSummary {
            report,
            total_ms: started.elapsed().as_millis(),
        })
    }

    // ── graph helpers (node/edge upsert) ───────────────────────────────────────

    async fn upsert_node(
        &self,
        id: &str,
        kind: &str,
        label: &str,
        outcome: Option<&str>,
    ) -> Result<()> {
        self.db
            .execute(
                "INSERT INTO node (id, kind, label, outcome) VALUES ($1, $2, $3, $4)
                 ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label, outcome = EXCLUDED.outcome;",
                &[&id, &kind, &label, &outcome],
            )
            .await
            .context("upsert node")?;
        Ok(())
    }

    async fn upsert_edge(&self, src: &str, dst: &str, kind: &str) -> Result<()> {
        self.db
            .execute(
                "INSERT INTO edge (src, dst, kind) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING;",
                &[&src, &dst, &kind],
            )
            .await
            .context("upsert edge")?;
        Ok(())
    }

    // ── semantic nodes ──────────────────────────────────────────────────────────

    pub async fn upsert_tool(&self, slug: &str, text: &str) -> Result<()> {
        self.upsert_node(&format!("tool:{slug}"), "tool", text, None)
            .await
    }
    pub async fn upsert_concept(&self, slug: &str, text: &str) -> Result<()> {
        self.upsert_node(&format!("concept:{slug}"), "concept", text, None)
            .await
    }

    /// Remove this document's semantic edges (uses/about) — makes the deterministic graph rebuild idempotent.
    pub async fn clear_semantic_edges(&self, doc_path: &str) -> Result<()> {
        let doc_id = doc_node_id(doc_path);
        let kinds: Vec<&str> = SEMANTIC_EDGE_KINDS.to_vec();
        self.db
            .execute(
                "DELETE FROM edge WHERE src = $1 AND kind = ANY($2);",
                &[&doc_id, &kinds],
            )
            .await?;
        Ok(())
    }

    // ── semantic edges (doc → entity) ───────────────────────────────────────────

    pub async fn relate_doc_tool(&self, doc_path: &str, slug: &str) -> Result<()> {
        self.upsert_edge(&doc_node_id(doc_path), &format!("tool:{slug}"), "uses")
            .await
    }
    pub async fn relate_doc_concept(&self, doc_path: &str, slug: &str) -> Result<()> {
        self.upsert_edge(&doc_node_id(doc_path), &format!("concept:{slug}"), "about")
            .await
    }

    // ── graph retrieval ───────────────────────────────────────────────────────

    /// Structural neighbors (project/topic) — 1-hop from the chunk's document. Returns labels.
    pub async fn graph_neighbors(&self, chunk_id: &str) -> Result<Vec<String>> {
        let doc_id = doc_node_id(chunk_id);
        let rows = self
            .db
            .query(
                "SELECT n.label FROM edge e JOIN node n ON n.id = e.dst
                 WHERE e.src = $1 AND e.kind IN ('in_project', 'tagged', 'claims');",
                &[&doc_id],
            )
            .await?;
        Ok(rows.into_iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// Semantic neighbors (problem/solution/tool/concept/attempt) — 1-hop from the document. Returns labels.
    pub async fn semantic_neighbors(&self, chunk_id: &str) -> Result<Vec<String>> {
        let doc_id = doc_node_id(chunk_id);
        let rows = self
            .db
            .query(
                "SELECT n.label FROM edge e JOIN node n ON n.id = e.dst
                 WHERE e.src = $1 AND e.kind = ANY($2);",
                &[&doc_id, &SEMANTIC_EDGE_KINDS.to_vec()],
            )
            .await?;
        Ok(rows.into_iter().map(|r| r.get::<_, String>(0)).collect())
    }

    // ── stats / GC ──────────────────────────────────────────────────────────────

    pub async fn graph_stats(&self) -> Result<GraphStats> {
        Ok(GraphStats {
            documents: pg_count(&self.db, "SELECT count(*) FROM document;").await?,
            chunks: pg_count(&self.db, "SELECT count(*) FROM chunk;").await?,
            projects: count_node_kind(&self.db, "project").await?,
            topics: count_node_kind(&self.db, "topic").await?,
            claims: count_node_kind(&self.db, "claim").await?,
            decisions: count_node_kind(&self.db, "decision").await?,
            risks: count_node_kind(&self.db, "risk").await?,
            edges: pg_count(&self.db, "SELECT count(*) FROM edge;").await?,
        })
    }

    pub async fn semantic_stats(&self) -> Result<SemanticStats> {
        Ok(SemanticStats {
            tools: count_node_kind(&self.db, "tool").await?,
            concepts: count_node_kind(&self.db, "concept").await?,
            uses: count_edge_kind(&self.db, "uses").await?,
            about: count_edge_kind(&self.db, "about").await?,
        })
    }

    /// Remove orphan semantic nodes — entity nodes not referenced by any edge.
    pub async fn gc_orphans(&self) -> Result<GcStats> {
        let mut gc = GcStats::default();
        for kind in ["tool", "concept"] {
            let n = self
                .db
                .execute(
                    "DELETE FROM node WHERE kind = $1
                       AND id NOT IN (SELECT src FROM edge UNION SELECT dst FROM edge);",
                    &[&kind],
                )
                .await?;
            let c = usize::try_from(n).unwrap_or(0);
            match kind {
                "tool" => gc.tool = c,
                _ => gc.concept = c,
            }
        }
        Ok(gc)
    }
}

fn text_field(event: &Value, key: &str) -> Option<String> {
    event
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
}

fn severity_text_for_status(status: &str) -> String {
    match status.to_ascii_lowercase().as_str() {
        "failed" | "failure" | "error" => "ERROR".to_owned(),
        "warn" | "warning" => "WARN".to_owned(),
        "debug" => "DEBUG".to_owned(),
        "trace" => "TRACE".to_owned(),
        _ => "INFO".to_owned(),
    }
}

fn severity_number_for_text(severity_text: &str) -> i32 {
    match severity_text.to_ascii_uppercase().as_str() {
        "TRACE" => 1,
        "DEBUG" => 5,
        "WARN" => 13,
        "ERROR" => 17,
        "FATAL" => 21,
        _ => 9,
    }
}

fn redact_json_value(value: &Value) -> Value {
    let Ok(re) = crate::redact::build_secret_re() else {
        return value.clone();
    };
    match value {
        Value::String(s) => Value::String(crate::redact::redact(re, s)),
        Value::Array(items) => Value::Array(items.iter().map(redact_json_value).collect()),
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(k, v)| (k.clone(), redact_json_value(v)))
                .collect(),
        ),
        _ => value.clone(),
    }
}

fn row_to_hit(r: &tokio_postgres::Row) -> Hit {
    Hit {
        id: r.get(0),
        content: r.get(1),
        origin: r.get(2),
        project: r.get(3),
        source_path: r.get(4),
        dist: r.get(5),
    }
}
