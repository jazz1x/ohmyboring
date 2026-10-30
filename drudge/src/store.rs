//! Store — pgvector (document/chunk/embedding/FTS) + graph (node/edge tables).
//!
//! Cross-reference: ENFORCEMENT.md §A (error ADTs) · design decision D5 (claim temporal authority).
//!
//! ## Layers (engine-agnostic graph model)
//! - **pgvector** (`document`, `chunk`): vector (HNSW) + FTS (tsvector) + frontmatter columns.
//! - **graph** (`node`, `edge`): semantic ontology. node = entity, edge = typed relation.
//!   - node id convention: `doc:<source_path>` · `project:<name>` · `topic:<tag>`
//!     · `tool|concept:<slug>` · `claim:<subject>:<predicate>` · `session:<id>` · `person:<name>`
//!     · `<kind>:<subject>:<predicate>` (non-fact claims — decision·risk·next… — joined to their
//!     claim node by `is_a`).
//!   - the `document` table is the SSOT for documents; the graph references them by `doc:<path>` id (no duplicate storage).
//!
//! ## Advantage over AGE
//! Every value goes through `tokio-postgres` parameter binding ($1,$2…) → eliminates the cypher string-escaping footgun.
use std::collections::{HashMap, HashSet};
use std::time::{Duration, SystemTime};

use anyhow::{Context, Result};
use deadpool_postgres::{
    GenericClient, Manager, ManagerConfig, Pool, RecyclingMethod, Runtime, Timeouts, Transaction,
};
use pgvector::Vector;
use serde_json::{Value, json};
use tokio_postgres::error::SqlState;
use tokio_postgres::types::Json as PgJson;
use tokio_postgres::{Client, Config as PgConfig, NoTls};

use crate::frontmatter::{Claim, FrontMatter};

mod code_copy_surreal;
mod log_copy_surreal;
mod vector_pg;
mod vector_route;
mod vector_surreal;

pub use code_copy_surreal::{CodeCheck, CodeCopy};
pub use log_copy_surreal::{LogCheck, LogCopy};
pub use vector_route::VectorRoute;
pub use vector_surreal::{
    ClaimRowDiff, FillTable, KeySet, ProjectTable, ProjectTableError, RegisterAsk, SURREAL_BEHIND,
    Scope, SurrealClient, Swap,
};

/// Ingest input (one chunk).
#[derive(Debug)]
pub struct Doc {
    pub id: String, // "{source_path}#{idx}"
    pub content: String,
    pub embedding: Vec<f32>,
    pub front: FrontMatter,
    pub chunk_idx: usize,
}

/// What `Hit::dist` actually measures. Vector cosine distance and full-text rank live on
/// different, non-comparable scales (direction of "better" even flips) — this stops a caller
/// from doing arithmetic (e.g. a relevance threshold) across the two without knowing which it has.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DistKind {
    /// pgvector cosine distance to the query embedding (0 = identical, 2 = opposite). Lower is better.
    VectorCosine,
    /// Postgres `ts_rank` full-text score. Higher is better; unbounded, not comparable across queries.
    TextRank,
}

impl DistKind {
    /// String used in the DB and the one place in `main.rs` that still prints a label.
    /// Kept identical to the serde serialization so API, DB, and Python matcher cannot drift.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::VectorCosine => "vector_cosine",
            Self::TextRank => "text_rank",
        }
    }
}

/// One hit as stored in `query_log`. The Option makes "absent" unrepresentable as a value:
/// `None` → SQL NULL in the array, `Some` → the distance/kind.
#[derive(Debug)]
pub struct LoggedHit {
    pub path: String,
    pub dist: Option<f32>,
    pub dist_kind: Option<DistKind>,
}

impl LoggedHit {
    pub fn with_distance(path: impl Into<String>, dist: f32, dist_kind: DistKind) -> Self {
        Self {
            path: path.into(),
            dist: Some(dist),
            dist_kind: Some(dist_kind),
        }
    }

    pub fn without_distance(path: impl Into<String>) -> Self {
        Self {
            path: path.into(),
            dist: None,
            dist_kind: None,
        }
    }

    pub fn without_distances(paths: Vec<String>) -> Vec<Self> {
        paths.into_iter().map(Self::without_distance).collect()
    }
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
    pub dist_kind: DistKind,
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

/// Result of a consumption write: how many edges were written and how many paths had no
/// `document` row and were skipped.
#[derive(Debug, Default)]
pub struct ConsumptionReport {
    pub used: usize,
    pub contested: usize,
    /// `supersedes` edges written (`doc:<newer>` → `doc:<older>` pairs).
    pub supersedes: usize,
    pub unknown: usize,
}

/// Result of a handover write: how many `handed` edges were written and how many paths had no
/// `document` row and were skipped.
#[derive(Debug, Default)]
pub struct HandoverReport {
    pub handed: usize,
    pub unknown: usize,
}

/// What a `supersedes` edge closes in the older note's claims.
#[derive(Debug, Clone, Copy)]
enum SealRule {
    /// Every current claim of the older note (verdict writes, `compact`).
    WholeNote,
    /// Only the slots the newer note re-states (`remember`).
    RestatedSlots,
}

/// Result of a supersedes write: how many `supersedes` edges were written and how many pairs were
/// skipped — a pair naming the same path twice, or one naming a path with no `document` row.
#[derive(Debug, Default)]
pub struct SupersedesReport {
    /// `supersedes` edges written (`doc:<newer>` → `doc:<older>` pairs).
    pub supersedes: usize,
    pub unknown: usize,
    /// The skipped paths themselves, in input order — the door echoes them to the caller, which
    /// otherwise learns "1 unknown" without learning which of its names missed.
    pub unknown_paths: Vec<String>,
    /// The `(subject, predicate)` slots of the older notes this write closed, read after the
    /// seal, in input order, each once.
    pub retired: Vec<(String, String)>,
}

/// Splits `supersedes` pairs into edge-worthy ones and the skipped paths — a pair that names the
/// same path twice replaces nothing, so it is skipped and counted as unknown, never written.
fn split_supersedes(pairs: &[[String; 2]]) -> (Vec<&[String; 2]>, Vec<&str>) {
    let mut valid = Vec::with_capacity(pairs.len());
    let mut skipped = Vec::new();
    for pair in pairs {
        if pair[0] == pair[1] {
            skipped.push(pair[1].as_str());
        } else {
            valid.push(pair);
        }
    }
    (valid, skipped)
}

/// Consumption edge counts pointing at one document.
#[derive(Debug, Default, Clone, Copy)]
pub struct ConsumptionCounts {
    pub used: i64,
    pub contested: i64,
}

/// Whose `contested` edges a count takes. Ranking takes on an owner note only the owner's own
/// objection; the displayed count takes every judge.
#[derive(Debug, Clone, Copy)]
enum ContestedBy {
    AnyJudge,
    OwnerOnOwnerNotes,
}

/// Who wrote a note, as far as ordering inside a returned set is concerned.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Authorship {
    Owner { updated_at: SystemTime },
    Other,
}

/// What orders one note inside a returned set, besides its score.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RankFacts {
    pub superseded: bool,
    pub authorship: Authorship,
}

/// Graph size summary (for audit).
#[derive(Debug, Default)]
pub struct GraphStats {
    pub documents: usize,
    pub chunks: usize,
    pub projects: usize,
    pub topics: usize,
    pub claims: usize,
    pub claims_with_doc: usize,
    pub decisions: usize,
    pub risks: usize,
    pub edges: usize,
}

/// GC deletion stats.
#[derive(Debug, Default)]
pub struct GcStats {
    pub tool: usize,
    pub concept: usize,
    pub claim_nodes: usize,
    pub claim_edges: usize,
}

impl GcStats {
    pub const fn total(&self) -> usize {
        self.tool + self.concept + self.claim_nodes
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
    pub hit_dists: Vec<Option<f32>>,
    pub hit_dist_kinds: Vec<Option<String>>,
    pub sources: Vec<String>,
    pub answer_snippet: String,
    pub latency_ms: Option<i32>,
}

/// One correctness label on one hit of one logged query. `judge` distinguishes who said it.
#[derive(Debug)]
pub struct RecallLabelRow {
    pub query_log_id: i32,
    pub hit_index: i32,
    pub judge: String,
    pub verdict: String,
    pub model: String,
    pub note: String,
}

/// Label counts for one judge, plus the agreement with the other judge where both labelled the
/// same hit. `agreed`/`compared` is the honesty check on an LLM judge: reported, never assumed.
#[derive(Debug, Default)]
pub struct RecallLabelStats {
    pub judge: String,
    pub relevant: i64,
    pub irrelevant: i64,
    pub unsure: i64,
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
#[derive(Debug)]
pub struct EventLogFilter<'a> {
    pub limit: i64,
    pub component: Option<&'a str>,
    pub event_name: Option<&'a str>,
    pub status: Option<&'a str>,
    pub run_id: Option<&'a str>,
    pub workflow: Option<&'a str>,
    pub since_hours: Option<i32>,
}

const COMPACT_LOCK_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(60);

/// Result of a maintenance compact pass.
#[derive(Debug, Default)]
pub struct CompactReport {
    pub vacuum_ms: u128,
    pub reindex_ms: u128,
    /// Half-built indexes a failed `REINDEX CONCURRENTLY` left behind. Reported rather than
    /// swept silently: a number that climbs every run says reindexing keeps failing, which is
    /// the fault worth seeing — the leftovers themselves are only its residue.
    pub dropped_invalid_indexes: usize,
    /// Tables whose `REINDEX CONCURRENTLY` could not get its lock within the lock timeout.
    pub skipped_lock_timeout: Vec<String>,
    pub prune_query_log: usize,
    pub gc_tool: usize,
    pub gc_concept: usize,
    pub gc_claim_nodes: usize,
    pub gc_claim_edges: usize,
    /// Current claims retired because a `supersedes` edge names their note — counted, not
    /// sealed silently, so a nightly number that is not zero says edges are fixing rows the
    /// write path missed.
    pub sealed_superseded_claims: usize,
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
    pool: Pool,
    /// Embedding dimension (= `boring.json` `embed_dim`; bge-m3 = 1024). Enforced at every embedding
    /// upsert via `checked_vector` and mirrored by the `vector(dim)` DDL columns created in `open`.
    dim: usize,
    target: crate::pg_detail::ConnectTarget,
    /// Which backend ranks vectors (`vector.backend`); pgvector unless `with_vector_route` says otherwise.
    vector_route: VectorRoute,
}

/// Hand-written: `pool` holds the parsed DSN (user and password), which must never reach a log.
impl std::fmt::Debug for Store {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Store")
            .field("dim", &self.dim)
            .finish_non_exhaustive()
    }
}

/// Semantic edge kinds (doc→entity) — the SSOT shared by clear/stats.
/// Kernel A: graph is tool/concept only (`uses`/`about`). Narrative (problem/attempt/solution) lives in
/// the note body markdown, not as graph nodes — so those edge kinds are gone.
const SEMANTIC_EDGE_KINDS: [&str; 3] = ["uses", "about", "claims"];

const OWNER_SPEAKER_NODE: &str = "person:owner";

const STALE_CLAIM_MIRROR_NODES: &str = "(kind = 'claim' AND id NOT IN (SELECT 'claim:' || subject || ':' || predicate FROM claim)) \
     OR (kind IN (SELECT DISTINCT kind FROM claim WHERE kind <> 'fact') \
         AND id NOT IN (SELECT kind || ':' || subject || ':' || predicate FROM claim WHERE kind <> 'fact'))";

/// Retirement, defined once: a note is retired when a `supersedes` edge points at it
/// whose source was allowed to retire it — an owner-written note yields to an
/// owner-written newer note only, so an edge from a non-owner leaves the note live. A
/// retired note's rows keep every seal the edge stamped and can never be a slot's
/// latest. Every seal/unseal/race tests this one fragment: a note the seal refused to
/// close must not read as retired here, or the hand-over reopens its slot's older row
/// while the note itself stays current — two current rows in one slot. `c` is the claim
/// alias at every use site; `doc:` matches what `doc_node_id` writes.
const RETIRED_NOTE: &str = "EXISTS (SELECT 1 FROM edge e \
     JOIN document n ON n.source_path = substr(e.src, 5) \
     WHERE e.kind = 'supersedes' AND e.dst = 'doc:' || c.source_path \
       AND (n.author = 'owner' OR NOT EXISTS (SELECT 1 FROM document o \
                                             WHERE o.source_path = c.source_path \
                                               AND o.author = 'owner')))";

/// `ingest::canon` in SQL. The class lists Rust's White_Space code points because `\s` follows the
/// server locale. Measured equal to `ingest::canon` on 12 inputs; `lower()` differs from
/// `to_lowercase()` only on special casing (`İ`, final sigma).
const CANON_SEP: &str = r"[\t-\r \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000_-]+";

fn canon_sql(col: &str) -> String {
    format!("btrim(regexp_replace(lower({col}), '{CANON_SEP}', '-', 'g'), '-')")
}

/// A claim row is canon-spelled when its stored subject and predicate already equal their canon.
/// Inside one canon slot such a row outranks every old-spelling row, whatever their `valid_from`.
fn is_canon_sql(alias: &str) -> String {
    let subject = canon_sql(&format!("{alias}.subject"));
    let predicate = canon_sql(&format!("{alias}.predicate"));
    format!("({alias}.subject = {subject} AND {alias}.predicate = {predicate})")
}

/// Not user memory, though both live in the vault and stay searchable elsewhere.
///
/// - `eval-*.md`: internal fixtures, which must remain searchable while `make eval` runs.
/// - `daily-brief-*.md` / `weekly-brief-*.md`: **the briefing's own past output.** Recency feeds
///   the briefing, so leaving these in makes it read yesterday's summary and restate it as
///   today's work. Measured 2026-09-03: `omb-helper` appears in 18 daily-brief notes and **zero**
///   real session notes, having entered on 08-01 and been repeated every morning since — an item
///   with no evidence behind it that the reader cannot act on and cannot get rid of. The vault
///   holds 29 such notes against 1,522 real ones, so the loop is small in volume and total in
///   effect: whatever enters it never leaves.
///
/// They stay in the corpus and stay retrievable by `recall`/`search` — this excludes them only
/// from the recency surface that generates the next briefing.
/// How far back a "stalled" report reaches. Beyond this an item is not stalled but abandoned, and
/// the honest answer is that nobody is working on it — not a line in tomorrow's briefing.
///
/// Chosen against the measured distribution rather than picked: `next` claims over 30 days number
/// 202 of 548, and the four oldest had been pinned to the top slots since 2026-06-30. Thirty days
/// keeps 295 candidates, which is more than the twelve slots can ever show.
const STALE_HORIZON_DAYS: i64 = 30;

const NOT_USER_MEMORY_RE: &str = r"(^|/)(eval-|daily-brief-|weekly-brief-)[^/]*\.md$";

/// Shortest `value` that still reads as a statement rather than a label.
///
/// A register row is read as `subject predicate: value`, and the value is the only part that
/// carries what happened. Measured on the live corpus (2026-09-20): of 766 current risk claims
/// 472 hold a value under this length, and what those look like in the session-start card is
/// `ohmyboring incident: code-implementation-mismatch` — a tag, not a risk. Rows below the line
/// are not hidden; they sort after rows above it, so a register with nothing better still answers.
pub const INFORMATIVE_VALUE_CHARS: i32 = 25;

/// Predicates that merely restate their own `kind`, by kind.
///
/// `risk` + `incident` is 647 of 766 current risk claims; `decision` + `decision` is 620 of 2,399.
/// In those rows the predicate slot carries no information at all, so the reader gets
/// `subject incident: <value>` where the useful half is whatever the value happens to hold. Same
/// treatment as a short value: demoted, never dropped.
const TAUTOLOGICAL_PREDICATES: &str = "^(incident|decision|status|state|상태|결정|\
                                       next[-_ ]?(step|action)|action|다음 작업)$";

/// Cosine-distance ceiling for a recurrence pair. Measured against the live corpus
/// (2026-09-21): at 0.2 the pairs are the same incident restated ("Orca CLI cannot connect
/// to the running app" ↔ "orca orchestration cannot connect because Orca is not running",
/// 0.121); from 0.25 different incidents mix in ("missing sealed CSV" ↔ "Vigil
/// approval/schema…", 0.246).
pub const RECURRENCE_MAX_DISTANCE: f32 = 0.2;

/// Minimum gap between the newer and the older claim of a recurrence pair. A same-day
/// near-duplicate is the same write restated, not "the same mistake again".
pub const RECURRENCE_MIN_DAYS_APART: i64 = 3;

/// I/O-boundary timeout for pool wait/create/recycle. Prevents infinite hangs on DB loss;
/// drudge/CLAUDE.md treats this as a graceful boundary, distinct from defensive `{timeout:200}` bounds.
const POOL_TIMEOUT_SECONDS: u64 = 5;
const POOL_TIMEOUTS: Timeouts = Timeouts {
    wait: Some(Duration::from_secs(POOL_TIMEOUT_SECONDS)),
    create: Some(Duration::from_secs(POOL_TIMEOUT_SECONDS)),
    recycle: Some(Duration::from_secs(POOL_TIMEOUT_SECONDS)),
};

/// A note is its number; the name's width is notation (4-digit before the renumbering, 11-digit
/// after). The path as given first, then the other widths of the same number.
pub fn wiki_path_spellings(path: &str) -> Vec<String> {
    let (dir, name) = path.rsplit_once('/').unwrap_or(("", path));
    let number = name
        .strip_prefix("wiki-")
        .and_then(|s| s.strip_suffix(".md"))
        .filter(|digits| !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()))
        .and_then(|digits| digits.parse::<u64>().ok());
    let mut out = vec![path.to_owned()];
    if let Some(n) = number {
        for spelled in [format!("wiki-{n:04}.md"), format!("wiki-{n:011}.md")] {
            let candidate = if dir.is_empty() {
                spelled
            } else {
                format!("{dir}/{spelled}")
            };
            if !out.contains(&candidate) {
                out.push(candidate);
            }
        }
    }
    out
}

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

fn anchor_check_row_from(row: &tokio_postgres::Row) -> AnchorCheckRow {
    AnchorCheckRow {
        subject: row.get(0),
        predicate: row.get(1),
        valid_from: row.get(2),
        anchor: row.get(3),
        anchor_hash: row.get(4),
        anchor_symbol: row.get(5),
    }
}
#[derive(Debug)]
pub struct AnchoredClaim {
    pub claim: Claim,
    pub anchor: Option<String>,
    pub era: String,
    pub stale_at: Option<SystemTime>,
    pub stale_reason: Option<String>,
}

#[derive(Debug, Default)]
pub struct ClaimEraCounts {
    pub anchored: usize,
    pub unanchored: usize,
    pub pre_anchor: usize,
    /// Current claims that carry a label rather than a statement — the same two tests the
    /// registers and the session-start card demote on (`INFORMATIVE_VALUE_CHARS`,
    /// `TAUTOLOGICAL_PREDICATES`), counted so the weakness is visible instead of merely sorted
    /// to the bottom.
    ///
    /// Ordering hides the problem from whoever reads a register; this number keeps it in the
    /// sync log and in `/audit`. Measured 2026-09-20 before any of it was wired: 647 of 769 risk
    /// claims restate their kind in the predicate, and 472 hold a value under 25 characters.
    pub label_only: usize,
}

#[derive(Debug)]
pub struct AnchorCheckRow {
    pub subject: String,
    pub predicate: String,
    pub valid_from: SystemTime,
    pub anchor: String,
    pub anchor_hash: String,
    pub anchor_symbol: Option<String>,
}

#[derive(Debug, Clone)]
pub struct AnchorCheckWatermark {
    pub valid_from: SystemTime,
    pub subject: String,
    pub predicate: String,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct RegisterRow {
    pub node_id: String,
    pub subject: String,
    pub predicate: String,
    pub value: String,
    pub kind: String,
    pub confidence: String,
    #[serde(serialize_with = "serialize_valid_from")]
    pub valid_from: SystemTime,
    pub project: String,
}

fn serialize_valid_from<S: serde::Serializer>(
    valid_from: &SystemTime,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    let dt = chrono::DateTime::<chrono::Utc>::from(*valid_from);
    serializer.serialize_str(&dt.to_rfc3339())
}

impl RegisterRow {
    pub fn new(
        subject: String,
        predicate: String,
        value: String,
        kind: String,
        confidence: String,
        valid_from: SystemTime,
        project: String,
    ) -> Self {
        let claim = Claim {
            subject: subject.clone(),
            predicate: predicate.clone(),
            value: value.clone(),
            kind,
            confidence,
            said_by: None,
        };
        Self {
            node_id: format!("claim:{subject}:{predicate}"),
            subject,
            predicate,
            value,
            kind: claim.kind().to_owned(),
            confidence: claim.confidence().to_owned(),
            valid_from,
            project,
        }
    }
}

#[derive(Debug)]
pub struct RegisterRows {
    pub rows: Vec<RegisterRow>,
    pub total_matching: i64,
}

/// One end of a recurrence pair, as the register cites it.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ClaimRef {
    pub source_path: String,
    pub subject: String,
    pub predicate: String,
    pub value: String,
    pub kind: String,
    #[serde(serialize_with = "serialize_valid_from")]
    pub valid_from: SystemTime,
}

/// One row of the recurrence register: a recent risk/blocked claim whose value restates an
/// older claim from a different note — "the same mistake again". `older` holds every
/// earlier claim within `RECURRENCE_MAX_DISTANCE`, oldest first. `label_only` mirrors the
/// register demotion rule: the newer claim's predicate merely restates its kind
/// (`TAUTOLOGICAL_PREDICATES`), so the row pairs labels, not statements.
#[derive(Debug, Clone, serde::Serialize)]
pub struct Recurrence {
    pub newer: ClaimRef,
    pub older: Vec<ClaimRef>,
    pub distance: f32,
    pub days_apart: i64,
    pub label_only: bool,
}

fn register_row(r: &tokio_postgres::Row) -> RegisterRow {
    RegisterRow::new(
        r.get(0),
        r.get(1),
        r.get(2),
        r.get(3),
        r.get(4),
        r.get(5),
        r.get(6),
    )
}

/// What a schema step leaves behind. `ALTER TABLE … IF NOT EXISTS` and `CREATE INDEX … IF NOT
/// EXISTS` still take a table lock (AccessExclusive / Share) before noticing the object exists,
/// which deadlocked `Store::open` against a concurrent writer — so the catalog is read first and
/// the DDL is only sent when this effect is missing.
enum Effect {
    Column {
        table: &'static str,
        column: &'static str,
    },
    ColumnDefault {
        table: &'static str,
        column: &'static str,
    },
    Index(&'static str),
}

struct SchemaStep {
    effect: Effect,
    ddl: String,
}

/// The one place the judge ranking lives: owner 3 > `agent:*` 2 > inferred 1 > NULL or any other
/// value 0. An edge's judge only moves up this order; `col` is the column expression to rank.
fn judge_rank_sql(col: &str) -> String {
    format!(
        "(CASE WHEN {col} = 'owner' THEN 3 WHEN {col} LIKE 'agent:%' THEN 2 \
         WHEN {col} = 'inferred' THEN 1 ELSE 0 END)"
    )
}

fn step(effect: Effect, ddl: &str) -> SchemaStep {
    SchemaStep {
        effect,
        ddl: ddl.to_owned(),
    }
}

fn column(table: &'static str, column: &'static str, ddl: &str) -> SchemaStep {
    step(Effect::Column { table, column }, ddl)
}

fn index(name: &'static str, ddl: &str) -> SchemaStep {
    step(Effect::Index(name), ddl)
}

/// Every ALTER / CREATE INDEX of the schema path, in dependency order.
#[allow(clippy::too_many_lines)] // a flat list of declarations; splitting only obscures the migration order.
fn schema_steps(slot_subject: &str, slot_predicate: &str) -> Vec<SchemaStep> {
    vec![
        index(
            "chunk_hnsw",
            "CREATE INDEX IF NOT EXISTS chunk_hnsw ON chunk USING hnsw (embedding vector_cosine_ops)",
        ),
        index(
            "chunk_gin",
            "CREATE INDEX IF NOT EXISTS chunk_gin ON chunk USING gin (tsv)",
        ),
        index(
            "edge_src",
            "CREATE INDEX IF NOT EXISTS edge_src ON edge(src)",
        ),
        index(
            "edge_dst",
            "CREATE INDEX IF NOT EXISTS edge_dst ON edge(dst)",
        ),
        // Who judged this edge: 'owner', 'inferred', 'agent:<name>'; NULL = written before the
        // column existed. Stored verbatim; only `judge_rank_sql` ranks it, when a later write
        // names the same edge (a higher rank replaces, an equal or lower one writes nothing).
        column(
            "edge",
            "judge",
            "ALTER TABLE edge ADD COLUMN IF NOT EXISTS judge text",
        ),
        // NULL = written before the column existed (never back-filled — the time is unknown). The
        // default is its own step: a DEFAULT on ADD COLUMN would back-fill every row with now().
        column(
            "edge",
            "first_seen_at",
            "ALTER TABLE edge ADD COLUMN IF NOT EXISTS first_seen_at timestamptz",
        ),
        step(
            Effect::ColumnDefault {
                table: "edge",
                column: "first_seen_at",
            },
            "ALTER TABLE edge ALTER COLUMN first_seen_at SET DEFAULT now()",
        ),
        column(
            "document",
            "extracted_sha",
            "ALTER TABLE document ADD COLUMN IF NOT EXISTS extracted_sha text NOT NULL DEFAULT ''",
        ),
        column(
            "document",
            "updated_at",
            "ALTER TABLE document ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()",
        ),
        column(
            "document",
            "author",
            "ALTER TABLE document ADD COLUMN IF NOT EXISTS author text NOT NULL DEFAULT 'unknown'",
        ),
        index(
            "document_updated",
            "CREATE INDEX IF NOT EXISTS document_updated ON document(updated_at DESC)",
        ),
        column(
            "claim",
            "kind",
            "ALTER TABLE claim ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'fact'",
        ),
        column(
            "claim",
            "confidence",
            "ALTER TABLE claim ADD COLUMN IF NOT EXISTS confidence text NOT NULL DEFAULT 'certain'",
        ),
        // D2 step 1 (PRD §8): `anchor` = `project:path[:span]`; `era` is the D3 legacy marker
        // (existing rows keep 'pre-anchor').
        column(
            "claim",
            "anchor",
            "ALTER TABLE claim ADD COLUMN IF NOT EXISTS anchor text",
        ),
        column(
            "claim",
            "stale_at",
            "ALTER TABLE claim ADD COLUMN IF NOT EXISTS stale_at timestamptz",
        ),
        column(
            "claim",
            "stale_reason",
            "ALTER TABLE claim ADD COLUMN IF NOT EXISTS stale_reason text",
        ),
        column(
            "claim",
            "era",
            "ALTER TABLE claim ADD COLUMN IF NOT EXISTS era text NOT NULL DEFAULT 'pre-anchor'",
        ),
        // D2 step 2: `anchor_hash` = sha256 of the covering symbol's body; `anchor_symbol` = the
        // key the sync-time re-pin looks the hash up by after a move.
        column(
            "claim",
            "anchor_hash",
            "ALTER TABLE claim ADD COLUMN IF NOT EXISTS anchor_hash text",
        ),
        column(
            "claim",
            "anchor_symbol",
            "ALTER TABLE claim ADD COLUMN IF NOT EXISTS anchor_symbol text",
        ),
        column(
            "claim",
            "said_by",
            "ALTER TABLE claim ADD COLUMN IF NOT EXISTS said_by text",
        ),
        index(
            "claim_anchor",
            "CREATE INDEX IF NOT EXISTS claim_anchor ON claim(anchor) WHERE anchor IS NOT NULL",
        ),
        index(
            "claim_current",
            "CREATE INDEX IF NOT EXISTS claim_current ON claim(subject, predicate) WHERE superseded_at IS NULL",
        ),
        index(
            "claim_kind",
            "CREATE INDEX IF NOT EXISTS claim_kind ON claim(kind) WHERE superseded_at IS NULL",
        ),
        // The planner matches the expression text, so it comes from the same `canon_sql` the queries use.
        index(
            "claim_canon_slot",
            &format!(
                "CREATE INDEX IF NOT EXISTS claim_canon_slot ON claim(({slot_subject}), ({slot_predicate}))"
            ),
        ),
        index(
            "claim_hnsw",
            "CREATE INDEX IF NOT EXISTS claim_hnsw ON claim USING hnsw (embedding vector_cosine_ops) WHERE superseded_at IS NULL",
        ),
        column(
            "query_log",
            "hit_dists",
            "ALTER TABLE query_log ADD COLUMN IF NOT EXISTS hit_dists real[] NOT NULL DEFAULT '{}'",
        ),
        column(
            "query_log",
            "hit_dist_kinds",
            "ALTER TABLE query_log ADD COLUMN IF NOT EXISTS hit_dist_kinds text[] NOT NULL DEFAULT '{}'",
        ),
        index(
            "query_log_created",
            "CREATE INDEX IF NOT EXISTS query_log_created ON query_log(created_at DESC)",
        ),
        index(
            "recall_label_created",
            "CREATE INDEX IF NOT EXISTS recall_label_created ON recall_label(created_at DESC)",
        ),
        index(
            "event_log_observed",
            "CREATE INDEX IF NOT EXISTS event_log_observed ON event_log(observed_at DESC, id DESC)",
        ),
        index(
            "event_log_component",
            "CREATE INDEX IF NOT EXISTS event_log_component ON event_log(component, observed_at DESC)",
        ),
        index(
            "event_log_event",
            "CREATE INDEX IF NOT EXISTS event_log_event ON event_log(event_name, observed_at DESC)",
        ),
        index(
            "event_log_status",
            "CREATE INDEX IF NOT EXISTS event_log_status ON event_log(status, observed_at DESC)",
        ),
        index(
            "event_log_run_id",
            "CREATE INDEX IF NOT EXISTS event_log_run_id ON event_log(run_id, observed_at DESC)",
        ),
        // Rows from when the watermark was a bare timestamp read the '' pair — it sorts before
        // every real key, so the sweep re-checks that timestamp's group once, which is correct.
        column(
            "sync_state",
            "value_subject",
            "ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS value_subject text NOT NULL DEFAULT ''",
        ),
        column(
            "sync_state",
            "value_predicate",
            "ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS value_predicate text NOT NULL DEFAULT ''",
        ),
    ]
}

/// Decision: is this step's effect already in the catalog? Reads only; takes no table lock.
async fn effect_present(db: &Client, effect: &Effect) -> Result<bool> {
    let row = match effect {
        Effect::Column { table, column } => {
            db.query_one(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns
                                 WHERE table_schema = current_schema()
                                   AND table_name = $1 AND column_name = $2)",
                &[table, column],
            )
            .await
        }
        Effect::ColumnDefault { table, column } => {
            db.query_one(
                "SELECT EXISTS (SELECT 1 FROM pg_attribute
                                 WHERE attrelid = to_regclass($1) AND attname = $2 AND atthasdef)",
                &[table, column],
            )
            .await
        }
        Effect::Index(name) => {
            db.query_one("SELECT to_regclass($1) IS NOT NULL", &[name])
                .await
        }
    }
    .context("read catalog for schema step")?;
    Ok(row.get(0))
}

/// Effect: send only the DDL whose effect is missing.
async fn apply_missing_ddl(db: &Client, steps: &[SchemaStep]) -> Result<()> {
    for s in steps {
        if !effect_present(db, &s.effect).await? {
            db.batch_execute(&s.ddl)
                .await
                .with_context(|| format!("schema step: {}", s.ddl))?;
        }
    }
    Ok(())
}

impl Store {
    // ── connect + ensure schema ───────────────────────────────────────────────

    /// PostgreSQL connect + pgvector + node/edge graph schema initialization.
    /// `dim` = the embedding dimension (`boring.json` `embed_dim`) → the `vector(dim)` columns.
    pub async fn open(dsn: &str, dim: usize) -> Result<Self> {
        Self::open_with_pool_size(dsn, dim, deadpool_postgres::PoolConfig::default().max_size).await
    }

    /// `open` with an explicit pool `max_size` — the one code path `open` delegates to.
    #[allow(clippy::too_many_lines)] // schema DDL grows with features; splitting only obscures the one migration block.
    pub async fn open_with_pool_size(dsn: &str, dim: usize, pool_size: usize) -> Result<Self> {
        let pg_config: PgConfig = dsn.parse().context("parse postgres connection string")?;
        let target = crate::pg_detail::ConnectTarget::from_config(&pg_config);
        let manager = Manager::from_config(
            pg_config,
            NoTls,
            ManagerConfig {
                recycling_method: RecyclingMethod::Verified,
            },
        );
        let pool = Pool::builder(manager)
            .runtime(Runtime::Tokio1)
            .max_size(pool_size)
            .timeouts(POOL_TIMEOUTS)
            .build()
            .context("build postgres connection pool")?;

        // connect retry (IO boundary, graceful) — when postgres is started separately via profile
        // drudge waits up to ~10s even if it comes up first (depends_on removed → absorbs startup race).
        {
            let mut tries = 0_u32;
            loop {
                match pool.get().await {
                    Ok(obj) => match obj.query_one("SELECT 1", &[]).await {
                        Ok(_) => break,
                        Err(e) if tries < 9 => {
                            tries += 1;
                            eprintln!("[store] postgres connect retry {tries}/10 … ({e})");
                            tokio::time::sleep(Duration::from_secs(1)).await;
                        }
                        Err(e) => {
                            return Err(anyhow::Error::new(e).context(
                                "postgres connect (retries exhausted) — is Postgres up? \
                                 vector mode needs `BORING_VECTOR=on make up` (starts pgvector); \
                                 or run wiki-first with BORING_VECTOR unset",
                            ));
                        }
                    },
                    Err(e) if tries < 9 => {
                        tries += 1;
                        eprintln!("[store] postgres connect retry {tries}/10 … ({e})");
                        tokio::time::sleep(Duration::from_secs(1)).await;
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
        }

        let obj = pool
            .get()
            .await
            .context("get connection for schema initialization")?;

        let slot_subject = canon_sql("subject");
        let slot_predicate = canon_sql("predicate");

        // DDL parameterized by the embedding dim (`embed_dim`). `vector({dim})` is the only interpolation;
        // dim is a parsed integer (no injection surface). `'{{}}'` escapes the literal empty-array default.
        obj
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
                     anchor        text,
                     stale_at      timestamptz,
                     stale_reason  text,
                     era           text NOT NULL DEFAULT 'pre-anchor',
                     anchor_hash   text,
                     anchor_symbol text,
                     PRIMARY KEY (subject, predicate, valid_from)
                 );
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
                 -- recall_label: the correctness label query_log cannot carry. A distance says how
                 --   far a hit was, never whether it was worth injecting, so precision was
                 --   unmeasurable and every relevance threshold was argued from distances alone.
                 --   One row per (query_log row, hit position, judge): `llm` and `human` verdicts
                 --   are stored SIDE BY SIDE, never overwriting each other, so their agreement rate
                 --   is itself a measurement — an LLM judge nobody has checked
                 --   is a biased instrument until that rate says otherwise.
                 CREATE TABLE IF NOT EXISTS recall_label (
                     query_log_id int NOT NULL REFERENCES query_log(id) ON DELETE CASCADE,
                     hit_index    int NOT NULL,
                     judge        text NOT NULL,
                     verdict      text NOT NULL,
                     model        text NOT NULL DEFAULT '',
                     note         text NOT NULL DEFAULT '',
                     created_at   timestamptz NOT NULL DEFAULT now(),
                     PRIMARY KEY (query_log_id, hit_index, judge)
                 );
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
                 -- Round-robin watermark for the D2 step-2 anchor staleness sweep: each sync takes
                 -- the next batch of hashed claims after this position (wrapping to the corpus
                 -- head), so a full pass is bounded per sync instead of growing with the corpus.
                 -- The watermark is the WHOLE (valid_from, subject, predicate) sort key, not just
                 -- the timestamp: claims distilled from one note share a valid_from, and a
                 -- timestamp-only resume made a batch boundary inside such a group skip the rest
                 -- of the group for a whole cycle.
                 CREATE TABLE IF NOT EXISTS sync_state (
                     key   text PRIMARY KEY,
                     value timestamptz NOT NULL
                 );
                 "
            ))
            .await
            .context("pgvector + graph schema")?;

        apply_missing_ddl(&obj, &schema_steps(&slot_subject, &slot_predicate)).await?;

        // Data migrations: they only touch rows, and need the columns added above.
        obj.batch_execute(&format!(
            "                 -- `about` was carrying two different relations under one name: a claim
                 -- belonging to a project (8,485 rows) and a document mentioning a concept
                 -- (5,148). Only the second is semantic, and `semantic_stats.about` -- which sits
                 -- beside `tools`, `concepts` and `uses` -- was counting both, so that number was
                 -- overstated by the whole of the first. Idempotent: the predicate matches
                 -- nothing once the rows carry their own name.
                 UPDATE edge SET kind = 'claim_of_project'
                  WHERE kind = 'about' AND src LIKE 'claim:%' AND dst LIKE 'project:%';
                 -- A claim that there is no next step is not a next step. The distiller has
                 -- been writing `next-step: none` / `남은작업: 없음` as `kind: next` since the
                 -- beginning, so the stalled register reported work that was already finished
                 -- every morning: 70 of 645 current next/blocked claims (2026-09-09) denied
                 -- their own existence, and `k6/k7`, `pr-232`, `fds-17084` and
                 -- `omcr-completed-form` were holding slots in the twelve it renders.
                 --
                 -- `frontmatter::Claim::kind` stops new ones; this relabels what is stored.
                 --
                 -- Both predicates match on the row's own `value`. Keying on
                 -- `(subject, predicate)` alone would sweep in every other row on the same axis:
                 -- an axis whose history once held `none` usually holds real work now, and 31
                 -- live next-steps -- `bi-slack-analytics-bot next-step: main merge and live
                 -- benchmark` among them -- would have been relabelled as denials. The node
                 -- mirror keys on the CURRENT row only, because that is what the mirror
                 -- reflects: a sealed denial under a live next-step must leave the typed node
                 -- standing.
                 --
                 -- A non-fact claim writes two node rows (`claim:s:p` carrying the kind in
                 -- `outcome`, plus a typed `next:s:p` joined by `is_a`), so the mirror loses the
                 -- typed node and its edge -- what `upsert_claim_node` would never have created
                 -- for a fact.
                 --
                 -- Idempotent: each predicate matches nothing on a second run. The vocabulary is
                 -- duplicated from `frontmatter::WORK_DENIALS` because SQL cannot call into Rust;
                 -- `test_work_denials_match_the_migration` fails if the two lists drift apart.
                 CREATE TEMP TABLE denied_now ON COMMIT DROP AS
                   SELECT subject, predicate, kind FROM claim
                    WHERE superseded_at IS NULL
                      AND kind IN ('next', 'blocked')
                      AND lower(btrim(btrim(value), '.。')) = ANY(ARRAY[
                          'none', 'nothing', 'n/a', 'na', 'not applicable', 'no', '-', '--',
                          '없음', '없다', '없습니다', '없어요', '해당 없음', '해당없음',
                          '남은 작업 없음', '남은 작업이 없음', 'なし', '無し', '該当なし']);
                 DELETE FROM edge e USING denied_now d
                  WHERE e.kind = 'is_a'
                    AND e.src = 'claim:' || d.subject || ':' || d.predicate
                    AND e.dst = d.kind  || ':' || d.subject || ':' || d.predicate;
                 DELETE FROM node n USING denied_now d
                  WHERE n.id = d.kind || ':' || d.subject || ':' || d.predicate;
                 UPDATE node n SET outcome = 'fact'
                   FROM denied_now d
                  WHERE n.id = 'claim:' || d.subject || ':' || d.predicate
                    AND n.outcome IN ('next', 'blocked');
                 UPDATE claim SET kind = 'fact'
                  WHERE kind IN ('next', 'blocked')
                    AND lower(btrim(btrim(value), '.。')) = ANY(ARRAY[
                        'none', 'nothing', 'n/a', 'na', 'not applicable', 'no', '-', '--',
                        '없음', '없다', '없습니다', '없어요', '해당 없음', '해당없음',
                        '남은 작업 없음', '남은 작업이 없음', 'なし', '無し', '該当なし']);
                 CREATE TABLE IF NOT EXISTS node_gc_backup_20260917 AS
                   SELECT * FROM node WHERE {STALE_CLAIM_MIRROR_NODES};
                 CREATE TABLE IF NOT EXISTS edge_gc_backup_20260917 AS
                   SELECT * FROM edge
                    WHERE src IN (SELECT id FROM node WHERE {STALE_CLAIM_MIRROR_NODES})
                       OR dst IN (SELECT id FROM node WHERE {STALE_CLAIM_MIRROR_NODES});
                 "
            ))
            .await
            .context("claim/graph data migrations")?;
        Ok(Self {
            pool,
            dim,
            target,
            vector_route: VectorRoute::default(),
        })
    }

    /// Start the store on the given vector backend (`vector.backend`).
    #[must_use]
    pub fn with_vector_route(mut self, route: VectorRoute) -> Self {
        self.vector_route = route;
        self
    }

    /// The door wording of a failure that came out of this store (see `pg_detail`).
    pub fn door_detail(&self, e: &anyhow::Error) -> String {
        crate::pg_detail::door_detail(&self.target, e)
    }

    /// `prefix: detail` for a stage-tagged failure.
    pub fn door_staged(&self, e: &crate::pg_detail::Staged) -> String {
        e.door_text(&self.target)
    }

    /// Acquire a connection from the pool. Kept as a method (not direct field access) so every
    /// call site is a single point to instrument and so callers can reuse one object across a
    /// multi-statement logical unit (e.g. stats/compact) rather than holding several connections.
    async fn db(&self) -> Result<deadpool_postgres::Object> {
        self.pool
            .get()
            .await
            .context("get postgres connection from pool")
    }

    /// Probe that the database is reachable. Propagates the original error — callers decide whether
    /// to treat failure as degraded health or a hard error.
    pub async fn liveness_probe(&self) -> Result<()> {
        let db = self.db().await?;
        db.query_one("SELECT 1", &[])
            .await
            .context("store liveness probe (SELECT 1)")?;
        Ok(())
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
        Self::get_doc_sha_on(&self.db().await?, path).await
    }

    /// `get_doc_sha` on a caller-held connection or transaction handle.
    pub async fn get_doc_sha_on(c: &impl GenericClient, path: &str) -> Result<Option<String>> {
        let rows = c
            .query("SELECT sha FROM document WHERE source_path = $1;", &[&path])
            .await?;
        Ok(rows.first().map(|r| r.get::<_, String>(0)))
    }

    /// The spelling of `path` that has a `document` row (the path as given wins), or None when no
    /// width of that note exists. Verdict writes key on this, so a row that cites an old-width id
    /// still lands on the document instead of counting as unknown.
    pub async fn resolve_doc_path_on(c: &impl GenericClient, path: &str) -> Result<Option<String>> {
        for candidate in wiki_path_spellings(path) {
            if Self::get_doc_sha_on(c, &candidate).await?.is_some() {
                return Ok(Some(candidate));
            }
        }
        Ok(None)
    }

    pub async fn all_doc_paths(&self) -> Result<Vec<String>> {
        let rows = self
            .db()
            .await?
            .query("SELECT source_path FROM document;", &[])
            .await?;
        Ok(rows.iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// Document mtime — the `valid_from` of a claim (temporal sort key). Falls back to now() if absent (graceful).
    pub async fn doc_updated_at(&self, path: &str) -> Result<SystemTime> {
        Self::doc_updated_at_on(&self.db().await?, path).await
    }

    /// `doc_updated_at` on a caller-held connection or transaction handle.
    pub async fn doc_updated_at_on(c: &impl GenericClient, path: &str) -> Result<SystemTime> {
        let rows = c
            .query(
                "SELECT updated_at FROM document WHERE source_path = $1;",
                &[&path],
            )
            .await?;
        Ok(rows
            .first()
            .map_or_else(SystemTime::now, |r| r.get::<_, SystemTime>(0)))
    }

    /// Move only the stored change-detection sha (old whole-file sha → content hash); nothing else is touched.
    pub async fn set_doc_sha(&self, path: &str, sha: &str) -> Result<()> {
        Self::set_doc_sha_on(&self.db().await?, path, sha).await?;
        self.mirror_surreal(&Scope::notes([path.to_owned()])).await
    }

    /// `set_doc_sha` on a caller-held connection or transaction handle.
    pub async fn set_doc_sha_on(c: &impl GenericClient, path: &str, sha: &str) -> Result<()> {
        c.execute(
            "UPDATE document SET sha = $2 WHERE source_path = $1;",
            &[&path, &sha],
        )
        .await
        .context("set document sha")?;
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
                .db()
                .await?
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
                        &NOT_USER_MEMORY_RE,
                    ],
                )
                .await
                .context("recent docs with time window")?,
            None => self
                .db()
                .await?
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
                    &[&limit, &exclude_origins, &project, &NOT_USER_MEMORY_RE],
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
            .db()
            .await?
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

    /// GraphRAG retrieval: the older notes this one shares ground with — the thread it continues.
    ///
    /// Two notes share ground when they assert the same **claim** (`claim:<subject>:<predicate>`)
    /// or when they are **about** the same concept. The first is the stronger statement and is
    /// weighted accordingly: a shared concept says the two notes cover the same area, a shared
    /// claim says they answered the same question — and the newer one may supersede or contest the
    /// older, which is exactly the note a reader wants next.
    ///
    /// `related_doc_content` is the neighbour walk `ask` uses, and it counts every non-project
    /// node including tools, needing two shared. For a briefing that is the wrong shape twice
    /// over. Measured on one morning's twelve notes: the nodes they shared were `tool:rg` (9),
    /// `tool:python` (6), `tool:sed` (6), `tool:git` (5) -- grouping by those produces "things
    /// that ran rg". Excluding tools, the same twelve reached `concept:mutationtesting` in 71
    /// older notes and `concept:singlesourceoftruth` in 19, which is the thread a reader means
    /// when they ask what this continues.
    ///
    /// One shared concept is enough here, where the neighbour walk wants two: concepts are
    /// specific (`contractdrivendevelopment`, not `git`), and requiring a second is what kept
    /// this reachable only through tools.
    ///
    /// Older only. A briefing already holds today; what it never had was last week.
    pub async fn related_by_shared_ground(
        &self,
        source_path: &str,
        limit: i64,
        min_shared: i64,
    ) -> Result<Vec<RecentDoc>> {
        let doc_id = doc_node_id(source_path);
        let rows = self
            .db()
            .await?
            .query(
                "WITH self_concepts AS (
                     SELECT dst FROM edge WHERE src = $1 AND kind = 'about'
                       AND dst LIKE 'concept:%'
                 ),
                 -- The other way two notes are about the same thing: they assert the same claim.
                 -- A claim node is `claim:<subject>:<predicate>`, so a shared one means both notes
                 -- settled the same question — the second may supersede the first, or contest it,
                 -- and either way that is the note a reader wants next.
                 --
                 -- This edge was written 6,921 times and never walked. Measured 2026-09-20:
                 -- 11,123 document pairs share a claim node and 10,886 of them (98%) share no two
                 -- concepts, so concept-sharing alone could not see them.
                 self_claims AS (
                     SELECT dst FROM edge WHERE src = $1 AND kind = 'claims'
                 ),
                 -- Older-than is a predicate on the candidates, not a filter on the winners.
                 -- Ranking first and trimming after is how a selector returns nothing: the two
                 -- best-connected notes are usually this morning's own, so the filter emptied a
                 -- set it was meant to narrow. Measured: three heads produced one row that way
                 -- and eleven this way.
                 shares AS (
                     SELECT e.src AS doc_node, 2 AS weight
                     FROM edge e
                     JOIN self_claims sl ON e.dst = sl.dst
                     WHERE e.src <> $1 AND e.kind = 'claims'
                     UNION ALL
                     SELECT e.src AS doc_node, 1 AS weight
                     FROM edge e
                     JOIN self_concepts sc ON e.dst = sc.dst
                     WHERE e.src <> $1 AND e.kind = 'about'
                 ),
                 -- Older-than is a predicate on the candidates, not a filter on the winners.
                 -- Ranking first and trimming after is how a selector returns nothing: the two
                 -- best-connected notes are usually this morning's own, so the filter emptied a
                 -- set it was meant to narrow. Measured: three heads produced one row that way
                 -- and eleven this way.
                 --
                 -- A shared claim outweighs a shared concept because it is the stronger statement:
                 -- concepts say the two notes are about the same area, a claim says they answered
                 -- the same question.
                 ranked AS (
                     SELECT s.doc_node, sum(s.weight) AS shared
                     FROM shares s
                     JOIN document od ON ('doc:' || od.source_path) = s.doc_node
                     WHERE od.updated_at < (SELECT updated_at FROM document WHERE source_path = $3)
                     GROUP BY s.doc_node HAVING sum(s.weight) >= $4
                     ORDER BY shared DESC, s.doc_node ASC LIMIT $2
                 )
                 SELECT d.source_path, d.project, d.tags,
                        string_agg(c.content, E'\n' ORDER BY c.chunk_idx) AS content
                 FROM ranked r
                 JOIN document d ON ('doc:' || d.source_path) = r.doc_node
                 JOIN chunk c ON c.source_path = d.source_path
                 GROUP BY d.source_path, d.project, d.tags, r.shared
                 ORDER BY r.shared DESC;",
                &[&doc_id, &limit, &source_path, &min_shared],
            )
            .await
            .context("related by shared ground")?;
        Ok(rows
            .into_iter()
            .map(|r| RecentDoc {
                source_path: r.get(0),
                project: r.get(1),
                tags: r.get(2),
                content: r.get::<_, Option<String>>(3).unwrap_or_default(),
            })
            .collect())
    }

    pub async fn related_doc_content(
        &self,
        source_path: &str,
        limit: i64,
    ) -> Result<Vec<RecentDoc>> {
        let doc_id = doc_node_id(source_path);
        let rows = self
            .db()
            .await?
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
            .db()
            .await?
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

    /// True when the note's own latest row for `(subject, predicate)` already says exactly
    /// this.
    ///
    /// Re-ingesting a note re-asserts every claim in it, and `valid_from` is the note's mtime —
    /// so editing one line of a note wrote a fresh row, and a fresh 1024-dim embedding, for
    /// every unrelated claim it contains. Measured on this corpus: 36,421 of 55,498 claim rows
    /// (66%) are byte-identical re-writes of the row before them, and the `claim` table is
    /// 393 MB of a 744 MB database with 85% of it superseded.
    ///
    /// Comparing the whole tuple, not just the value: a different note asserting the same value
    /// is new provenance and must still be recorded, and so is a change of `kind` or
    /// `confidence`. Only an exact repeat of the note's own latest row is nothing — the note
    /// on disk is the source of truth, so a note edited back to an old value must read as
    /// changed, while re-syncing a value the slot took from another note must not write a row.
    #[allow(clippy::too_many_arguments)]
    pub async fn claim_is_unchanged(
        &self,
        subject: &str,
        predicate: &str,
        value: &str,
        source_path: &str,
        kind: &str,
        confidence: &str,
        anchor: Option<&str>,
    ) -> Result<bool> {
        Self::claim_is_unchanged_on(
            &self.db().await?,
            subject,
            predicate,
            value,
            source_path,
            kind,
            confidence,
            anchor,
        )
        .await
    }

    /// `claim_is_unchanged` on a caller-held connection or transaction handle.
    #[allow(clippy::too_many_arguments)]
    pub async fn claim_is_unchanged_on(
        c: &impl GenericClient,
        subject: &str,
        predicate: &str,
        value: &str,
        source_path: &str,
        kind: &str,
        confidence: &str,
        anchor: Option<&str>,
    ) -> Result<bool> {
        let rows = c
            .query(
                "SELECT 1 FROM claim
                 WHERE subject = $1 AND predicate = $2 AND source_path = $4
                   AND valid_from = (SELECT max(valid_from) FROM claim
                                      WHERE subject = $1 AND predicate = $2 AND source_path = $4)
                   AND value = $3 AND kind = $5 AND confidence = $6
                   AND anchor IS NOT DISTINCT FROM $7
                 LIMIT 1;",
                &[
                    &subject,
                    &predicate,
                    &value,
                    &source_path,
                    &kind,
                    &confidence,
                    &anchor,
                ],
            )
            .await
            .context("claim unchanged probe")?;
        Ok(!rows.is_empty())
    }

    /// Temporal claim upsert + supersede. A `fact` is a slot: for the same `(subject,predicate)`,
    /// old values are sealed via `superseded_at` across notes, and only the latest `valid_from`
    /// row is current (NULL). Every other kind is an item in a list — a project has many
    /// next-steps, risks, decisions — so the seal scope narrows to the note's own rows:
    /// `(subject, predicate, source_path)`. Idempotent (re-ingesting the same row is harmless).
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
        self.upsert_claim_with_anchor(
            subject,
            predicate,
            value,
            source_path,
            valid_from,
            embedding,
            kind,
            confidence,
            None,
            None,
            None,
        )
        .await
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn upsert_claim_with_anchor(
        &self,
        subject: &str,
        predicate: &str,
        value: &str,
        source_path: &str,
        valid_from: SystemTime,
        embedding: &[f32],
        kind: &str,
        confidence: &str,
        anchor: Option<&str>,
        anchor_hash: Option<&str>,
        anchor_symbol: Option<&str>,
    ) -> Result<()> {
        self.upsert_claim_with_anchor_on(
            &self.db().await?,
            subject,
            predicate,
            value,
            source_path,
            valid_from,
            embedding,
            kind,
            confidence,
            anchor,
            anchor_hash,
            anchor_symbol,
        )
        .await?;
        self.mirror_surreal(&Scope::notes([source_path.to_owned()]))
            .await
    }

    /// Lock every claim row of one canon slot, in the order `lock_claim_slots_of` takes them.
    async fn lock_claim_slot_on(
        c: &impl GenericClient,
        subject: &str,
        predicate: &str,
    ) -> Result<()> {
        let canon_subject = canon_sql("c.subject");
        let canon_predicate = canon_sql("c.predicate");
        let slot_subject = canon_sql("$1::text");
        let slot_predicate = canon_sql("$2::text");
        c.execute(
            &format!(
                "SELECT 1 FROM claim c
                 WHERE {canon_subject} = {slot_subject} AND {canon_predicate} = {slot_predicate}
                 ORDER BY ({canon_subject}) COLLATE \"C\", ({canon_predicate}) COLLATE \"C\",
                          c.valid_from, c.subject COLLATE \"C\", c.predicate COLLATE \"C\"
                 FOR UPDATE OF c;"
            ),
            &[&subject, &predicate],
        )
        .await
        .context("lock claim slot in slot order")?;
        Ok(())
    }

    /// `upsert_claim_with_anchor` on a caller-held connection or transaction handle.
    #[allow(clippy::too_many_arguments)]
    pub async fn upsert_claim_with_anchor_on(
        &self,
        c: &impl GenericClient,
        subject: &str,
        predicate: &str,
        value: &str,
        source_path: &str,
        valid_from: SystemTime,
        embedding: &[f32],
        kind: &str,
        confidence: &str,
        anchor: Option<&str>,
        anchor_hash: Option<&str>,
        anchor_symbol: Option<&str>,
    ) -> Result<()> {
        let vec = self.checked_vector(embedding)?; // dim guard (shared with upsert_chunk)
        // Take the slot's rows in the order `lock_claim_slots_of` takes them, before the upsert
        // and the seals below touch any of them: the seals lock rows in scan order, so a
        // supersede holding one row of the slot while waiting on another meets this request in
        // a cycle. `c` is the request's transaction (`ingest_in_own_tx`), so the locks last to
        // its commit.
        Self::lock_claim_slot_on(c, subject, predicate).await?;
        c.execute(
                "INSERT INTO claim (subject, predicate, value, source_path, valid_from, embedding, kind, confidence, anchor, anchor_hash, anchor_symbol, era)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::text, $10::text, $11::text,
                         CASE WHEN $9::text IS NULL THEN 'unanchored' ELSE 'anchored' END)
                 ON CONFLICT (subject, predicate, valid_from) DO UPDATE SET
                     value = EXCLUDED.value, source_path = EXCLUDED.source_path,
                     embedding = EXCLUDED.embedding, kind = EXCLUDED.kind, confidence = EXCLUDED.confidence,
                     anchor = EXCLUDED.anchor, anchor_hash = EXCLUDED.anchor_hash,
                     anchor_symbol = EXCLUDED.anchor_symbol, era = EXCLUDED.era;",
                &[
                    &subject,
                    &predicate,
                    &value,
                    &source_path,
                    &valid_from,
                    &vec,
                    &kind,
                    &confidence,
                    &anchor,
                    &anchor_hash,
                    &anchor_symbol,
                ],
            )
            .await
            .context("insert claim")?;
        // Seal everything below the slot's latest valid_from, leaving only the single
        // latest row current. A `fact` is a slot: seal across notes on (subject, predicate).
        // Every other kind is an item in a list, so the seal stays inside the note that wrote
        // the row: (subject, predicate, source_path). A note's own later row supersedes its
        // own earlier one; another note's items are never touched. The slot's latest is
        // computed over live notes only (NOT {RETIRED_NOTE}), so a retired note neither
        // keeps the slot nor stamps the rows below the next-latest live one. The unseal
        // below stays on the same scope as the seal — scoping them differently is how a
        // slot ends with no current row at all. An owner-written row is sealed only when
        // the latest row is owner-written too. The non-fact unseal also skips a retired
        // note's rows outright: nothing about them reopens. The fact unseal needs no such
        // guard — (subject, predicate, valid_from) is the primary key, so a row equalling
        // the live race's max is a race row itself and cannot be retired.
        if kind == "fact" {
            c.execute(
                    &format!(
                        "UPDATE claim c SET superseded_at = m.mx
                         FROM (SELECT c.subject, c.predicate, max(c.valid_from) AS mx FROM claim c
                               WHERE c.subject = $1 AND c.predicate = $2 AND NOT {RETIRED_NOTE}
                               GROUP BY c.subject, c.predicate) m
                         WHERE c.subject = m.subject AND c.predicate = m.predicate
                           AND c.valid_from < m.mx AND c.superseded_at IS DISTINCT FROM m.mx
                           AND (NOT EXISTS (SELECT 1 FROM document o
                                             WHERE o.source_path = c.source_path AND o.author = 'owner')
                                OR EXISTS (SELECT 1 FROM claim l JOIN document d ON d.source_path = l.source_path
                                            WHERE l.subject = m.subject AND l.predicate = m.predicate
                                              AND l.valid_from = m.mx AND d.author = 'owner'));"
                    ),
                    &[&subject, &predicate],
                )
                .await
                .context("seal old claims")?;
            c.execute(
                &format!(
                    "UPDATE claim c SET superseded_at = NULL
                         WHERE c.subject = $1 AND c.predicate = $2 AND c.superseded_at IS NOT NULL
                           AND c.valid_from = (SELECT max(c.valid_from) FROM claim c
                                               WHERE c.subject = $1 AND c.predicate = $2
                                                 AND NOT {RETIRED_NOTE});"
                ),
                &[&subject, &predicate],
            )
            .await
            .context("unseal latest claim")?;
        } else {
            c.execute(
                "UPDATE claim c SET superseded_at = m.mx
                     FROM (SELECT subject, predicate, source_path, max(valid_from) AS mx FROM claim
                           WHERE subject = $1 AND predicate = $2 AND source_path = $3
                           GROUP BY subject, predicate, source_path) m
                     WHERE c.subject = m.subject AND c.predicate = m.predicate
                       AND c.source_path = m.source_path
                       AND c.valid_from < m.mx AND c.superseded_at IS DISTINCT FROM m.mx;",
                &[&subject, &predicate, &source_path],
            )
            .await
            .context("seal old claims")?;
            c.execute(
                &format!(
                    "UPDATE claim c SET superseded_at = NULL
                         WHERE c.subject = $1 AND c.predicate = $2 AND c.source_path = $3
                           AND c.superseded_at IS NOT NULL
                           AND c.valid_from = (SELECT max(valid_from) FROM claim
                                             WHERE subject = $1 AND predicate = $2
                                               AND source_path = $3)
                           AND NOT {RETIRED_NOTE};"
                ),
                &[&subject, &predicate, &source_path],
            )
            .await
            .context("unseal latest claim")?;
        }
        Ok(())
    }

    /// Upsert graph nodes/edges for a claim: `doc —claims→ claim:{subject}:{predicate}` and,
    /// for non-fact claims, a typed node (`decision:|risk:...`) plus an `is_a` edge.
    /// Also links the claim node to `project:{project}` when a project is present, and writes
    /// the `said` edges when the claim names its speaker. Returns the number of edges written.
    /// `subject`/`predicate` must be the canonical key — the same strings the claim row was
    /// written under; passing the raw frontmatter spelling strands the node from its row.
    pub async fn upsert_claim_node(
        &self,
        path: &str,
        project: &str,
        subject: &str,
        predicate: &str,
        claim: &crate::frontmatter::Claim,
    ) -> Result<usize> {
        let written =
            Self::upsert_claim_node_on(&self.db().await?, path, project, subject, predicate, claim)
                .await?;
        self.mirror_surreal(&Scope::notes([path.to_owned()]))
            .await?;
        Ok(written)
    }

    /// `upsert_claim_node` on a caller-held connection or transaction handle.
    pub async fn upsert_claim_node_on(
        c: &impl GenericClient,
        path: &str,
        project: &str,
        subject: &str,
        predicate: &str,
        claim: &crate::frontmatter::Claim,
    ) -> Result<usize> {
        let claim_id = format!("claim:{subject}:{predicate}");
        let label = format!("{predicate}: {}", claim.value);
        let kind = claim.kind();
        let confidence = claim.confidence();

        // claim node
        Self::upsert_node_on(c, &claim_id, "claim", &label, Some(kind)).await?;

        // typed node for decisions/risks/etc.
        if kind != "fact" {
            let typed_id = format!("{kind}:{subject}:{predicate}");
            let typed_label = format!("{subject} — {}", claim.value);
            Self::upsert_node_on(c, &typed_id, kind, &typed_label, Some(confidence)).await?;
            Self::upsert_edge_on(c, &claim_id, &typed_id, "is_a", None).await?;
        }

        // doc -> claim edge
        let doc_id = doc_node_id(path);
        Self::upsert_edge_on(c, &doc_id, &claim_id, "claims", None).await?;

        // claim -> project edge
        if !project.is_empty() {
            let project_id = format!("project:{project}");
            Self::upsert_edge_on(c, &claim_id, &project_id, "claim_of_project", None).await?;
        }

        if let Some(said_by) = claim.said_by {
            Self::relate_said_on(c, said_by, path, subject, predicate).await?;
        }

        Ok(1 + usize::from(kind != "fact")
            + usize::from(!project.is_empty())
            + 2 * usize::from(claim.said_by.is_some()))
    }

    /// Top-k **current** claims (superseded_at IS NULL) for the session-start card and the
    /// briefing, ordered the way the registers are: a row that says something outranks one that
    /// only labels something, and newest-first decides inside each rank.
    ///
    /// The two paths have to agree. The card is the surface that showed four consecutive
    /// `ohmyboring incident: <fragment>` rows, and the ordering rule shipped one function away
    /// from it — in `recent_register_rows`, which the card never calls.
    pub async fn recent_claims(
        &self,
        k: i64,
        project: Option<&str>,
        kinds: Option<&[String]>,
        exclude_origins: &[String],
    ) -> Result<Vec<Claim>> {
        match &self.vector_route {
            VectorRoute::Pgvector => {
                self.recent_claims_pg(k, project, kinds, exclude_origins)
                    .await
            }
            VectorRoute::Surreal(client) => {
                let answer = client
                    .register_rows(&RegisterAsk {
                        k,
                        project,
                        kinds,
                        exclude_origins,
                    })
                    .await?;
                Ok(answer
                    .rows
                    .into_iter()
                    .map(|r| Claim {
                        subject: r.subject,
                        predicate: r.predicate,
                        value: r.value,
                        kind: r.kind,
                        confidence: r.confidence,
                        said_by: None,
                    })
                    .collect())
            }
        }
    }

    async fn recent_claims_pg(
        &self,
        k: i64,
        project: Option<&str>,
        kinds: Option<&[String]>,
        exclude_origins: &[String],
    ) -> Result<Vec<Claim>> {
        let rows = self
            .db()
            .await?
            .query(
                "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence FROM claim c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE c.superseded_at IS NULL
                   AND ($2::text IS NULL OR d.project = $2)
                   AND ($3::text[] IS NULL OR c.kind = ANY($3))
                   AND NOT (d.origin = ANY($4))
                   AND d.source_path !~ $5
                 ORDER BY (CASE WHEN length(c.value) < $6 THEN 1 ELSE 0 END)
                        + (CASE WHEN c.predicate ~* $7 THEN 1 ELSE 0 END),
                          c.valid_from DESC
                 LIMIT $1;",
                &[
                    &k,
                    &project,
                    &kinds,
                    &exclude_origins,
                    &NOT_USER_MEMORY_RE,
                    &INFORMATIVE_VALUE_CHARS,
                    &TAUTOLOGICAL_PREDICATES,
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
                said_by: None,
            })
            .collect())
    }

    pub const NORTH_STAR_PREDICATES: [&'static str; 2] = ["goal", "check"];

    pub fn north_star_subject(project: &str) -> String {
        format!("north-star:{project}")
    }

    /// Current `decision` claims stored as a repository's north star: subject
    /// `north-star:<project>`, predicate `goal` / `check`, newest `valid_from` per predicate.
    /// Fetch only — the card puts them first (`ask::with_north_star`).
    pub async fn north_star_claims(
        &self,
        project: &str,
        exclude_origins: &[String],
    ) -> Result<Vec<Claim>> {
        let subject = Self::north_star_subject(project);
        let predicates: Vec<String> = Self::NORTH_STAR_PREDICATES
            .iter()
            .map(|p| (*p).to_owned())
            .collect();
        let rows = self
            .db()
            .await?
            .query(
                "SELECT DISTINCT ON (c.predicate) c.subject, c.predicate, c.value, c.kind, c.confidence
                 FROM claim c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE c.superseded_at IS NULL
                   AND c.kind = 'decision'
                   AND c.subject = $1
                   AND c.predicate = ANY($2)
                   AND NOT (d.origin = ANY($3))
                   AND d.source_path !~ $4
                 ORDER BY c.predicate, c.valid_from DESC;",
                &[&subject, &predicates, &exclude_origins, &NOT_USER_MEMORY_RE],
            )
            .await
            .context("north star claims")?;
        Ok(rows
            .iter()
            .map(|r| Claim {
                subject: r.get(0),
                predicate: r.get(1),
                value: r.get(2),
                kind: r.get(3),
                confidence: r.get(4),
                said_by: None,
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
            .db()
            .await?
            .query(
                // Oldest-first with no lower bound pinned the same twelve claims to the same
                // slots every single day. Measured 2026-09-03: the top four had not moved since
                // 2026-06-30 / 07-01 / 07-03, and 497 of 548 current `next` claims are over a
                // week old, so the bucket never empties and never rotates.
                //
                // The floor is not a decay curve and does not treat age as falsehood — the claim
                // stays current and every other surface still returns it. It says only that a
                // *stalled* report is about work that stalled recently enough to still be the
                // same work. Past `STALE_HORIZON_DAYS` an item is not stalled, it is abandoned,
                // and a briefing that reports it every morning has stopped reporting anything.
                //
                // One row per source document, oldest first: a single note that emitted several
                // next-steps used to take several of the twelve slots. `wiki-0231` held two with
                // `fds-16220` and `fds 16220` — the same work under two spellings of one axis.
                "WITH ranked AS (
                   SELECT c.subject, c.predicate, c.value, c.kind, c.confidence, c.valid_from,
                          ROW_NUMBER() OVER (
                            PARTITION BY c.source_path ORDER BY c.valid_from ASC
                          ) AS per_doc
                     FROM claim c
                     JOIN document d ON d.source_path = c.source_path
                    WHERE c.superseded_at IS NULL
                      AND c.valid_from <  (NOW() - INTERVAL '1 day' * ($5::bigint))
                      AND c.valid_from >= (NOW() - INTERVAL '1 day' * ($7::bigint))
                      AND ($2::text IS NULL OR d.project = $2)
                      AND ($3::text[] IS NULL OR c.kind = ANY($3))
                      AND NOT (d.origin = ANY($4))
                      AND d.source_path !~ $6
                 )
                 SELECT subject, predicate, value, kind, confidence
                   FROM ranked
                  WHERE per_doc = 1
                  ORDER BY valid_from ASC
                  LIMIT $1;",
                &[
                    &k,
                    &project,
                    &kinds,
                    &exclude_origins,
                    &older_than_days,
                    &NOT_USER_MEMORY_RE,
                    &STALE_HORIZON_DAYS,
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
                said_by: None,
            })
            .collect())
    }

    /// Top-k current claims for a register answer, plus the full match count so the cut is stated, not silent.
    ///
    /// Rows that say something sort ahead of rows that only label something: a value shorter than
    /// `INFORMATIVE_VALUE_CHARS` or a predicate that restates its kind costs a row one rank, both
    /// cost two, and newest-first decides inside each rank. Nothing is filtered out — a register
    /// whose every row is a label still answers with them, in the order it always had.
    pub async fn recent_register_rows(
        &self,
        k: i64,
        project: Option<&str>,
        kinds: Option<&[String]>,
        exclude_origins: &[String],
    ) -> Result<RegisterRows> {
        match &self.vector_route {
            VectorRoute::Pgvector => {
                self.recent_register_rows_pg(k, project, kinds, exclude_origins)
                    .await
            }
            VectorRoute::Surreal(client) => {
                client
                    .register_rows(&RegisterAsk {
                        k,
                        project,
                        kinds,
                        exclude_origins,
                    })
                    .await
            }
        }
    }

    async fn recent_register_rows_pg(
        &self,
        k: i64,
        project: Option<&str>,
        kinds: Option<&[String]>,
        exclude_origins: &[String],
    ) -> Result<RegisterRows> {
        let rows = self
            .db()
            .await?
            .query(
                "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence, c.valid_from, d.project,
                        COUNT(*) OVER () AS total
                   FROM claim c
                   JOIN document d ON d.source_path = c.source_path
                  WHERE c.superseded_at IS NULL
                    AND ($2::text IS NULL OR d.project = $2)
                    AND ($3::text[] IS NULL OR c.kind = ANY($3))
                    AND NOT (d.origin = ANY($4))
                    AND d.source_path !~ $5
                  ORDER BY (CASE WHEN length(c.value) < $6 THEN 1 ELSE 0 END)
                         + (CASE WHEN c.predicate ~* $7 THEN 1 ELSE 0 END),
                           c.valid_from DESC
                  LIMIT $1;",
                &[
                    &k,
                    &project,
                    &kinds,
                    &exclude_origins,
                    &NOT_USER_MEMORY_RE,
                    &INFORMATIVE_VALUE_CHARS,
                    &TAUTOLOGICAL_PREDICATES,
                ],
            )
            .await
            .context("recent register rows")?;
        Ok(RegisterRows {
            rows: rows.iter().map(register_row).collect(),
            total_matching: rows.first().map_or(0, |r| r.get::<_, i64>(7)),
        })
    }

    /// The claims a set of documents currently declare, one `RegisterRows` per path — the
    /// register row shape, so a hit's claims and a register entry serialize identically.
    /// "Currently declare" is the `doc —claims→ claim` edge, not the `source_path` column: a
    /// rewritten note keeps its old rows alive, but only edge-reachable claims are handed over.
    /// Current rows only, every kind including `fact` (`/search`, wiki-5375; the registers keep
    /// their own fact-free SQL). Most solving kind first (`decision`, then `next`/`blocked`, then `risk`, then the rest),
    /// newest `valid_from` first within a kind, at most `per_doc` rows per document, with the
    /// full qualifying count in `total_matching` so a cut is stated, not silent.
    pub async fn declared_claims(
        &self,
        paths: &[String],
        per_doc: i64,
    ) -> Result<HashMap<String, RegisterRows>> {
        let doc_ids: Vec<String> = paths.iter().map(|p| doc_node_id(p)).collect();
        let rows = self
            .db()
            .await?
            .query(
                "WITH asked AS (
                     SELECT unnest($1::text[]) AS doc_id, unnest($2::text[]) AS path
                 ),
                 declared AS (
                     SELECT a.path AS doc_path,
                            c.subject, c.predicate, c.value, c.kind, c.confidence, c.valid_from,
                            dm.project,
                            ROW_NUMBER() OVER (
                                PARTITION BY e.src, e.dst
                                ORDER BY (c.source_path = a.path) DESC, c.valid_from DESC
                            ) AS pick
                     FROM edge e
                     JOIN asked a ON a.doc_id = e.src
                     JOIN claim c ON ('claim:' || c.subject || ':' || c.predicate) = e.dst
                     JOIN document dm ON dm.source_path = c.source_path
                     WHERE e.kind = 'claims'
                       AND c.superseded_at IS NULL
                 ),
                 ranked AS (
                     SELECT doc_path, subject, predicate, value, kind, confidence, valid_from, project,
                            CASE kind WHEN 'decision' THEN 0
                                      WHEN 'next' THEN 1
                                      WHEN 'blocked' THEN 1
                                      WHEN 'risk' THEN 2
                                      ELSE 3 END AS tier,
                            COUNT(*) OVER (PARTITION BY doc_path) AS total
                     FROM declared
                     WHERE pick = 1
                 ),
                 picked AS (
                     SELECT *, ROW_NUMBER() OVER (
                         PARTITION BY doc_path
                         ORDER BY tier ASC, valid_from DESC, subject ASC, predicate ASC
                     ) AS rn
                     FROM ranked
                 )
                 SELECT doc_path, subject, predicate, value, kind, confidence, valid_from, project, total
                 FROM picked
                 WHERE rn <= $3
                 ORDER BY doc_path, rn;",
                &[&doc_ids, &paths, &per_doc],
            )
            .await
            .context("declared claims")?;
        let mut map: HashMap<String, RegisterRows> = HashMap::new();
        for r in &rows {
            let path: String = r.get(0);
            let entry = map.entry(path).or_insert_with(|| RegisterRows {
                rows: Vec::new(),
                total_matching: r.get(8),
            });
            entry.rows.push(RegisterRow::new(
                r.get(1),
                r.get(2),
                r.get(3),
                r.get(4),
                r.get(5),
                r.get(6),
                r.get(7),
            ));
        }
        Ok(map)
    }

    /// Stalled claims for a register answer: same window as `stalled_claims`, newest-first,
    /// plus the full match count so the cut is stated, not silent.
    pub async fn stalled_register_rows(
        &self,
        k: i64,
        project: Option<&str>,
        kinds: Option<&[String]>,
        exclude_origins: &[String],
        older_than_days: i64,
    ) -> Result<RegisterRows> {
        let rows = self
            .db()
            .await?
            .query(
                "WITH ranked AS (
                   SELECT c.subject, c.predicate, c.value, c.kind, c.confidence, c.valid_from, d.project,
                          ROW_NUMBER() OVER (
                            PARTITION BY c.source_path ORDER BY c.valid_from ASC
                          ) AS per_doc
                     FROM claim c
                     JOIN document d ON d.source_path = c.source_path
                    WHERE c.superseded_at IS NULL
                      AND c.valid_from <  (NOW() - INTERVAL '1 day' * ($5::bigint))
                      AND c.valid_from >= (NOW() - INTERVAL '1 day' * ($7::bigint))
                      AND ($2::text IS NULL OR d.project = $2)
                      AND ($3::text[] IS NULL OR c.kind = ANY($3))
                      AND NOT (d.origin = ANY($4))
                      AND d.source_path !~ $6
                 )
                 SELECT subject, predicate, value, kind, confidence, valid_from, project,
                        COUNT(*) OVER () AS total
                   FROM ranked
                  WHERE per_doc = 1
                  ORDER BY valid_from ASC
                  LIMIT $1;",
                &[
                    &k,
                    &project,
                    &kinds,
                    &exclude_origins,
                    &older_than_days,
                    &NOT_USER_MEMORY_RE,
                    &STALE_HORIZON_DAYS,
                ],
            )
            .await
            .context("stalled register rows")?;
        Ok(RegisterRows {
            rows: rows.iter().map(register_row).collect(),
            total_matching: rows.first().map_or(0, |r| r.get::<_, i64>(7)),
        })
    }

    /// document upsert + project/topic nodes + in_project/tagged edge regeneration (idempotent).
    /// `updated_at` = source file mtime (the true recency signal) — the sort key for recency-first retrieval.
    pub async fn upsert_document(
        &self,
        front: &FrontMatter,
        sha: &str,
        updated_at: SystemTime,
    ) -> Result<()> {
        Self::upsert_document_on(&self.db().await?, front, sha, updated_at).await?;
        self.mirror_surreal(&Scope::notes([front.source_path.clone()]))
            .await
    }

    /// `upsert_document` on a caller-held connection or transaction handle.
    pub async fn upsert_document_on(
        c: &impl GenericClient,
        front: &FrontMatter,
        sha: &str,
        updated_at: SystemTime,
    ) -> Result<()> {
        let path = &front.source_path;
        let title_ref: Option<&str> = front.title.as_deref();
        let author = String::from(front.author.clone());
        c.execute(
                "INSERT INTO document (source_path, origin, project, kind, title, tags, sha, updated_at, author)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                 ON CONFLICT (source_path) DO UPDATE SET
                     origin = EXCLUDED.origin, project = EXCLUDED.project, kind = EXCLUDED.kind,
                     title = EXCLUDED.title, tags = EXCLUDED.tags, sha = EXCLUDED.sha,
                     updated_at = EXCLUDED.updated_at, author = EXCLUDED.author;",
                &[
                    path,
                    &front.origin,
                    &front.project,
                    &front.kind,
                    &title_ref,
                    &front.tags,
                    &sha,
                    &updated_at,
                    &author,
                ],
            )
            .await
            .context("upsert document")?;

        let doc_id = doc_node_id(path);

        // project node + in_project edge
        if !front.project.is_empty() {
            let pid = format!("project:{}", front.project);
            Self::upsert_node_on(c, &pid, "project", &front.project, None).await?;
            Self::upsert_edge_on(c, &doc_id, &pid, "in_project", None).await?;
        }

        // tagged: remove existing then regenerate (idempotent)
        c.execute(
            "DELETE FROM edge WHERE src = $1 AND kind = 'tagged';",
            &[&doc_id],
        )
        .await?;
        // Topic nodes are shared between notes: take them in tag order for every file, so two
        // files in flight wait on each other instead of deadlocking.
        let mut tags: Vec<&String> = front.tags.iter().collect();
        tags.sort();
        for tag in tags {
            let tid = format!("topic:{tag}");
            Self::upsert_node_on(c, &tid, "topic", tag, None).await?;
            Self::upsert_edge_on(c, &doc_id, &tid, "tagged", None).await?;
        }
        Ok(())
    }

    /// Prune chunks at or beyond `from_idx` for a document — the stale tail left when a re-ingested
    /// note has FEWER chunks than before. Used by the upsert-then-prune re-ingest so a reader never
    /// sees an empty/half-deleted chunk set (no delete-first window). `from_idx == new chunk count`.
    pub async fn prune_chunks_from(&self, path: &str, from_idx: usize) -> Result<()> {
        Self::prune_chunks_from_on(&self.db().await?, path, from_idx).await?;
        self.mirror_surreal(&Scope::notes([path.to_owned()])).await
    }

    /// `prune_chunks_from` on a caller-held connection or transaction handle.
    pub async fn prune_chunks_from_on(
        c: &impl GenericClient,
        path: &str,
        from_idx: usize,
    ) -> Result<()> {
        let from = i32::try_from(from_idx).unwrap_or(i32::MAX);
        c.execute(
            "DELETE FROM chunk WHERE source_path = $1 AND chunk_idx >= $2;",
            &[&path, &from],
        )
        .await?;
        Ok(())
    }

    /// Remove (prune) document + chunks (CASCADE) + graph edges + claims (explicit; claim has no FK).
    pub async fn delete_document(&self, path: &str) -> Result<()> {
        self.db()
            .await?
            .execute("DELETE FROM document WHERE source_path = $1;", &[&path])
            .await?;
        let doc_id = doc_node_id(path);
        self.db()
            .await?
            .execute("DELETE FROM edge WHERE src = $1 OR dst = $1;", &[&doc_id])
            .await?;
        // claim has NO FK to document (unlike chunk's ON DELETE CASCADE) so the document delete does not
        // cascade here — mirror the explicit edge delete above. Provenance is single-valued (source_path
        // is overwritten last-writer-wins on conflict and is not part of the PK), so every claim row
        // carrying this path is owned by THIS document → remove it (current + its own sealed history).
        // Caveat: if this doc owned the latest value of a (subject,predicate) while an OLDER row from
        // another doc stays sealed, that pair loses its current pointer (a MISSING claim, never an
        // orphaned/WRONG one) — inherent to single-valued provenance; the remedy is a re-seal pass.
        self.db()
            .await?
            .execute("DELETE FROM claim WHERE source_path = $1;", &[&path])
            .await?;
        // The chunk rows went with the document (CASCADE), so no SQL above names them; Surreal
        // drops every key of this note that pg no longer holds.
        self.mirror_surreal(&Scope::notes([path.to_owned()])).await
    }

    // ── chunk (embedding) ─────────────────────────────────────────────────────

    pub async fn upsert_chunk(&self, d: &Doc) -> Result<()> {
        self.upsert_chunk_on(&self.db().await?, d).await?;
        self.mirror_surreal(&Scope::notes([d.front.source_path.clone()]))
            .await
    }

    /// `upsert_chunk` on a caller-held connection or transaction handle.
    pub async fn upsert_chunk_on(&self, c: &impl GenericClient, d: &Doc) -> Result<()> {
        let vec = self.checked_vector(&d.embedding)?; // dim guard (shared with upsert_claim)
        let idx = i32::try_from(d.chunk_idx).unwrap_or(i32::MAX);
        c.execute(
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

    pub async fn text_search(&self, query: &str, k: usize) -> Result<Vec<Hit>> {
        let k_i64 = i64::try_from(k).unwrap_or(i64::MAX);
        let rows = self
            .db()
            .await?
            .query(
                "SELECT id, content, origin, project, source_path,
                        ts_rank(tsv, plainto_tsquery('simple', $1))::float4 AS dist
                 FROM chunk WHERE tsv @@ plainto_tsquery('simple', $1)
                 ORDER BY dist DESC LIMIT $2;",
                &[&query, &k_i64],
            )
            .await?;
        Ok(rows
            .iter()
            .map(|r| row_to_hit(r, DistKind::TextRank))
            .collect())
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
            .db()
            .await?
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
        Ok(rows
            .iter()
            .map(|r| row_to_hit(r, DistKind::TextRank))
            .collect())
    }

    pub async fn count(&self) -> Result<usize> {
        pg_count(&*self.db().await?, "SELECT count(*) FROM chunk;").await
    }

    pub async fn all_meta(&self) -> Result<Vec<Meta>> {
        let rows = self
            .db()
            .await?
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
        hits: &[LoggedHit],
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
        let hit_paths: Vec<String> = hits.iter().map(|h| h.path.clone()).collect();
        let hit_dists: Vec<Option<f32>> = hits.iter().map(|h| h.dist).collect();
        let hit_dist_kinds: Vec<Option<&str>> = hits
            .iter()
            .map(|h| h.dist_kind.map(DistKind::as_str))
            .collect();
        self.db().await?
            .execute(
                "INSERT INTO query_log (endpoint, query, hit_paths, hit_dists, hit_dist_kinds, sources, answer_snippet, latency_ms)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8);",
                &[
                    &endpoint,
                    &query,
                    &hit_paths,
                    &hit_dists,
                    &hit_dist_kinds,
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
        let rows = self.db().await?
                        .query(
                "SELECT id, created_at, endpoint, query, hit_paths, hit_dists, hit_dist_kinds, sources, answer_snippet, latency_ms
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
                hit_dists: r.get(5),
                hit_dist_kinds: r.get(6),
                sources: r.get(7),
                answer_snippet: r.get(8),
                latency_ms: r.get(9),
            })
            .collect())
    }

    /// Record one judge's verdict on one hit. Re-labelling the same (query, hit, judge) replaces
    /// that judge's own verdict and nothing else — a human audit never silently rewrites the LLM
    /// row it disagrees with, because the disagreement is the measurement.
    pub async fn record_recall_label(
        &self,
        query_log_id: i32,
        hit_index: i32,
        judge: &str,
        verdict: &str,
        model: &str,
        note: &str,
    ) -> Result<()> {
        self.db()
            .await?
            .execute(
                "INSERT INTO recall_label (query_log_id, hit_index, judge, verdict, model, note)
                 VALUES ($1, $2, $3, $4, $5, $6)
                 ON CONFLICT (query_log_id, hit_index, judge)
                 DO UPDATE SET verdict = EXCLUDED.verdict, model = EXCLUDED.model,
                               note = EXCLUDED.note, created_at = now();",
                &[&query_log_id, &hit_index, &judge, &verdict, &model, &note],
            )
            .await
            .context("record recall label")?;
        Ok(())
    }

    /// Labels newest-first. The sampler reads these to skip pairs it has already judged.
    pub async fn recent_recall_labels(&self, limit: i64) -> Result<Vec<RecallLabelRow>> {
        let rows = self
            .db()
            .await?
            .query(
                "SELECT query_log_id, hit_index, judge, verdict, model, note
                 FROM recall_label ORDER BY created_at DESC LIMIT $1;",
                &[&limit],
            )
            .await
            .context("recent recall labels")?;
        Ok(rows
            .into_iter()
            .map(|r| RecallLabelRow {
                query_log_id: r.get(0),
                hit_index: r.get(1),
                judge: r.get(2),
                verdict: r.get(3),
                model: r.get(4),
                note: r.get(5),
            })
            .collect())
    }

    /// Every project name the corpus actually knows.
    ///
    /// The briefing labels each item with the `##` heading the model wrote above it, and the model
    /// writes section titles there as readily as project names. A name the corpus has never heard
    /// of is not a project the reader can look up, so the briefing checks its headings against
    /// this list. Returning the names, not a count: the caller is deciding membership.
    pub async fn project_names(&self) -> Result<Vec<String>> {
        let rows = self
            .db()
            .await?
            .query(
                "SELECT DISTINCT project FROM document
                  WHERE project IS NOT NULL AND project <> ''
                  ORDER BY project;",
                &[],
            )
            .await
            .context("project names")?;
        Ok(rows.into_iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// Projects with a document touched within `days`, busiest first (ties by name) — `GET /projects?active_days=`.
    pub async fn active_project_counts(&self, days: i32) -> Result<Vec<(String, i64)>> {
        let rows = self
            .db()
            .await?
            .query(crate::projects::ACTIVE_PROJECTS_SQL, &[&days])
            .await
            .context("active project counts")?;
        Ok(rows
            .into_iter()
            .map(|r| (r.get::<_, String>(0), r.get::<_, i64>(1)))
            .collect())
    }

    /// Current `rule` claims for `GET /rules` (SQL copied from the Python door).
    pub async fn rule_rows(&self) -> Result<Vec<crate::rules::RuleRow>> {
        let rows = self
            .db()
            .await?
            .query(crate::rules::SQL, &[&"rule"])
            .await
            .context("rule claims")?;
        Ok(rows
            .into_iter()
            .map(|r| crate::rules::RuleRow {
                subject: r.get(0),
                predicate: r.get(1),
                value: r.get(2),
                source_path: r.get(3),
            })
            .collect())
    }

    /// Current claims of one subject for `GET /claim-source` (SQL copied from the Python door).
    /// `valid_from` is rendered in UTC; the Python door renders in the psycopg connection's
    /// TimeZone, which is unverified.
    pub async fn claim_source_rows(
        &self,
        subject: &str,
    ) -> Result<Vec<crate::claim_source::ClaimRow>> {
        let rows = self
            .db()
            .await?
            .query(crate::claim_source::SQL, &[&subject])
            .await
            .context("claim source")?;
        Ok(rows
            .into_iter()
            .map(|r| crate::claim_source::ClaimRow {
                source_path: r.get(0),
                valid_from: chrono::DateTime::<chrono::Utc>::from(r.get::<_, SystemTime>(1))
                    .fixed_offset(),
                value: r.get(2),
                author: r.get(3),
            })
            .collect())
    }

    /// Current claims of one predicate for `GET /claim-sources` (SQL copied from the Python door).
    /// Same UTC-vs-connection-TimeZone caveat as `claim_source_rows`.
    pub async fn claim_source_list_rows(
        &self,
        predicate: &str,
    ) -> Result<Vec<crate::claim_source::ListRow>> {
        let rows = self
            .db()
            .await?
            .query(crate::claim_source::LIST_SQL, &[&predicate])
            .await
            .context("claim source list")?;
        Ok(rows
            .into_iter()
            .map(|r| crate::claim_source::ListRow {
                subject: r.get(0),
                row: crate::claim_source::ClaimRow {
                    source_path: r.get(1),
                    valid_from: chrono::DateTime::<chrono::Utc>::from(r.get::<_, SystemTime>(2))
                        .fixed_offset(),
                    value: r.get(3),
                    author: r.get(4),
                },
            })
            .collect())
    }

    /// Current claims of one note with the sealed value each replaced, for `GET /claim-priors`.
    pub async fn claim_priors(&self, source: &str) -> Result<Vec<crate::claim_priors::ClaimPrior>> {
        let rows = self
            .db()
            .await?
            .query(crate::claim_priors::SQL, &[&source])
            .await
            .context("claim priors")?;
        Ok(rows
            .into_iter()
            .map(|r| crate::claim_priors::ClaimPrior {
                subject: r.get(0),
                predicate: r.get(1),
                value: r.get(2),
                prior: crate::claim_priors::prior_of(
                    r.get(3),
                    r.get(4),
                    r.get::<_, Option<SystemTime>>(5)
                        .map(chrono::DateTime::<chrono::Utc>::from),
                ),
            })
            .collect())
    }

    /// Every live claim's `(subject, source_path)` for `GET /repairs/split-subjects`. No ORDER BY:
    /// the grouping sorts, so row order changes nothing.
    pub async fn split_subject_rows(&self) -> Result<Vec<(String, String)>> {
        let rows = self
            .db()
            .await?
            .query(crate::split_subjects::SUBJECTS_SQL, &[])
            .await
            .context("split subject rows")?;
        Ok(rows.iter().map(|r| (r.get(0), r.get(1))).collect())
    }

    /// Per-predicate row counts and the longest value of the given spellings, for
    /// `GET /repairs/split-subjects/samples`.
    pub async fn split_samples(
        &self,
        variants: &[String],
    ) -> Result<Vec<crate::split_subjects::Sample>> {
        let rows = self
            .db()
            .await?
            .query(
                crate::split_subjects::SAMPLES_SQL,
                &[
                    &crate::split_subjects::SAMPLE_VALUE_CHARS,
                    &variants,
                    &crate::split_subjects::SAMPLE_LIMIT,
                ],
            )
            .await
            .context("split samples")?;
        Ok(rows
            .iter()
            .map(|r| crate::split_subjects::Sample {
                predicate: r.get(0),
                rows: r.get(1),
                value: r.get(2),
            })
            .collect())
    }

    /// The group `subject` names, read on the request's transaction.
    async fn split_group_on(
        tx: &Transaction<'_>,
        subject: &str,
    ) -> Result<Option<(Vec<String>, Vec<String>)>> {
        let rows = tx
            .query(crate::split_subjects::SUBJECTS_SQL, &[])
            .await
            .context("split subject rows")?;
        let rows: Vec<(String, String)> = rows.iter().map(|r| (r.get(0), r.get(1))).collect();
        Ok(crate::split_subjects::find_group(&rows, subject))
    }

    /// `POST /repairs/split-subjects`: delete the claim rows of one group's other spellings and
    /// blank the affected notes' sha so the next sync re-reads them, in one transaction. Without
    /// the owner's standing the owner-written notes are left as they are and named in
    /// `owner_held`. `None` means `subject` names no live group.
    pub(crate) async fn merge_split_subject(
        &self,
        subject: &str,
        standing: crate::serve::owner::Standing,
    ) -> Result<Option<crate::split_subjects::Merged>> {
        use crate::serve::owner::Standing;
        let mut conn = self.db().await?;
        let tx = conn
            .transaction()
            .await
            .context("begin split merge transaction")?;
        // The first read only names the spellings to lock. The rows the merge acts on are read
        // again once the slots are held, on the same transaction: a row an ingest or a seal
        // committed in between is seen, and no other writer can change them until we commit.
        let Some((spellings, _)) = Self::split_group_on(&tx, subject).await? else {
            return Ok(None);
        };
        Self::lock_split_slots(&tx, &spellings).await?;
        let Some((variants, notes)) = Self::split_group_on(&tx, subject).await? else {
            return Ok(None);
        };
        let owner_held = match standing {
            Standing::Owner => Vec::new(),
            Standing::NotOwner => Self::owner_authored(&tx, &notes).await?,
        };
        let touched: Vec<String> = notes
            .into_iter()
            .filter(|path| !owner_held.contains(path))
            .collect();
        let deleted_rows = tx
            .execute(crate::split_subjects::DELETE_SQL, &[&variants, &touched])
            .await
            .context("delete split claim rows")?;
        let reread_notes = tx
            .execute(crate::split_subjects::BLANK_SHA_SQL, &[&touched])
            .await
            .context("blank split notes' sha")?;
        tx.commit()
            .await
            .context("commit split merge transaction")?;
        self.mirror_surreal(&Scope::notes(touched)).await?;
        Ok(Some(crate::split_subjects::Merged {
            subject: subject.to_owned(),
            deleted_rows,
            reread_notes,
            owner_held,
        }))
    }

    /// Card-session edges for `GET /approved` (SQL copied from the Python door). Row order is
    /// not fixed; the caller sorts.
    pub async fn approved_edges(&self) -> Result<Vec<crate::approved::EdgeRow>> {
        let rows = self
            .db()
            .await?
            .query(crate::approved::SQL, &[])
            .await
            .context("approved edges")?;
        Ok(rows
            .into_iter()
            .map(|r| crate::approved::EdgeRow {
                src: r.get(0),
                dst: r.get(1),
                kind: r.get(2),
            })
            .collect())
    }

    /// Per-judge verdict counts, plus how often the two judges agreed on hits both of them
    /// labelled. `unsure` is counted, never folded into either side — an abstention is not a vote.
    pub async fn recall_label_stats(&self) -> Result<(Vec<RecallLabelStats>, i64, i64)> {
        let rows = self
            .db()
            .await?
            .query(
                "SELECT judge,
                        count(*) FILTER (WHERE verdict = 'relevant')   AS relevant,
                        count(*) FILTER (WHERE verdict = 'irrelevant') AS irrelevant,
                        count(*) FILTER (WHERE verdict = 'unsure')     AS unsure
                 FROM recall_label GROUP BY judge ORDER BY judge;",
                &[],
            )
            .await
            .context("recall label stats")?;
        let per_judge = rows
            .into_iter()
            .map(|r| RecallLabelStats {
                judge: r.get(0),
                relevant: r.get(1),
                irrelevant: r.get(2),
                unsure: r.get(3),
            })
            .collect();
        // Agreement is computed only over pairs BOTH judges labelled with a real verdict; an
        // `unsure` on either side is not agreement or disagreement, so it leaves the denominator.
        let agree = self
            .db()
            .await?
            .query_one(
                "SELECT
                   count(*) FILTER (WHERE l.verdict = h.verdict) AS agreed,
                   count(*)                                     AS compared
                 FROM recall_label l JOIN recall_label h
                   ON l.query_log_id = h.query_log_id AND l.hit_index = h.hit_index
                 WHERE l.judge = 'llm' AND h.judge = 'human'
                   AND l.verdict <> 'unsure' AND h.verdict <> 'unsure';",
                &[],
            )
            .await
            .context("recall label agreement")?;
        Ok((per_judge, agree.get(0), agree.get(1)))
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

        self.db().await?
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
            .db()
            .await?
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
        self.compact_with_lock_timeout(COMPACT_LOCK_TIMEOUT).await
    }

    pub async fn compact_with_lock_timeout(
        &self,
        lock_timeout: std::time::Duration,
    ) -> Result<CompactSummary> {
        let mut report = CompactReport::default();
        let started = std::time::Instant::now();
        let db = self.db().await?;

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
            db.batch_execute(&format!("VACUUM ANALYZE {table};"))
                .await
                .with_context(|| format!("vacuum analyze {table}"))?;
        }
        report.vacuum_ms = t0.elapsed().as_millis();

        // The timeout is session state on a pooled connection: reset it before the connection
        // goes back, whether or not the reindex phase succeeded.
        db.batch_execute(&format!("SET lock_timeout = {}", lock_timeout.as_millis()))
            .await
            .context("set lock_timeout for reindex")?;
        let reindexed = Self::reindex_tables(&db, &mut report).await;
        let reset = db.batch_execute("RESET lock_timeout").await;
        reindexed?;
        reset.context("reset lock_timeout after reindex")?;

        self.finish_compact(&db, report, started).await
    }

    /// `REINDEX TABLE CONCURRENTLY` waits for every older transaction, so under a held
    /// transaction it never ends. A table that cannot be locked within the session's
    /// `lock_timeout` is skipped and named in `report.skipped_lock_timeout`; the cancelled
    /// reindex leaves its `_ccnew` index, which the next run's sweep below drops and counts.
    async fn reindex_tables(db: &Client, report: &mut CompactReport) -> Result<()> {
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
            // Sweep first: a `REINDEX CONCURRENTLY` that fails or is interrupted leaves its
            // half-built index behind as `<name>_ccnew<N>`, marked invalid. Postgres never
            // reclaims those on its own, the next REINDEX cannot reuse the name, and they
            // accumulate one per failed run — twelve of them had collected here by 2026-09-02,
            // four generations deep on three different indexes. They are invisible to the
            // planner, so nothing is slower and nothing errors; they simply never leave.
            let leftovers = db
                .query(
                    // `REINDEX TABLE` rebuilds the table's TOAST index too, so a failure leaves
                    // `pg_toast_<oid>_index_ccnewN` behind -- and that index belongs to the TOAST
                    // table, not to `claim`, so a sweep matching only `t.relname = $1` never saw
                    // it. Nine generations had collected on the claim TOAST by 2026-09-09, one
                    // per failed reindex, while the sweep reported itself clean.
                    //
                    // `c.oid::regclass` rather than `c.relname`, because a TOAST index lives in
                    // the `pg_toast` schema, which is not on `search_path`: `DROP INDEX IF EXISTS
                    // "pg_toast_16765_index_ccnew2"` resolves to nothing, prints
                    // `NOTICE: does not exist, skipping`, and returns success. The sweep would
                    // have counted nine drops and removed nothing -- a gate reporting a number it
                    // did not do. `regclass` renders the schema qualifier when one is needed.
                    "SELECT c.oid::regclass::text FROM pg_class c
                       JOIN pg_index i ON i.indexrelid = c.oid
                       JOIN pg_class t ON t.oid = i.indrelid
                       JOIN pg_class owner ON owner.oid = t.oid OR owner.reltoastrelid = t.oid
                      WHERE NOT i.indisvalid AND owner.relname = $1
                        AND c.relname LIKE '%\\_ccnew%'",
                    &[&table],
                )
                .await
                .with_context(|| format!("list invalid indexes on {table}"))?;
            for row in leftovers {
                // Already quoted where quoting is needed: `regclass` output is a ready
                // identifier, so wrapping it again would make `pg_toast.x` one bare name.
                let name: String = row.get(0);
                db.batch_execute(&format!("DROP INDEX CONCURRENTLY IF EXISTS {name};"))
                    .await
                    .with_context(|| format!("drop invalid index {name}"))?;
                report.dropped_invalid_indexes += 1;
            }
            match db
                .batch_execute(&format!("REINDEX TABLE CONCURRENTLY {table};"))
                .await
            {
                Ok(()) => {}
                Err(e) if e.code() == Some(&SqlState::LOCK_NOT_AVAILABLE) => {
                    report.skipped_lock_timeout.push(table.to_owned());
                }
                Err(e) => return Err(e).with_context(|| format!("reindex table {table}")),
            }
        }
        report.reindex_ms = t0.elapsed().as_millis();
        Ok(())
    }

    async fn finish_compact(
        &self,
        db: &Client,
        mut report: CompactReport,
        started: std::time::Instant,
    ) -> Result<CompactSummary> {
        let pruned = db
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
        report.gc_claim_nodes = gc.claim_nodes;
        report.gc_claim_edges = gc.claim_edges;

        report.sealed_superseded_claims = self
            .seal_claims_under_supersedes_edges()
            .await
            .context("seal superseded claims")?;

        Ok(CompactSummary {
            report,
            total_ms: started.elapsed().as_millis(),
        })
    }

    // ── graph helpers (node/edge upsert) ───────────────────────────────────────

    async fn upsert_node_on(
        c: &impl GenericClient,
        id: &str,
        kind: &str,
        label: &str,
        outcome: Option<&str>,
    ) -> Result<()> {
        c.execute(
                "INSERT INTO node (id, kind, label, outcome) VALUES ($1, $2, $3, $4)
                 ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label, outcome = EXCLUDED.outcome;",
                &[&id, &kind, &label, &outcome],
            )
            .await
            .context("upsert node")?;
        Ok(())
    }

    async fn upsert_edge_on(
        c: &impl GenericClient,
        src: &str,
        dst: &str,
        kind: &str,
        judge: Option<&str>,
    ) -> Result<()> {
        let sql = format!(
            "INSERT INTO edge (src, dst, kind, judge) VALUES ($1, $2, $3, $4)
             ON CONFLICT (src, dst, kind) DO UPDATE SET judge = EXCLUDED.judge
             WHERE {} > {};",
            judge_rank_sql("EXCLUDED.judge"),
            judge_rank_sql("edge.judge"),
        );
        c.execute(&sql, &[&src, &dst, &kind, &judge])
            .await
            .context("upsert edge")?;
        Ok(())
    }

    /// The only writer of `supersedes` edges: what one note replaced another is read out of the
    /// session's own words at scoring time, not emitted by the distilling LLM. Idempotent — a
    /// repeat call adds edges that are not already there, never deletes. Skipped pairs (same path
    /// twice, or a path with no `document` row) are counted in `unknown`, not errored. Each edge
    /// carries `judge` like a consumption edge does.
    ///
    /// The edge also retires what the older note currently asserts: its current claims are
    /// sealed, stamped with the newer note's latest claim `valid_from` (a non-fact claim is an
    /// item in a list, so inside one note only that note's own later row could retire it — the
    /// explicit supersedes is the one legitimate retirement from outside). Each fact slot the
    /// retired note touched passes to the latest remaining live row. Whether the edge retires
    /// at all is `{RETIRED_NOTE}` — an owner-written older note yields only to an owner-written
    /// newer one, the store-side half of the door's refusal.
    ///
    /// Runs on the request's transaction handle `tx`; the caller opens and commits it.
    pub async fn record_supersedes(
        tx: &Transaction<'_>,
        pairs: &[[String; 2]],
        judge: Option<&str>,
    ) -> Result<SupersedesReport> {
        Self::record_supersedes_sealing(tx, pairs, judge, SealRule::WholeNote).await
    }

    /// `record_supersedes` with the seal rule named by the caller.
    async fn record_supersedes_sealing(
        tx: &Transaction<'_>,
        pairs: &[[String; 2]],
        judge: Option<&str>,
        rule: SealRule,
    ) -> Result<SupersedesReport> {
        let mut report = SupersedesReport::default();
        let (pairs, skipped) = split_supersedes(pairs);
        report.unknown += skipped.len();
        report
            .unknown_paths
            .extend(skipped.iter().map(|p| (*p).to_owned()));
        // Known pairs are picked in request order (so `unknown_paths` keeps it), then written
        // sorted by (older, newer): two requests touching the same older notes take the seal's
        // row locks in the same order, so opposite-order requests wait instead of deadlocking.
        let mut known = Vec::new();
        for pair in pairs {
            if Self::get_doc_sha_on(tx, &pair[0]).await?.is_none() {
                report.unknown += 1;
                report.unknown_paths.push(pair[0].clone());
                continue;
            }
            if Self::get_doc_sha_on(tx, &pair[1]).await?.is_none() {
                report.unknown += 1;
                report.unknown_paths.push(pair[1].clone());
                continue;
            }
            known.push(pair);
        }
        known.sort_by(|a, b| (&a[1], &a[0]).cmp(&(&b[1], &b[0])));
        for pair in known {
            Self::upsert_edge_on(
                tx,
                &doc_node_id(&pair[0]),
                &doc_node_id(&pair[1]),
                "supersedes",
                judge,
            )
            .await?;
            report.supersedes += 1;
            match rule {
                SealRule::WholeNote => {
                    Self::seal_superseded_claims(tx, &pair[0], &pair[1]).await?;
                }
                SealRule::RestatedSlots => {
                    Self::seal_restated_slots(tx, &pair[0], &pair[1]).await?;
                }
            }
            Self::note_retired_slots(tx, &pair[0], &pair[1], &mut report.retired).await?;
        }
        Ok(report)
    }

    /// Owner decision, as the Python writer does it: close only the `(subject, predicate)` slots
    /// the newer note re-states, stamped like the whole-note seal. The older note's other facts
    /// stay live, and nothing is handed on to a next-latest row. Same owner rule as the whole
    /// seal (`{RETIRED_NOTE}`).
    async fn seal_restated_slots(tx: &Transaction<'_>, newer: &str, older: &str) -> Result<()> {
        Self::lock_claim_slots_of(tx, newer).await?;
        tx.execute(
            &format!(
                "UPDATE claim c SET superseded_at = COALESCE(
                        (SELECT max(valid_from) FROM claim WHERE source_path = $1),
                        now())
                 WHERE c.source_path = $2 AND c.superseded_at IS NULL
                   AND (c.subject, c.predicate) IN
                       (SELECT subject, predicate FROM claim WHERE source_path = $1)
                   AND {RETIRED_NOTE};",
            ),
            &[&newer, &older],
        )
        .await
        .context("seal re-stated slots of the superseded note")?;
        Ok(())
    }

    /// Appends the older note's slots closed by this write to `retired`, each once. Read after
    /// the seal: whichever rule closed a row (the claim write's latest-wins or the seal), a row
    /// of the older note stamped with the newer note's latest `valid_from` was closed by it.
    async fn note_retired_slots(
        tx: &Transaction<'_>,
        newer: &str,
        older: &str,
        retired: &mut Vec<(String, String)>,
    ) -> Result<()> {
        let rows = tx
            .query(
                "SELECT c.subject, c.predicate FROM claim c
                  WHERE c.source_path = $2
                    AND c.superseded_at = (SELECT max(valid_from) FROM claim WHERE source_path = $1)
                  ORDER BY c.subject, c.predicate;",
                &[&newer, &older],
            )
            .await
            .context("read retired claims")?;
        for row in rows {
            let slot: (String, String) = (row.get(0), row.get(1));
            if !retired.contains(&slot) {
                retired.push(slot);
            }
        }
        Ok(())
    }

    /// Lock every claim row in the canon slots `source_path` touches, slot by slot. Ingest takes
    /// a slot's rows in `sorted_claims` order (byte order of the canon subject, then predicate);
    /// the seals below would otherwise take them in table scan order, and two requests on the
    /// same slots then wait on each other in a cycle. `"C"` collation is Rust's `str` order.
    async fn lock_claim_slots_of(tx: &Transaction<'_>, source_path: &str) -> Result<()> {
        let canon_subject = canon_sql("c.subject");
        let canon_predicate = canon_sql("c.predicate");
        let touched_subject = canon_sql("subject");
        let touched_predicate = canon_sql("predicate");
        tx.execute(
            &format!(
                "SELECT 1 FROM claim c
                 WHERE ({canon_subject}, {canon_predicate}) IN
                       (SELECT {touched_subject}, {touched_predicate} FROM claim
                        WHERE source_path = $1)
                 ORDER BY ({canon_subject}) COLLATE \"C\", ({canon_predicate}) COLLATE \"C\",
                          c.valid_from, c.subject COLLATE \"C\", c.predicate COLLATE \"C\"
                 FOR UPDATE OF c;"
            ),
            &[&source_path],
        )
        .await
        .context("lock claim slots in slot order")?;
        Ok(())
    }

    /// Lock every claim row carrying one of `spellings`, in the order `lock_claim_slots_of` takes
    /// a slot's rows (canon subject, canon predicate in `"C"` collation, then `valid_from`), so a
    /// merge and an ingest or seal on the same slots wait on each other instead of in a cycle.
    async fn lock_split_slots(tx: &Transaction<'_>, spellings: &[String]) -> Result<()> {
        let canon_subject = canon_sql("c.subject");
        let canon_predicate = canon_sql("c.predicate");
        tx.execute(
            &format!(
                "SELECT 1 FROM claim c
                 WHERE c.subject = ANY($1)
                 ORDER BY ({canon_subject}) COLLATE \"C\", ({canon_predicate}) COLLATE \"C\",
                          c.valid_from, c.subject COLLATE \"C\", c.predicate COLLATE \"C\"
                 FOR UPDATE OF c;"
            ),
            &[&spellings],
        )
        .await
        .context("lock split slots in slot order")?;
        Ok(())
    }

    /// Seal every current claim of the superseded note: `superseded_at` = the newer note's
    /// latest claim `valid_from`, falling back to `now()` when the newer note asserts nothing.
    /// Only `superseded_at IS NULL` rows move, so a repeat call — and a later `compact` over the
    /// same edge — changes nothing. The seal fires only when the note is actually retired —
    /// the owner rule lives in `{RETIRED_NOTE}`, the same fragment every other site tests.
    /// After the seal, each fact slot the retired note touched is handed to the next-latest
    /// live row: with the retired note out of the competition the latest remaining live row
    /// becomes current, instead of the slot ending with none.
    async fn seal_superseded_claims(
        tx: &Transaction<'_>,
        newer: &str,
        older: &str,
    ) -> Result<usize> {
        Self::lock_claim_slots_of(tx, older).await?;
        let sealed = tx
            .execute(
                &format!(
                    "UPDATE claim c SET superseded_at = COALESCE(
                            (SELECT max(valid_from) FROM claim WHERE source_path = $1),
                            now())
                     WHERE c.source_path = $2 AND c.superseded_at IS NULL
                       AND {RETIRED_NOTE};",
                ),
                &[&newer, &older],
            )
            .await
            .context("seal superseded note claims")?;
        // Hand each fact slot the retired note touched to its next-latest live row: the
        // slot's max is taken over live rows only (`NOT {RETIRED_NOTE}`), so the latest
        // among them becomes current. A slot with no live row left stays empty — all its
        // rows are the retired note's, sealed above.
        // The slot is the canon spelling; its latest is the canon-spelled row first, then
        // `valid_from`, so an old-spelling row never outranks a canon-spelled one.
        let canon_subject = canon_sql("c.subject");
        let canon_predicate = canon_sql("c.predicate");
        let canon_touched_subject = canon_sql("subject");
        let canon_touched_predicate = canon_sql("predicate");
        let is_canon = is_canon_sql("c");
        tx.execute(
                &format!(
                    "UPDATE claim c SET superseded_at = NULL
                     FROM (SELECT DISTINCT ON (cs, cp) cs, cp, is_canon, valid_from
                           FROM (SELECT {canon_subject} AS cs, {canon_predicate} AS cp,
                                        {is_canon} AS is_canon, c.valid_from
                                 FROM claim c
                                 WHERE ({canon_subject}, {canon_predicate}) IN
                                       (SELECT {canon_touched_subject}, {canon_touched_predicate} FROM claim
                                        WHERE source_path = $1 AND kind = 'fact')
                                   AND NOT {RETIRED_NOTE}) r
                           ORDER BY cs, cp, is_canon DESC, valid_from DESC) m
                     WHERE c.kind = 'fact' AND c.superseded_at IS NOT NULL
                       AND {canon_subject} = m.cs AND {canon_predicate} = m.cp
                       AND {is_canon} = m.is_canon AND c.valid_from = m.valid_from;",
                ),
                &[&older],
            )
            .await
            .context("promote next-latest live fact")?;
        Ok(usize::try_from(sealed).unwrap_or(0))
    }

    /// The same retirement over every `supersedes` edge already on the graph — the sweep
    /// reaches edges whose retirement pass has not run yet, e.g. one written straight onto
    /// the graph. Returns the number of claims sealed.
    async fn seal_claims_under_supersedes_edges(&self) -> Result<usize> {
        let rows = self
            .db()
            .await?
            .query("SELECT src, dst FROM edge WHERE kind = 'supersedes';", &[])
            .await
            .context("list supersedes edges")?;
        let mut total = 0_usize;
        for row in rows {
            let src: String = row.get(0);
            let dst: String = row.get(1);
            let (Some(newer), Some(older)) = (
                src.strip_prefix("doc:").map(str::to_owned),
                dst.strip_prefix("doc:").map(str::to_owned),
            ) else {
                continue;
            };
            // One transaction per edge: a failure at edge N keeps the seals of edges before it.
            let mut conn = self.db().await?;
            let tx = conn.transaction().await.context("begin seal transaction")?;
            total += Self::seal_superseded_claims(&tx, &newer, &older).await?;
            tx.commit().await.context("commit seal transaction")?;
            self.mirror_surreal(&Scope::notes([older])).await?;
        }
        Ok(total)
    }

    /// A pooled connection for a caller that opens its own request transaction
    /// (`conn.transaction()`) and passes the handle to the `&Transaction` functions below.
    pub async fn conn(&self) -> Result<deadpool_postgres::Object> {
        self.db().await
    }

    /// Which of `paths` the owner wrote (`document.author = 'owner'`).
    pub async fn owner_authored(tx: &Transaction<'_>, paths: &[String]) -> Result<Vec<String>> {
        if paths.is_empty() {
            return Ok(Vec::new());
        }
        let rows = tx
            .query(
                "SELECT source_path FROM document WHERE source_path = ANY($1) AND author = 'owner' ORDER BY source_path;",
                &[&paths],
            )
            .await
            .context("owner-authored documents")?;
        Ok(rows.iter().map(|r| r.get(0)).collect())
    }

    /// The scorer's write door: record what one session consumed. Upserts `session:<id>` (label =
    /// `observed_at`) and one `used`/`contested` edge per known path, each carrying `judge` — who
    /// judged ('owner', 'inferred', 'agent:<name>', or NULL when the caller brings no name). An
    /// edge that already exists changes judge only upward (`judge_rank_sql`: owner > agent:* >
    /// inferred > NULL), so an owner press lands on an edge distill inferred and nothing lowers
    /// it. Idempotent — a repeat call updates the label and adds edges, never deletes. Paths with no `document` row are skipped
    /// and counted in the report, not errored.
    ///
    /// Runs on the request's transaction handle `tx`; the caller opens and commits it, so a
    /// failure anywhere leaves none of the request's rows.
    pub async fn record_consumption(
        tx: &Transaction<'_>,
        session_id: &str,
        observed_at: &str,
        used: &[String],
        contested: &[String],
        supersedes: &[[String; 2]],
        judge: Option<&str>,
    ) -> Result<ConsumptionReport> {
        let session_node = format!("session:{session_id}");
        Self::upsert_node_on(tx, &session_node, "session", observed_at, None).await?;

        let mut report = ConsumptionReport::default();
        for path in used {
            let Some(keyed) = Self::resolve_doc_path_on(tx, path).await? else {
                report.unknown += 1;
                continue;
            };
            Self::upsert_edge_on(tx, &session_node, &doc_node_id(&keyed), "used", judge).await?;
            report.used += 1;
        }
        for path in contested {
            let Some(keyed) = Self::resolve_doc_path_on(tx, path).await? else {
                report.unknown += 1;
                continue;
            };
            Self::upsert_edge_on(tx, &session_node, &doc_node_id(&keyed), "contested", judge)
                .await?;
            report.contested += 1;
        }
        let supersedes_report = Self::record_supersedes(tx, supersedes, judge).await?;
        report.supersedes += supersedes_report.supersedes;
        report.unknown += supersedes_report.unknown;
        Ok(report)
    }

    /// The handover door: record what one session was handed, without judging it. Upserts
    /// `session:<id>` (label = `observed_at`) and one `handed` edge per known path — the same
    /// skeleton as `record_consumption`, with edge kind `handed` instead of a verdict. Handed
    /// edges are deliberately invisible to `consumption_counts`: being handed is not being
    /// consumed. Idempotent — a repeat call updates the label and adds no duplicate edges. Paths
    /// with no `document` row are counted in `unknown`, not errored.
    ///
    /// Runs on the request's transaction handle `tx`; the caller opens and commits it.
    pub async fn record_handover(
        tx: &Transaction<'_>,
        session_id: &str,
        observed_at: &str,
        paths: &[String],
    ) -> Result<HandoverReport> {
        let session_node = format!("session:{session_id}");
        Self::upsert_node_on(tx, &session_node, "session", observed_at, None).await?;

        let mut report = HandoverReport::default();
        for path in paths {
            let Some(keyed) = Self::resolve_doc_path_on(tx, path).await? else {
                report.unknown += 1;
                continue;
            };
            Self::upsert_edge_on(tx, &session_node, &doc_node_id(&keyed), "handed", None).await?;
            report.handed += 1;
        }
        Ok(report)
    }

    /// `record_handover` in its own transaction — for a caller whose request is that one call.
    pub async fn handover_in_tx(
        &self,
        session_id: &str,
        observed_at: &str,
        paths: &[String],
    ) -> Result<HandoverReport> {
        let mut conn = self.db().await?;
        let tx = conn
            .transaction()
            .await
            .context("begin handover transaction")?;
        let report = Self::record_handover(&tx, session_id, observed_at, paths).await?;
        tx.commit().await.context("commit handover transaction")?;
        self.mirror_surreal(&Scope::graph([format!("session:{session_id}")]))
            .await?;
        Ok(report)
    }

    /// One gap report, in its own transaction: the `gap:` node (label = the question, outcome =
    /// kind), the `session:` node (label = `observed_at`, overwritten on every call), the
    /// session→gap edge, and a `gap_handed` edge to each handed note that has a `document` row.
    /// Same writes as the Python door's `record_gap`; a repeat adds no edge twice.
    pub async fn record_gap_in_tx(
        &self,
        args: &crate::gap::GapArgs,
        observed_at: &str,
    ) -> Result<crate::gap::GapReport> {
        let gap = crate::gap::gap_node_id(&args.session_id, &args.query);
        let session_node = format!("session:{}", args.session_id);
        let mut conn = self.db().await?;
        let tx = conn.transaction().await.context("begin gap transaction")?;
        Self::upsert_node_on(&tx, &gap, "gap", &args.query, Some(args.kind.as_str())).await?;
        Self::upsert_node_on(&tx, &session_node, "session", observed_at, None).await?;
        Self::upsert_edge_on(&tx, &session_node, &gap, "gap", None).await?;
        let known: HashSet<String> = tx
            .query(
                "SELECT source_path FROM document WHERE source_path = ANY($1);",
                &[&args.handed],
            )
            .await
            .context("known handed documents")?
            .iter()
            .map(|r| r.get(0))
            .collect();
        let handed: Vec<&String> = args.handed.iter().filter(|p| known.contains(*p)).collect();
        for path in &handed {
            Self::upsert_edge_on(&tx, &gap, &doc_node_id(path), "gap_handed", None).await?;
        }
        tx.commit().await.context("commit gap transaction")?;
        self.mirror_surreal(&Scope::graph([gap.clone(), session_node]))
            .await?;
        Ok(crate::gap::GapReport {
            gap,
            kind: args.kind.as_str(),
            handed: handed.len(),
            unknown: args.handed.len() - handed.len(),
        })
    }

    /// `record_supersedes` in its own transaction — for a caller whose request is that one call.
    pub async fn supersedes_in_tx(
        &self,
        pairs: &[[String; 2]],
        judge: Option<&str>,
    ) -> Result<SupersedesReport> {
        let mut conn = self.db().await?;
        let tx = conn
            .transaction()
            .await
            .context("begin supersedes transaction")?;
        let report = Self::record_supersedes(&tx, pairs, judge).await?;
        tx.commit().await.context("commit supersedes transaction")?;
        self.mirror_surreal(&Scope::notes(pairs.iter().flatten().cloned()))
            .await?;
        Ok(report)
    }

    /// The supersedes write of a `remember` request, in its own transaction: it closes only the
    /// slots the new note re-states. `compact` and verdict writes keep the whole-note seal.
    pub async fn remember_supersedes_in_tx(
        &self,
        pairs: &[[String; 2]],
        judge: Option<&str>,
    ) -> Result<SupersedesReport> {
        let mut conn = self.db().await?;
        let tx = conn
            .transaction()
            .await
            .context("begin supersedes transaction")?;
        let report = Self::remember_supersedes(&tx, pairs, judge).await?;
        tx.commit().await.context("commit supersedes transaction")?;
        self.mirror_surreal(&Scope::notes(pairs.iter().flatten().cloned()))
            .await?;
        Ok(report)
    }

    /// `remember_supersedes_in_tx` on the request's transaction handle: the caller opens and
    /// commits it, so the edges commit or roll back with the note they came from.
    pub async fn remember_supersedes(
        tx: &Transaction<'_>,
        pairs: &[[String; 2]],
        judge: Option<&str>,
    ) -> Result<SupersedesReport> {
        Self::record_supersedes_sealing(tx, pairs, judge, SealRule::RestatedSlots).await
    }

    /// Consumption counts (`used`/`contested` edges) per document — one query for all hits of a
    /// `/search` response. Keyed by `source_path`; documents with no consumption edges are absent.
    /// `handed` edges never count here: being handed is not being consumed.
    pub async fn consumption_counts(
        &self,
        paths: &[String],
    ) -> Result<HashMap<String, ConsumptionCounts>> {
        self.counts_by(paths, ContestedBy::AnyJudge).await
    }

    /// The counts ranking feeds back: on an owner note, only the owner's own `contested` edges
    /// count — no other judge lowers what the owner wrote. `used` counts from every judge.
    pub async fn ranking_feedback_counts(
        &self,
        paths: &[String],
    ) -> Result<HashMap<String, ConsumptionCounts>> {
        self.counts_by(paths, ContestedBy::OwnerOnOwnerNotes).await
    }

    async fn counts_by(
        &self,
        paths: &[String],
        contested_by: ContestedBy,
    ) -> Result<HashMap<String, ConsumptionCounts>> {
        let doc_ids: Vec<String> = paths.iter().map(|p| doc_node_id(p)).collect();
        let owner_only = match contested_by {
            ContestedBy::AnyJudge => false,
            ContestedBy::OwnerOnOwnerNotes => true,
        };
        let rows = self
            .db()
            .await?
            .query(
                "SELECT e.dst, e.kind, count(*) FROM edge e
                 LEFT JOIN document d ON d.source_path = substr(e.dst, 5)
                 WHERE e.dst = ANY($1) AND e.kind IN ('used','contested')
                   AND NOT ($2 AND e.kind = 'contested'
                            AND d.author IS NOT DISTINCT FROM 'owner'
                            AND e.judge IS DISTINCT FROM 'owner')
                 GROUP BY e.dst, e.kind;",
                &[&doc_ids, &owner_only],
            )
            .await
            .context("consumption counts")?;
        let mut counts: HashMap<String, ConsumptionCounts> = HashMap::new();
        for row in rows {
            let dst: String = row.get(0);
            let kind: String = row.get(1);
            let n: i64 = row.get(2);
            let path = dst.strip_prefix("doc:").unwrap_or(&dst).to_owned();
            let entry = counts.entry(path).or_default();
            if kind == "used" {
                entry.used = n;
            } else {
                entry.contested = n;
            }
        }
        Ok(counts)
    }

    /// Current claims the owner said, per note, from the note's own claim rows — a claim node is
    /// a slot shared by every note that states it, so the edges cannot say whose row it was.
    pub async fn said_by_owner_counts(&self, paths: &[String]) -> Result<HashMap<String, i64>> {
        let rows = self
            .db()
            .await?
            .query(
                "SELECT source_path, count(*) FROM claim
                 WHERE source_path = ANY($1) AND said_by = 'owner' AND superseded_at IS NULL
                 GROUP BY source_path;",
                &[&paths],
            )
            .await
            .context("said-by-owner counts")?;
        Ok(rows
            .into_iter()
            .map(|row| (row.get(0), row.get(1)))
            .collect())
    }

    /// Mirror the note's `said_by` onto its own latest row for the slot — apart from
    /// `claim_is_unchanged`, whose tuple does not carry it, so an edited `said_by` still lands.
    pub async fn mirror_claim_said_by(
        &self,
        subject: &str,
        predicate: &str,
        source_path: &str,
        said_by: Option<crate::frontmatter::SaidBy>,
    ) -> Result<()> {
        Self::mirror_claim_said_by_on(&self.db().await?, subject, predicate, source_path, said_by)
            .await
    }

    /// `mirror_claim_said_by` on a caller-held connection or transaction handle.
    pub async fn mirror_claim_said_by_on(
        c: &impl GenericClient,
        subject: &str,
        predicate: &str,
        source_path: &str,
        said_by: Option<crate::frontmatter::SaidBy>,
    ) -> Result<()> {
        let said_by = said_by.map(String::from);
        c.execute(
            "UPDATE claim SET said_by = $4
                 WHERE subject = $1 AND predicate = $2 AND source_path = $3
                   AND said_by IS DISTINCT FROM $4
                   AND valid_from = (SELECT max(valid_from) FROM claim
                                      WHERE subject = $1 AND predicate = $2 AND source_path = $3);",
            &[&subject, &predicate, &source_path, &said_by],
        )
        .await
        .context("mirror claim said_by")?;
        Ok(())
    }

    /// Paths handed to one session — the `source_path`s of the documents its `handed` edges
    /// reach, distinct, in path order. This is what a bare `{session_id, verdict}` `/consumption`
    /// applies the verdict to: the engine already knows what was handed, the caller only brings
    /// the thumbs-up/down. An unknown session hands nothing — an empty read, not an error.
    pub async fn handed_paths(tx: &Transaction<'_>, session_id: &str) -> Result<Vec<String>> {
        let rows = tx
            .query(
                "SELECT DISTINCT dst FROM edge WHERE src = $1 AND kind = 'handed' ORDER BY dst;",
                &[&format!("session:{session_id}")],
            )
            .await
            .context("handed paths")?;
        Ok(rows
            .iter()
            .map(|row| {
                let dst: String = row.get(0);
                dst.strip_prefix("doc:").unwrap_or(&dst).to_owned()
            })
            .collect())
    }

    /// The concept names written on each document (`about` edges to `concept:` nodes), as the
    /// note spelled them — what `recall` shows so a writer reuses a name instead of coining one.
    /// Same query as the Python door's `pg.concepts_by_doc`.
    pub async fn concepts_of(&self, paths: &[String]) -> Result<HashMap<String, Vec<String>>> {
        let doc_ids: Vec<String> = paths.iter().map(|p| doc_node_id(p)).collect();
        let rows = self
            .db()
            .await?
            .query(
                "SELECT e.src, n.label FROM edge e JOIN node n ON n.id = e.dst
                 WHERE e.src = ANY($1) AND e.kind = 'about' AND n.kind = 'concept'
                 ORDER BY e.src, n.label;",
                &[&doc_ids],
            )
            .await
            .context("concepts of")?;
        let mut map: HashMap<String, Vec<String>> = HashMap::new();
        for row in rows {
            let src: String = row.get(0);
            let label: String = row.get(1);
            map.entry(src.strip_prefix("doc:").unwrap_or(&src).to_owned())
                .or_default()
                .push(label);
        }
        Ok(map)
    }

    /// `supersedes` edges pointing at these documents — the `source_path`s of the newer docs that
    /// replaced each one, i.e. `superseded_by` for every hit of a `/search` response in one query.
    pub async fn superseded_by(&self, paths: &[String]) -> Result<HashMap<String, Vec<String>>> {
        let doc_ids: Vec<String> = paths.iter().map(|p| doc_node_id(p)).collect();
        let rows = self
            .db()
            .await?
            .query(
                "SELECT dst, src FROM edge WHERE dst = ANY($1) AND kind = 'supersedes';",
                &[&doc_ids],
            )
            .await
            .context("superseded by")?;
        let mut map: HashMap<String, Vec<String>> = HashMap::new();
        for row in rows {
            let dst: String = row.get(0);
            let src: String = row.get(1);
            map.entry(dst.strip_prefix("doc:").unwrap_or(&dst).to_owned())
                .or_default()
                .push(src.strip_prefix("doc:").unwrap_or(&src).to_owned());
        }
        Ok(map)
    }

    /// Per document: replaced by a newer note (a `supersedes` edge points at it), and whether
    /// the owner wrote it, then when (`updated_at`). The one read that orders a returned set.
    pub async fn rank_facts(&self, paths: &[String]) -> Result<HashMap<String, RankFacts>> {
        let rows = self
            .db()
            .await?
            .query(
                "SELECT d.source_path, d.author = 'owner', d.updated_at,
                        EXISTS (SELECT 1 FROM edge e
                                WHERE e.dst = 'doc:' || d.source_path AND e.kind = 'supersedes')
                 FROM document d WHERE d.source_path = ANY($1);",
                &[&paths],
            )
            .await
            .context("rank facts")?;
        Ok(rows
            .iter()
            .map(|row| {
                let authorship = if row.get::<_, bool>(1) {
                    Authorship::Owner {
                        updated_at: row.get(2),
                    }
                } else {
                    Authorship::Other
                };
                let facts = RankFacts {
                    superseded: row.get(3),
                    authorship,
                };
                (row.get(0), facts)
            })
            .collect())
    }

    /// Documents with the most `used` edges from sessions whose `observed_at` label falls within
    /// the last `days` days, newest signal first. Each row carries its `used` and `contested`
    /// counts over the same window — the "reused this week" section of the briefing.
    ///
    /// The session label is an RFC 3339 string; it is compared by casting `label::timestamptz`,
    /// guarded by `kind = 'session'` so non-session nodes are never cast, and by a regex that
    /// requires the RFC 3339 date-time prefix — a label that fails to parse must not abort the
    /// query, it just drops out of the window.
    pub async fn reused_recently(
        &self,
        days: i64,
        limit: i64,
    ) -> Result<Vec<(RecentDoc, i64, i64)>> {
        let rows = self
            .db()
            .await?
            .query(
                "WITH window_sessions AS (
                     SELECT id FROM node
                     WHERE kind = 'session'
                       AND label ~ '^\\d{4}-\\d{2}-\\d{2}T'
                       AND label::timestamptz >= now() - make_interval(days => ($1::bigint)::int)
                 ),
                 counts AS (
                     SELECT e.dst AS doc_node,
                            count(*) FILTER (WHERE e.kind = 'used')      AS used,
                            count(*) FILTER (WHERE e.kind = 'contested') AS contested
                     FROM edge e
                     JOIN window_sessions s ON s.id = e.src
                     WHERE e.kind IN ('used', 'contested')
                     GROUP BY e.dst
                     ORDER BY used DESC, contested DESC, e.dst ASC
                     LIMIT $2
                 )
                 SELECT d.source_path, d.project, d.tags,
                        coalesce(string_agg(c.content, E'\\n' ORDER BY c.chunk_idx), '') AS content,
                        counts.used, counts.contested
                 FROM counts
                 JOIN document d ON ('doc:' || d.source_path) = counts.doc_node
                 LEFT JOIN chunk c ON c.source_path = d.source_path
                 GROUP BY d.source_path, d.project, d.tags, counts.used, counts.contested
                 ORDER BY counts.used DESC, counts.contested DESC, d.source_path ASC;",
                &[&days, &limit],
            )
            .await
            .context("reused recently")?;
        Ok(rows
            .iter()
            .map(|r| {
                (
                    RecentDoc {
                        source_path: r.get(0),
                        project: r.get(1),
                        tags: r.get(2),
                        content: r.get(3),
                    },
                    r.get(4),
                    r.get(5),
                )
            })
            .collect())
    }

    // ── semantic nodes ──────────────────────────────────────────────────────────

    pub async fn upsert_tool(&self, slug: &str, text: &str) -> Result<()> {
        Self::upsert_tool_on(&self.db().await?, slug, text).await?;
        self.mirror_surreal(&Scope::graph([format!("tool:{slug}")]))
            .await
    }
    pub async fn upsert_tool_on(c: &impl GenericClient, slug: &str, text: &str) -> Result<()> {
        Self::upsert_node_on(c, &format!("tool:{slug}"), "tool", text, None).await
    }
    pub async fn upsert_concept(&self, slug: &str, text: &str) -> Result<()> {
        Self::upsert_concept_on(&self.db().await?, slug, text).await?;
        self.mirror_surreal(&Scope::graph([format!("concept:{slug}")]))
            .await
    }
    pub async fn upsert_concept_on(c: &impl GenericClient, slug: &str, text: &str) -> Result<()> {
        Self::upsert_node_on(c, &format!("concept:{slug}"), "concept", text, None).await
    }

    /// Remove this document's semantic edges (uses/about) — makes the deterministic graph rebuild idempotent.
    pub async fn clear_semantic_edges(&self, doc_path: &str) -> Result<()> {
        Self::clear_semantic_edges_on(&self.db().await?, doc_path).await?;
        self.mirror_surreal(&Scope::notes([doc_path.to_owned()]))
            .await
    }
    pub async fn clear_semantic_edges_on(c: &impl GenericClient, doc_path: &str) -> Result<()> {
        let doc_id = doc_node_id(doc_path);
        let kinds: Vec<&str> = SEMANTIC_EDGE_KINDS.to_vec();
        c.execute(
            "DELETE FROM edge WHERE src = $1 AND kind = ANY($2);",
            &[&doc_id, &kinds],
        )
        .await?;
        Ok(())
    }

    // ── semantic edges (doc → entity) ───────────────────────────────────────────

    pub async fn relate_doc_tool(&self, doc_path: &str, slug: &str) -> Result<()> {
        Self::relate_doc_tool_on(&self.db().await?, doc_path, slug).await?;
        self.mirror_surreal(&Scope::notes([doc_path.to_owned()]))
            .await
    }
    pub async fn relate_doc_tool_on(
        c: &impl GenericClient,
        doc_path: &str,
        slug: &str,
    ) -> Result<()> {
        Self::upsert_edge_on(
            c,
            &doc_node_id(doc_path),
            &format!("tool:{slug}"),
            "uses",
            None,
        )
        .await
    }
    pub async fn relate_doc_concept(&self, doc_path: &str, slug: &str) -> Result<()> {
        Self::relate_doc_concept_on(&self.db().await?, doc_path, slug).await?;
        self.mirror_surreal(&Scope::notes([doc_path.to_owned()]))
            .await
    }
    pub async fn relate_doc_concept_on(
        c: &impl GenericClient,
        doc_path: &str,
        slug: &str,
    ) -> Result<()> {
        Self::upsert_edge_on(
            c,
            &doc_node_id(doc_path),
            &format!("concept:{slug}"),
            "about",
            None,
        )
        .await
    }

    /// `person:owner —said→ doc` and `—said→ claim:{subject}:{predicate}`, from the note's own
    /// `said_by` line, so a re-ingest writes them again. No judge: a speaker is not a verdict.
    pub async fn relate_said(
        &self,
        said_by: crate::frontmatter::SaidBy,
        doc_path: &str,
        subject: &str,
        predicate: &str,
    ) -> Result<()> {
        Self::relate_said_on(&self.db().await?, said_by, doc_path, subject, predicate).await?;
        self.mirror_surreal(&Scope::notes([doc_path.to_owned()]))
            .await
    }
    /// The `person:` node a `said_by` names, upserted; returns its id.
    pub async fn upsert_speaker_on(
        c: &impl GenericClient,
        said_by: crate::frontmatter::SaidBy,
    ) -> Result<&'static str> {
        let speaker = match said_by {
            crate::frontmatter::SaidBy::Owner => OWNER_SPEAKER_NODE,
        };
        Self::upsert_node_on(c, speaker, "person", &String::from(said_by), None).await?;
        Ok(speaker)
    }
    pub async fn relate_said_on(
        c: &impl GenericClient,
        said_by: crate::frontmatter::SaidBy,
        doc_path: &str,
        subject: &str,
        predicate: &str,
    ) -> Result<()> {
        let speaker = Self::upsert_speaker_on(c, said_by).await?;
        Self::upsert_edge_on(c, speaker, &doc_node_id(doc_path), "said", None).await?;
        Self::upsert_edge_on(
            c,
            speaker,
            &format!("claim:{subject}:{predicate}"),
            "said",
            None,
        )
        .await
    }

    // ── graph retrieval ───────────────────────────────────────────────────────

    /// Structural neighbors (project/topic) — 1-hop from the chunk's document. Returns labels.
    pub async fn graph_neighbors(&self, chunk_id: &str) -> Result<Vec<String>> {
        let doc_id = doc_node_id(chunk_id);
        let rows = self
            .db()
            .await?
            .query(
                "SELECT n.label FROM edge e JOIN node n ON n.id = e.dst
                 WHERE e.src = $1 AND e.kind IN ('in_project', 'tagged', 'claims');",
                &[&doc_id],
            )
            .await?;
        Ok(rows.into_iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// Semantic neighbors (`uses`/`about`/`claims` edges) — 1-hop from the document. Returns labels.
    pub async fn semantic_neighbors(&self, chunk_id: &str) -> Result<Vec<String>> {
        let doc_id = doc_node_id(chunk_id);
        let rows = self
            .db()
            .await?
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
        let db = self.db().await?;
        Ok(GraphStats {
            documents: pg_count(&db, "SELECT count(*) FROM document;").await?,
            chunks: pg_count(&db, "SELECT count(*) FROM chunk;").await?,
            projects: count_node_kind(&db, "project").await?,
            topics: count_node_kind(&db, "topic").await?,
            claims: count_node_kind(&db, "claim").await?,
            claims_with_doc: pg_count(
                &db,
                "SELECT count(DISTINCT dst) FROM edge WHERE kind = 'claims';",
            )
            .await?,
            decisions: count_node_kind(&db, "decision").await?,
            risks: count_node_kind(&db, "risk").await?,
            edges: pg_count(&db, "SELECT count(*) FROM edge;").await?,
        })
    }

    pub async fn semantic_stats(&self) -> Result<SemanticStats> {
        let db = self.db().await?;
        Ok(SemanticStats {
            tools: count_node_kind(&db, "tool").await?,
            concepts: count_node_kind(&db, "concept").await?,
            uses: count_edge_kind(&db, "uses").await?,
            about: count_edge_kind(&db, "about").await?,
        })
    }

    pub async fn claim_era_counts(&self) -> Result<ClaimEraCounts> {
        let row = self
            .db()
            .await?
            .query_one(
                "SELECT count(*) FILTER (WHERE era = 'anchored'),
                        count(*) FILTER (WHERE era = 'unanchored'),
                        count(*) FILTER (WHERE era = 'pre-anchor'),
                        count(*) FILTER (WHERE length(value) < $1 OR predicate ~* $2)
                   FROM claim
                  WHERE superseded_at IS NULL;",
                &[&INFORMATIVE_VALUE_CHARS, &TAUTOLOGICAL_PREDICATES],
            )
            .await
            .context("claim era counts")?;
        let n = |i: usize| {
            let v: i64 = row.get(i);
            usize::try_from(v).unwrap_or(0)
        };
        Ok(ClaimEraCounts {
            anchored: n(0),
            unanchored: n(1),
            pre_anchor: n(2),
            label_only: n(3),
        })
    }

    pub async fn claim_stale_count(&self) -> Result<usize> {
        let db = self.db().await?;
        pg_count(
            &db,
            "SELECT count(*) FROM claim WHERE stale_at IS NOT NULL;",
        )
        .await
    }

    pub async fn anchor_check_watermark(&self) -> Result<Option<AnchorCheckWatermark>> {
        let row = self
            .db()
            .await?
            .query_opt(
                "SELECT value, value_subject, value_predicate
                   FROM sync_state WHERE key = 'anchor_check_watermark';",
                &[],
            )
            .await
            .context("anchor check watermark")?;
        Ok(row.map(|r| AnchorCheckWatermark {
            valid_from: r.get(0),
            subject: r.get(1),
            predicate: r.get(2),
        }))
    }

    pub async fn set_anchor_check_watermark(&self, watermark: &AnchorCheckWatermark) -> Result<()> {
        self.db()
            .await?
            .execute(
                "INSERT INTO sync_state (key, value, value_subject, value_predicate)
                 VALUES ('anchor_check_watermark', $1, $2, $3)
                 ON CONFLICT (key) DO UPDATE SET
                     value = EXCLUDED.value,
                     value_subject = EXCLUDED.value_subject,
                     value_predicate = EXCLUDED.value_predicate;",
                &[
                    &watermark.valid_from,
                    &watermark.subject,
                    &watermark.predicate,
                ],
            )
            .await
            .context("set anchor check watermark")?;
        Ok(())
    }

    pub async fn claims_due_for_anchor_check(
        &self,
        after: Option<&AnchorCheckWatermark>,
        limit: usize,
    ) -> Result<Vec<AnchorCheckRow>> {
        let limit = i64::try_from(limit).unwrap_or(i64::MAX);
        let (after_at, after_subject, after_predicate) = after.map_or_else(
            || (None, String::new(), String::new()),
            |w| (Some(w.valid_from), w.subject.clone(), w.predicate.clone()),
        );
        let rows = self
            .db()
            .await?
            .query(
                "SELECT subject, predicate, valid_from, anchor, anchor_hash, anchor_symbol
                   FROM claim
                  WHERE anchor_hash IS NOT NULL AND stale_at IS NULL
                    AND ($1::timestamptz IS NULL
                         OR (valid_from, subject, predicate) > ($1, $2, $3))
                  ORDER BY valid_from, subject, predicate
                  LIMIT $4;",
                &[&after_at, &after_subject, &after_predicate, &limit],
            )
            .await
            .context("claims due for anchor check")?;
        Ok(rows.iter().map(anchor_check_row_from).collect())
    }

    pub async fn claims_due_for_anchor_check_head(
        &self,
        watermark: &AnchorCheckWatermark,
        limit: usize,
    ) -> Result<Vec<AnchorCheckRow>> {
        let limit = i64::try_from(limit).unwrap_or(i64::MAX);
        let rows = self
            .db()
            .await?
            .query(
                "SELECT subject, predicate, valid_from, anchor, anchor_hash, anchor_symbol
                   FROM claim
                  WHERE anchor_hash IS NOT NULL AND stale_at IS NULL
                    AND (valid_from, subject, predicate) <= ($1, $2, $3)
                  ORDER BY valid_from, subject, predicate
                  LIMIT $4;",
                &[
                    &watermark.valid_from,
                    &watermark.subject,
                    &watermark.predicate,
                    &limit,
                ],
            )
            .await
            .context("claims due for anchor check (head)")?;
        Ok(rows.iter().map(anchor_check_row_from).collect())
    }

    pub async fn repin_claim(
        &self,
        subject: &str,
        predicate: &str,
        valid_from: SystemTime,
        anchor: &str,
    ) -> Result<()> {
        self.db()
            .await?
            .execute(
                "UPDATE claim SET anchor = $4
                  WHERE subject = $1 AND predicate = $2 AND valid_from = $3 AND stale_at IS NULL;",
                &[&subject, &predicate, &valid_from, &anchor],
            )
            .await
            .context("repin claim anchor")?;
        self.mirror_surreal(&Scope::slot(subject, predicate)).await
    }

    pub async fn mark_claim_stale(
        &self,
        subject: &str,
        predicate: &str,
        valid_from: SystemTime,
        reason: &str,
    ) -> Result<()> {
        self.db()
            .await?
            .execute(
                "UPDATE claim SET stale_at = now(), stale_reason = $4
                  WHERE subject = $1 AND predicate = $2 AND valid_from = $3 AND stale_at IS NULL;",
                &[&subject, &predicate, &valid_from, &reason],
            )
            .await
            .context("mark claim stale")?;
        self.mirror_surreal(&Scope::slot(subject, predicate)).await
    }

    /// Remove orphan semantic nodes — entity nodes not referenced by any edge.
    pub async fn gc_orphans(&self) -> Result<GcStats> {
        let mut gc = GcStats::default();
        let mut gone: Vec<String> = Vec::new();
        for kind in ["tool", "concept"] {
            let removed: Vec<String> = self
                .db()
                .await?
                .query(
                    "DELETE FROM node WHERE kind = $1
                       AND id NOT IN (SELECT src FROM edge UNION SELECT dst FROM edge)
                     RETURNING id;",
                    &[&kind],
                )
                .await?
                .iter()
                .map(|row| row.get(0))
                .collect();
            let c = removed.len();
            gone.extend(removed);
            match kind {
                "tool" => gc.tool = c,
                _ => gc.concept = c,
            }
        }
        let db = self.db().await?;
        let doomed: Vec<String> = db
            .query(
                &format!("SELECT id FROM node WHERE {STALE_CLAIM_MIRROR_NODES}"),
                &[],
            )
            .await?
            .into_iter()
            .map(|row| row.get(0))
            .collect();
        gc.claim_nodes = doomed.len();
        if !doomed.is_empty() {
            let edges = db
                .execute(
                    "DELETE FROM edge WHERE src = ANY($1) OR dst = ANY($1);",
                    &[&doomed],
                )
                .await?;
            db.execute("DELETE FROM node WHERE id = ANY($1);", &[&doomed])
                .await?;
            gc.claim_edges = usize::try_from(edges).unwrap_or(0);
            gone.extend(doomed);
        }
        self.mirror_surreal(&Scope::graph(gone)).await?;
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

fn row_to_hit(r: &tokio_postgres::Row, dist_kind: DistKind) -> Hit {
    Hit {
        id: r.get(0),
        content: r.get(1),
        origin: r.get(2),
        project: r.get(3),
        source_path: r.get(4),
        dist: r.get(5),
        dist_kind,
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use super::{DistKind, NOT_USER_MEMORY_RE};

    /// A pair naming the same path twice replaces nothing: it is not written, and its path is
    /// returned with the skipped ones so the caller sees the pair was seen but produced no edge.
    #[test]
    fn an_equal_paths_supersedes_pair_is_skipped_and_counted() {
        let pairs = [
            ["/w/new.md".to_owned(), "/w/old.md".to_owned()],
            ["/w/same.md".to_owned(), "/w/same.md".to_owned()],
        ];
        let (valid, skipped) = super::split_supersedes(&pairs);
        assert_eq!(valid.len(), 1, "the equal-paths pair is not written");
        assert_eq!(valid[0][1], "/w/old.md");
        assert_eq!(skipped, vec!["/w/same.md"]);
    }

    #[test]
    fn split_supersedes_passes_distinct_paths_through() {
        let pairs = [
            ["/w/b.md".to_owned(), "/w/a.md".to_owned()],
            ["/w/c.md".to_owned(), "/w/b.md".to_owned()],
        ];
        let (valid, skipped) = super::split_supersedes(&pairs);
        assert_eq!(valid.len(), 2);
        assert_eq!(skipped, Vec::<&str>::new());
    }

    /// The recency and claim surfaces feed the briefing, and the briefing writes notes into the
    /// same vault — so without this exclusion it reads yesterday's summary and restates it as
    /// today's work. Measured 2026-09-03: `omb-helper` appeared in 18 daily-brief notes and zero
    /// real session notes, entering on 08-01 and repeating every morning since.
    ///
    /// The other half matters as much: this must exclude ONLY generated output. Excluding a real
    /// note would silently delete memory from every surface that reads recency, and nothing else
    /// in the system would report it — 59 of 1,581 documents match, and all 59 are briefings.
    #[test]
    fn the_exclusion_covers_generated_output_and_nothing_else() {
        let re = regex::Regex::new(NOT_USER_MEMORY_RE).unwrap();

        for generated in [
            "/vault/wiki/daily-brief-2026-09-03.md",
            "/vault/wiki/weekly-brief-2026-08-31.md",
            "/vault/wiki/eval-fixture-01.md",
            "daily-brief-2026-01-01.md",
        ] {
            assert!(re.is_match(generated), "must be excluded: {generated}");
        }

        for real in [
            "/vault/wiki/wiki-0295.md",
            "/vault/wiki/wiki-1526.md",
            // Names that merely *contain* the markers are somebody's actual note. Anchoring the
            // pattern at a path segment is what keeps this from eating them.
            "/vault/wiki/my-daily-brief-notes.md",
            "/vault/wiki/notes-on-eval-design.md",
            "/vault/raw/2026-09-03-session.md",
        ] {
            assert!(!re.is_match(real), "must NOT be excluded: {real}");
        }
    }

    /// `agents/shared/recall_core.py` string-matches this exact JSON contract to decide whether a
    /// `dist` is a comparable cosine distance — a silent rename here would break that filter without
    /// any Rust-side signal that something changed.
    #[test]
    fn dist_kind_serializes_to_the_strings_recall_core_matches_on() {
        assert_eq!(
            serde_json::to_string(&DistKind::VectorCosine).unwrap(),
            "\"vector_cosine\""
        );
        assert_eq!(DistKind::VectorCosine.as_str(), "vector_cosine");
        assert_eq!(
            serde_json::to_string(&DistKind::TextRank).unwrap(),
            "\"text_rank\""
        );
        assert_eq!(DistKind::TextRank.as_str(), "text_rank");
    }
}
