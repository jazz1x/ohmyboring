//! SurrealDB implementation of five of the `VectorIndex` queries, plus the one-shot fill that
//! copies the vectors out of pgvector.
//!
//! The ranking tables (`chunk`, `chunk_p_*`, `claim`) hold the row's key, its embedding, and the
//! columns the pgvector SQL filters on. On `backend=surreal` the answer is read back from Surreal's
//! base rows by key (`chunk`, `claim_row`, `document`) and re-checked with the pgvector path's
//! predicates, so the answer carries the same text, origin and anchor the pgvector path returns
//! and a claim written only into Surreal (C-SURALL2 Z1) is still found. A key a ranking table
//! returns that the base table lacks is an error naming the drift, never a silently shorter answer.
//!
//! Wire: SurrealDB's HTTP JSON-RPC (`POST /rpc`, method `query`) with bind variables, over the
//! `reqwest` the engine already carries. The server is pinned in compose to
//! `surrealdb/surrealdb:v3.3.5@sha256:3db515b8…` (v3.3.4 lost recall after delete/overwrite churn; v3.3.5 fixes part of it).
//!
//! Index and query parameters are stage-0's: HNSW `DIMENSION <embed_dim> DIST COSINE TYPE F32 EFC 64
//! M 16`; query EF is `vector.surreal.ef` (default 2000: over 5 fresh fills it is the only EF whose worst recall run reaches pgvector's 21/22 — EF 400 min 14, EF 800 min 18; stage 0 measured 40 = pgvector's ef_search).
//!
//! Filter columns, one Surreal column per pgvector filter:
//!
//! | pgvector filter                                      | Surreal                              |
//! |------------------------------------------------------|--------------------------------------|
//! | vector_search_filtered `c.project`                   | `chunk.project`                      |
//! | vector_search_filtered `d.updated_at`                | `chunk.doc_updated_us`               |
//! | current_claims `c.superseded_at IS NULL`             | table membership (open claims only)  |
//! | current_claims `c.embedding IS NOT NULL`             | table membership                     |
//! | current_claims `d.origin`                            | `claim.origin`                       |
//! | current_claims `d.project`                           | `claim.project`                      |
//! | current_claims `c.kind`                              | `claim.kind`                         |
//! | current_claims `d.source_path !~ NOT_USER_MEMORY_RE` | `claim.source_path`                  |
//! | current_claims `split_part(c.anchor, ':', 2) LIKE`   | `claim.anchor`                       |
//! | current_claims `c.stale_at IS NULL`                  | `claim.stale_us`                     |

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use anyhow::{Context, Result, anyhow, ensure};
use pgvector::Vector;
use serde::Deserialize;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::sync::RwLock;
use tokio_postgres::Row;

use super::{
    AnchoredClaim, DistKind, Hit, INFORMATIVE_VALUE_CHARS, NOT_USER_MEMORY_RE, Recurrence,
    RegisterRow, RegisterRows, Store, TAUTOLOGICAL_PREDICATES,
};
use crate::config::SurrealSettings;
use crate::frontmatter::Claim;
use crate::vector::VectorIndex;

/// Rows per fill request: 1024 floats are ~11 KB of JSON, and the server refuses a request body over
/// 1 MiB (HTTP 413).
const FILL_BATCH: usize = 40;
/// Per-request ceiling for the I/O boundary; an HNSW insert batch is the slowest request.
const REQUEST_TIMEOUT: Duration = Duration::from_mins(5);
/// Ceiling and interval for waiting on a `DEFINE INDEX ... CONCURRENTLY` build (27,722 claims
/// took 4.3s); the wait is a failure past the ceiling, never silent.
const INDEX_BUILD_WAIT: Duration = Duration::from_mins(10);
const INDEX_BUILD_POLL: Duration = Duration::from_millis(500);
const KEY_SEP: char = '\u{1f}';
/// Keys per Surreal answer when a whole table's keys are read (`table_keys`).
const KEY_PAGE: usize = 20_000;
/// Notes per Surreal read when `claim_row_diff` looks up their `document` rows.
const CATCH_UP_BATCH: usize = 500;
/// Prefix of a per-project chunk table. The global `chunk` table keeps every chunk; a project table
/// keeps that project's chunks only, so a project-filtered KNN scans the project's graph instead of
/// filtering inside the global one (measured p95 120-150 ms for the in-KNN filter).
const PROJECT_TABLE_PREFIX: &str = "chunk_p_";

/// What the server refused that a caller acts on, parsed once from the reply text in `rejection`
/// (the server gives no code for it).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum Refusal {
    /// A table's document-ID space is still being reclaimed after a mass delete or an index
    /// removal; `DEFINE INDEX` is refused until the cleanup ends, and the server says to retry.
    #[error("surreal: {0}")]
    Reclaiming(String),
}

/// The table has no HNSW index and the wait for the server to let one be defined ran out.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error(
    "{table} 에 HNSW 인덱스를 못 만듦 — 문서 ID 공간 정리가 {waited_s}s 안에 안 끝남 (인덱스 없음)"
)]
pub struct ReclaimTimeout {
    pub table: String,
    pub waited_s: u64,
}

/// The server's own sentence for `Refusal::Reclaiming` (v3.3.5); anything else stays a plain error.
const RECLAIMING: &str = "is still being reclaimed";

fn rejection(statement: &str, message: &str) -> anyhow::Error {
    if message.contains(RECLAIMING) {
        anyhow::Error::new(Refusal::Reclaiming(message.to_owned()))
    } else {
        anyhow!("{statement}{message}")
    }
}

/// Why a project slug cannot name a table. The name is spliced into SurrealQL (a table name cannot
/// be bound), so it is parsed once here and never mapped: a slug outside `[a-z0-9_-]` has no
/// project table, and every caller says so.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ProjectTableError {
    #[error("project 가 비어 있어 프로젝트 테이블이 없음")]
    Empty,
    #[error("project {slug:?} 의 {bad:?} 는 테이블 이름에 못 씀 (a-z0-9_- 만)")]
    BadChar { slug: String, bad: char },
}

/// A project's chunk table, `chunk_p_<slug>`, slug in `[a-z0-9_-]+`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct ProjectTable {
    slug: String,
    name: String,
}

impl ProjectTable {
    /// # Errors
    /// `Empty` for an empty slug, `BadChar` naming the first character outside `[a-z0-9_-]`.
    pub fn parse(slug: &str) -> Result<Self, ProjectTableError> {
        if slug.is_empty() {
            return Err(ProjectTableError::Empty);
        }
        match slug
            .chars()
            .find(|c| !(c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(*c, '_' | '-')))
        {
            Some(bad) => Err(ProjectTableError::BadChar {
                slug: slug.to_owned(),
                bad,
            }),
            None => Ok(Self {
                slug: slug.to_owned(),
                name: format!("{PROJECT_TABLE_PREFIX}{slug}"),
            }),
        }
    }

    #[must_use]
    pub fn name(&self) -> &str {
        &self.name
    }

    #[must_use]
    pub fn slug(&self) -> &str {
        &self.slug
    }
}

/// `chunk` rows grouped by project table, plus the projects that cannot have one (each gets one
/// `[vector]` line: its chunks live in the global table only).
struct Placed {
    by_table: std::collections::BTreeMap<ProjectTable, Vec<(String, String)>>,
    rejected: std::collections::BTreeMap<String, ProjectTableError>,
}

impl Placed {
    /// `(project, key, literal)` rows.
    fn of(rows: Vec<(String, String, String)>) -> Self {
        let mut placed = Self {
            by_table: std::collections::BTreeMap::new(),
            rejected: std::collections::BTreeMap::new(),
        };
        for (project, key, literal) in rows {
            match ProjectTable::parse(&project) {
                Ok(table) => placed
                    .by_table
                    .entry(table)
                    .or_default()
                    .push((key, literal)),
                Err(e) => {
                    placed.rejected.insert(project, e);
                }
            }
        }
        placed
    }

    fn report(&self) {
        for e in self.rejected.values() {
            eprintln!("[vector] surreal: {e} — chunk 는 global 테이블에만 씀");
        }
    }
}

/// What one swap-in rebuild did to one table's HNSW index. Failure is a value here, not a
/// swallowed error: the old index is still in place and every caller prints the line and fails.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Swap {
    /// No row changed since the last swap (or fill): nothing was defined or removed.
    Skipped { table: String },
    /// `new` was defined, reported ready (`status` quotes the server's `building` object), and
    /// only then was `old` removed.
    Swapped {
        table: String,
        old: String,
        new: String,
        changed: u64,
        status: String,
    },
    /// The swap stopped; `old` is untouched.
    Failed { table: String, error: String },
}

impl Swap {
    #[must_use]
    pub fn failed(&self) -> bool {
        matches!(self, Self::Failed { .. })
    }

    #[must_use]
    pub fn line(&self) -> String {
        match self {
            Self::Skipped { table } => format!("{table}: skipped (바뀐 행 0)"),
            Self::Swapped {
                table,
                old,
                new,
                changed,
                status,
            } => format!("{table}: swapped {old} -> {new} (바뀐 행 {changed}, {status})"),
            Self::Failed { table, error } => {
                format!("{table}: FAILED — {error}")
            }
        }
    }
}

/// Which table a chunk KNN reads: the project's own table when it exists, else the global one.
enum ChunkSource {
    Global,
    Project(ProjectTable),
}

impl ChunkSource {
    /// The table as a SurrealQL identifier, ready to splice.
    fn table(&self) -> String {
        match self {
            Self::Global => quote_ident("chunk"),
            Self::Project(t) => quote_ident(t.name()),
        }
    }
}

/// A table or index name as a backtick-quoted SurrealQL identifier: a `-` in a project slug is not
/// an identifier character bare (`chunk_p_oh-my-boring` reads as a subtraction). The name only ever
/// holds `[a-z0-9_-]` (`ProjectTable::parse`), so nothing can close the quote.
fn quote_ident(name: &str) -> String {
    format!("`{name}`")
}

/// `chunk_embedding` -> `chunk_embedding_g1` -> `chunk_embedding_g2`: the next name is derived from
/// the live index name, so a swap never needs a stored generation number.
fn next_generation(index: &str) -> String {
    let (base, generation) = index
        .rsplit_once("_g")
        .and_then(|(base, n)| n.parse::<u64>().ok().map(|n| (base, n)))
        .unwrap_or((index, 0));
    format!("{base}_g{}", generation + 1)
}

/// Full-text analyzer for `chunk.content`. pg indexes `to_tsvector('simple', content)`
/// (baseline.sql:127): it splits on blanks and punctuation, lowercases, and does no stemming and
/// no Hangul segmentation. This does the same and nothing more — `segment korean` would add a
/// morphological split pg does not do, and whether our image has the `cjk` feature is unverified
/// (wiki-00000012804). The built-in tokenizers cannot reproduce pg's compound tokens (url, email,
/// host, file path, version, float, hyphenated word); `retrieve` never queries them (see
/// `text_terms`), and the token comparison counts what differs.
const TEXT_ANALYZER_DDL: &str =
    "DEFINE ANALYZER OVERWRITE omb_simple TOKENIZERS blank, punct FILTERS lowercase;";
/// BM25 index on the global chunk table. Defined AFTER the bulk load in `fill_from` and by
/// `ensure_text_index` for a copy filled before this index existed. Unlike HNSW it is an exact
/// inverted index kept in step on every write, so it has no swap generation and `swap_table`
/// never touches it (`hnsw_indexes` filters on ` HNSW `).
const TEXT_INDEX_DDL: &str =
    "DEFINE INDEX chunk_content_ft ON chunk FIELDS content FULLTEXT ANALYZER omb_simple BM25";

/// The words a Surreal full-text query is made of: the alphanumeric runs of `query`, blank
/// separated. pg's `plainto_tsquery('simple', …)` drops punctuation, while Surreal's `punct`
/// tokenizer keeps each punctuation mark as a token that a document must contain to match, so a
/// query like `hello, world` would need a comma. Empty means pg's empty query: no rows.
fn text_terms(query: &str) -> String {
    query
        .split(|c: char| !c.is_alphanumeric())
        .filter(|w| !w.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

/// DDL of one chunk table (global or per project): same fields, same HNSW definition.
fn chunk_ddl(table: &str, dim: usize) -> String {
    let t = quote_ident(table);
    let src_idx = quote_ident(&format!("{table}_source_path"));
    let emb_idx = quote_ident(&format!("{table}_embedding"));
    format!(
        "DEFINE TABLE {t} SCHEMAFULL;
         DEFINE FIELD cid ON {t} TYPE string;
         DEFINE FIELD source_path ON {t} TYPE string;
         DEFINE FIELD project ON {t} TYPE string;
         DEFINE FIELD doc_updated_us ON {t} TYPE int;
         DEFINE FIELD content ON {t} TYPE string;
         DEFINE FIELD origin ON {t} TYPE string;
         DEFINE FIELD embedding ON {t} TYPE array<float>;
         DEFINE INDEX {src_idx} ON {t} FIELDS source_path;
         DEFINE INDEX {emb_idx} ON {t} FIELDS embedding
             HNSW DIMENSION {dim} DIST COSINE TYPE F32 EFC 64 M 16;"
    )
}

/// Connection to the SurrealDB server.
#[derive(Debug, Clone)]
pub struct SurrealClient {
    http: reqwest::Client,
    settings: SurrealSettings,
    token: Arc<RwLock<Option<Token>>>,
    tx_retries: u32,
    tx_stats: Arc<claim_tx::TxStats>,
    claim_via: Arc<tokio::sync::OnceCell<()>>,
}

/// The `/signin` token, shared by every clone. Hand-written `Debug`: it must never reach a log.
struct Token(String);

impl std::fmt::Debug for Token {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Token(..)")
    }
}

/// What one `/rpc` call came back as. The server's token lifetime is its own setting and the root
/// default is not documented, so a token going stale mid-run is a real state, not a bug to hide.
enum Sent {
    Rejected(String),
    Answered {
        status: reqwest::StatusCode,
        text: String,
    },
}

#[derive(Deserialize)]
struct SigninReply {
    token: String,
}

#[derive(Deserialize)]
struct RpcReply {
    result: Option<Vec<RpcStatement>>,
    error: Option<RpcError>,
}

#[derive(Deserialize)]
struct RpcStatement {
    status: String,
    result: Value,
}

#[derive(Deserialize)]
struct RpcError {
    message: String,
}

impl SurrealClient {
    pub(super) fn namespace(&self) -> &str {
        self.settings.namespace.as_str()
    }

    pub(super) fn database(&self) -> &str {
        self.settings.database.as_str()
    }

    /// Query-time HNSW candidate list size, from `vector.surreal.ef`.
    fn ef(&self) -> usize {
        self.settings.ef.get()
    }

    pub fn new(settings: SurrealSettings) -> Result<Self> {
        let http = reqwest::Client::builder()
            .timeout(REQUEST_TIMEOUT)
            .build()
            .context("build surreal http client")?;
        Ok(Self {
            http,
            settings,
            token: Arc::new(RwLock::new(None)),
            tx_retries: claim_tx::TX_RETRIES,
            tx_stats: Arc::default(),
            claim_via: Arc::default(),
        })
    }

    /// `POST /signin` as the root user; the token goes into the shared slot. Basic auth on every
    /// request costs the server a password check each time (~10-13 ms vs ~0.2 ms for a Bearer).
    async fn sign_in(&self) -> Result<String> {
        let response = self
            .http
            .post(format!(
                "{}/signin",
                self.settings.url.trim_end_matches('/')
            ))
            .header("Accept", "application/json")
            .json(&json!({"user": self.settings.user, "pass": self.settings.password}))
            .send()
            .await
            .context("surreal signin request")?;
        let status = response.status();
        let text = response.text().await.context("surreal signin body")?;
        ensure!(
            status.is_success(),
            "surreal signin (http {status}): {text}"
        );
        let reply: SigninReply = serde_json::from_str(&text)
            .with_context(|| format!("surreal signin reply (http {status}) has no token"))?;
        *self.token.write().await = Some(Token(reply.token.clone()));
        Ok(reply.token)
    }

    /// Lets a test stand in for a token the server no longer accepts; expiry cannot be waited for.
    #[doc(hidden)]
    pub async fn replace_token(&self, token: &str) {
        *self.token.write().await = Some(Token(token.to_owned()));
    }

    async fn current_token(&self) -> Result<String> {
        let held = self.token.read().await.as_ref().map(|t| t.0.clone());
        match held {
            Some(t) => Ok(t),
            None => self.sign_in().await,
        }
    }

    async fn send(&self, token: &str, body: &Value) -> Result<Sent> {
        let response = self
            .http
            .post(format!("{}/rpc", self.settings.url.trim_end_matches('/')))
            .bearer_auth(token)
            .header("Surreal-NS", self.settings.namespace.as_str())
            .header("Surreal-DB", self.settings.database.as_str())
            .header("Accept", "application/json")
            .json(body)
            .send()
            .await
            .context("surreal request")?;
        let status = response.status();
        let text = response.text().await.context("surreal response body")?;
        match status {
            reqwest::StatusCode::UNAUTHORIZED => Ok(Sent::Rejected(text)),
            _ => Ok(Sent::Answered { status, text }),
        }
    }

    /// Only an auth rejection signs in again, once, and resends only this call; a second rejection
    /// fails with the server's message.
    async fn send_authed(&self, body: &Value) -> Result<(reqwest::StatusCode, String)> {
        let token = self.current_token().await?;
        let sent = match self.send(&token, body).await? {
            Sent::Rejected(_) => {
                let fresh = self.sign_in().await?;
                self.send(&fresh, body).await?
            }
            answered @ Sent::Answered { .. } => answered,
        };
        match sent {
            Sent::Rejected(message) => Err(anyhow!("surreal: {}", message.trim())),
            Sent::Answered { status, text } => Ok((status, text)),
        }
    }

    /// Run SurrealQL; one JSON value per statement. Any statement that did not answer `OK` fails
    /// the whole call with the server's message.
    ///
    /// Values go into the query text as literals (`text`, `floats`), never as RPC bind variables:
    /// the server reads a JSON string that looks like a record id (`mimir:satellite`, a claim
    /// anchor), a datetime or a uuid as that type, which broke the first fill on the copy.
    pub async fn run(&self, sql: &str) -> Result<Vec<Value>> {
        let body = json!({"id": 1, "method": "query", "params": [sql]});
        let (status, text) = self.send_authed(&body).await?;
        let reply: RpcReply = serde_json::from_str(&text)
            .with_context(|| format!("surreal reply (http {status}) is not an rpc reply"))?;
        match (reply.error, reply.result) {
            (Some(e), _) => Err(rejection("surreal: ", &e.message)),
            (None, Some(statements)) => statements
                .into_iter()
                .map(|s| {
                    if s.status == "OK" {
                        Ok(s.result)
                    } else {
                        let message = s
                            .result
                            .as_str()
                            .map_or_else(|| s.result.to_string(), str::to_owned);
                        Err(rejection(
                            &format!("surreal statement {}: ", s.status),
                            &message,
                        ))
                    }
                })
                .collect(),
            (None, None) => Err(anyhow!(
                "surreal reply (http {status}) has neither result nor error"
            )),
        }
    }

    pub(super) async fn rows<T: DeserializeOwned>(&self, sql: &str) -> Result<Vec<Vec<T>>> {
        self.run(sql)
            .await?
            .into_iter()
            .map(|v| serde_json::from_value(v).context("surreal rows"))
            .collect()
    }
}

/// A SurrealQL string literal: quoted, with the characters a string cannot hold raw escaped.
fn text(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if u32::from(c) < 0x20 => out.extend(format!("\\u{:04x}", u32::from(c)).chars()),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

pub(super) fn texts(items: &[String]) -> String {
    let inner: Vec<String> = items.iter().map(|s| text(s)).collect();
    format!("[{}]", inner.join(","))
}

/// A SurrealQL array literal of floats. `f32` Display is the shortest text that reads back to the
/// same value, which keeps a 1024-float vector near 11 KB.
fn floats(v: &[f32]) -> String {
    let inner: Vec<String> = v.iter().map(ToString::to_string).collect();
    format!("[{}]", inner.join(","))
}

#[derive(Deserialize)]
struct NearChunk {
    cid: String,
    d: f64,
}

#[derive(Deserialize)]
struct NearClaim {
    subject: String,
    predicate: String,
    valid_from_us: i64,
}

#[derive(Deserialize)]
struct NearDoc {
    source_path: String,
    d: f64,
}

/// The surreal side of the route: on `backend=surreal` every answer comes out of Surreal. The
/// ranking tables (`chunk`, `chunk_p_*`, `claim`) name the keys; the rows are re-read from the
/// base tables (`chunk`, `claim_row`, `document`) before they are returned.
pub(super) struct SurrealView<'a> {
    pub(super) client: &'a SurrealClient,
}

fn drift(what: &str, table: &str, missing: &[String]) -> anyhow::Error {
    anyhow!(
        "surreal 순위 표가 가리킨 {what} {n}개가 {table} 표에 없음 (두 표가 갈림, 예: {first}) — vector-fill 을 다시 돌릴 것",
        n = missing.len(),
        first = missing.first().map_or("", String::as_str)
    )
}

/// A dropped row is a visible event, not a silent filter: the answer is shorter than asked.
fn report_dropped(what: &str, dropped: usize) {
    if dropped > 0 {
        eprintln!("[vector] surreal: 재확인이 {what} {dropped}개를 뺌 (순위 표 행이 낡음)");
    }
}

/// One chunk as the base `chunk` table holds it now, with the `updated_us` of its document
/// (NONE when the document row is gone).
#[derive(Deserialize)]
struct BaseChunk {
    cid: String,
    content: String,
    origin: String,
    project: String,
    source_path: String,
    #[serde(default)]
    doc_us: Option<i64>,
}

/// One claim as `claim_row` and its live `document` hold it now.
#[derive(Deserialize)]
struct BaseClaim {
    subject: String,
    predicate: String,
    value: String,
    kind: String,
    confidence: String,
    #[serde(default)]
    anchor: Option<String>,
    era: String,
    #[serde(default)]
    stale_us: Option<i64>,
    #[serde(default)]
    stale_reason: Option<String>,
    valid_from_us: i64,
    #[serde(default)]
    superseded_us: Option<i64>,
    generated: bool,
    #[serde(default)]
    doc_origin: Option<String>,
    #[serde(default)]
    doc_project: Option<String>,
}

pub(super) fn micros_to_time(us: i64, what: &str) -> Result<SystemTime> {
    let micros = u64::try_from(us).with_context(|| format!("{what} {us} is before 1970"))?;
    Ok(SystemTime::UNIX_EPOCH + Duration::from_micros(micros))
}

/// The pg claim predicate `split_part(c.anchor, ':', 2) LIKE <prefix>%`: no anchor never matches.
fn anchor_under(anchor: Option<&str>, prefix: &str) -> bool {
    anchor.is_some_and(|a| a.split(':').nth(1).unwrap_or("").starts_with(prefix))
}

impl SurrealView<'_> {
    /// A ranking table can lag the base rows (a project table rebuilt later than `chunk`, a swap
    /// not yet done), so every key the ranking returns is re-checked here against the base
    /// `chunk` row and the live `document` row with the predicates pg's path applies (the #121
    /// origin boundary: an origin or project changed after the ranking row was written must not
    /// leak through it). A row that fails is dropped, not replaced — the answer may come back
    /// shorter than `k`. A key the base table lacks is drift and an error.
    async fn hydrate_hits(
        &self,
        near: Vec<NearChunk>,
        project: Option<&str>,
        since_hours: Option<i32>,
        dist_kind: DistKind,
    ) -> Result<Vec<Hit>> {
        if near.is_empty() {
            return Ok(Vec::new());
        }
        let records: Vec<String> = near
            .iter()
            .map(|n| format!("type::record('chunk', {})", text(&n.cid)))
            .collect();
        let sql = format!(
            "SELECT cid, content, origin, project, source_path,
                    (SELECT VALUE updated_us FROM ONLY type::record('document', $parent.source_path)) AS doc_us
               FROM [{}];",
            records.join(",")
        );
        let rows: Vec<BaseChunk> = first_statement(&sql, self.client.rows(&sql).await?)?;
        let cutoff = since_hours
            .map(|h| now_micros().map(|now| now - i64::from(h) * 3_600_000_000))
            .transpose()?;
        let mut by_id: HashMap<String, BaseChunk> =
            rows.into_iter().map(|r| (r.cid.clone(), r)).collect();
        let mut missing = Vec::new();
        let mut dropped = 0_usize;
        let mut hits = Vec::with_capacity(near.len());
        for n in near {
            let Some(row) = by_id.remove(&n.cid) else {
                missing.push(n.cid);
                continue;
            };
            let in_project = project.is_none_or(|p| row.project == p);
            let in_window = row.doc_us.is_some_and(|us| cutoff.is_none_or(|c| us >= c));
            if in_project && in_window {
                hits.push(Hit {
                    id: n.cid,
                    content: row.content,
                    origin: row.origin,
                    project: row.project,
                    source_path: row.source_path,
                    #[allow(clippy::cast_possible_truncation)]
                    dist: n.d as f32,
                    dist_kind,
                });
            } else {
                dropped += 1;
            }
        }
        ensure!(missing.is_empty(), drift("chunk", "chunk", &missing));
        report_dropped("chunk", dropped);
        Ok(hits)
    }

    async fn chunk_knn(
        &self,
        sql: &str,
        k: usize,
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Result<Vec<Hit>> {
        let mut near: Vec<NearChunk> = self
            .client
            .rows(sql)
            .await?
            .into_iter()
            .next()
            .unwrap_or_default();
        near.truncate(k);
        self.hydrate_hits(near, project, since_hours, DistKind::VectorCosine)
            .await
    }

    /// The text stage of `retrieve` on `backend=surreal`: BM25 over `chunk.content`, same filters
    /// as pg's `text_search_filtered`, hydrated from pg like the vector hits. `dist` is the BM25
    /// score (higher is better), `DistKind::TextRank` like pg's `ts_rank`.
    pub(super) async fn text_search_filtered(
        &self,
        query: &str,
        k: usize,
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Result<Vec<Hit>> {
        let terms = text_terms(query);
        if terms.is_empty() {
            return Ok(Vec::new());
        }
        let mut conds = vec![format!("content @0@ {}", text(&terms))];
        if let Some(p) = project {
            conds.push(format!("project = {}", text(p)));
        }
        if let Some(hours) = since_hours {
            conds.push(format!(
                "doc_updated_us >= {}",
                now_micros()? - i64::from(hours) * 3_600_000_000
            ));
        }
        let sql = format!(
            "SELECT cid, search::score(0) AS d FROM chunk WHERE {} ORDER BY d DESC, cid LIMIT {k};",
            conds.join(" AND ")
        );
        let near: Vec<NearChunk> = self
            .client
            .rows(&sql)
            .await?
            .into_iter()
            .next()
            .unwrap_or_default();
        self.hydrate_hits(near, project, since_hours, DistKind::TextRank)
            .await
    }
}

impl VectorIndex for SurrealView<'_> {
    async fn semantic_related_docs(
        &self,
        source_path: &str,
        limit: i64,
        max_dist: f64,
    ) -> Result<Vec<String>> {
        let sources: Vec<Vec<f32>> = self
            .client
            .rows::<Vec<f32>>(&format!(
                "SELECT VALUE embedding FROM chunk WHERE source_path = {};",
                text(source_path)
            ))
            .await?
            .into_iter()
            .next()
            .unwrap_or_default();
        // One nearest-neighbour query per chunk of the source document, each excluding that
        // document; a doc's distance is its closest chunk to any source chunk (pgvector: MIN over
        // the cross product).
        let ef = self.client.ef();
        let sql: String = sources
            .iter()
            .map(|emb| {
                format!(
                    "SELECT source_path, vector::distance::knn() AS d FROM chunk \
                     WHERE embedding <|{ef},{ef}|> {} AND source_path != {};",
                    floats(emb),
                    text(source_path)
                )
            })
            .collect::<Vec<_>>()
            .concat();
        let per_chunk: Vec<Vec<NearDoc>> = if sources.is_empty() {
            Vec::new()
        } else {
            self.client.rows(&sql).await?
        };
        let mut best: HashMap<String, f64> = HashMap::new();
        for doc in per_chunk.into_iter().flatten() {
            let entry = best.entry(doc.source_path).or_insert(f64::INFINITY);
            *entry = entry.min(doc.d);
        }
        let mut within: Vec<(String, f64)> =
            best.into_iter().filter(|(_, d)| *d <= max_dist).collect();
        within.sort_by(|a, b| a.1.total_cmp(&b.1).then_with(|| a.0.cmp(&b.0)));
        within.truncate(usize::try_from(limit).unwrap_or(0));
        Ok(within.into_iter().map(|(p, _)| p).collect())
    }

    async fn recurrences(
        &self,
        days: i64,
        project: Option<&str>,
        limit: i64,
    ) -> Result<Vec<Recurrence>> {
        let claims = self.recurrence_candidates().await?;
        recurrences::pair(&claims, SystemTime::now(), days, project, limit)
            .context("recurrences tautology pattern")
    }

    #[allow(clippy::too_many_lines)] // Surreal filter, pg re-check, drift: one linear read path
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
        let want = usize::try_from(k).unwrap_or(0);
        let ef = self.client.ef();
        let mut conds = vec![format!(
            "embedding <|{},{ef}|> {}",
            want.max(1),
            floats(query_emb)
        )];
        if !exclude_origins.is_empty() {
            conds.push(format!("!(origin IN {})", texts(exclude_origins)));
        }
        if let Some(p) = project {
            conds.push(format!("project = {}", text(p)));
        }
        if let Some(ks) = kinds {
            conds.push(format!("kind IN {}", texts(ks)));
        }
        conds.push(format!(
            "!string::matches(source_path, <regex> {})",
            text(NOT_USER_MEMORY_RE)
        ));
        if let Some(prefix) = anchor_path {
            conds.push(format!(
                "anchor != NONE AND string::starts_with(string::split(anchor, ':')[1] ?? '', {})",
                text(prefix)
            ));
        }
        if !include_stale {
            conds.push("stale_us = NONE".into());
        }
        let sql = format!(
            "SELECT subject, predicate, valid_from_us, vector::distance::knn() AS d FROM claim \
             WHERE {} ORDER BY d ASC;",
            conds.join(" AND ")
        );
        let mut near: Vec<NearClaim> = self
            .client
            .rows(&sql)
            .await?
            .into_iter()
            .next()
            .unwrap_or_default();
        near.truncate(want);
        if near.is_empty() {
            return Ok(Vec::new());
        }
        let records: Vec<String> = near
            .iter()
            .map(|n| {
                let key = claim_key(&n.subject, &n.predicate, n.valid_from_us);
                format!("type::record('claim_row', {})", text(&key))
            })
            .collect();
        let sql = format!(
            "SELECT subject, predicate, value, kind, confidence, anchor, era, stale_us,
                    stale_reason, valid_from_us, superseded_us,
                    string::matches(source_path, <regex> {re}) AS generated,
                    (SELECT VALUE origin FROM ONLY type::record('document', $parent.source_path)) AS doc_origin,
                    (SELECT VALUE project FROM ONLY type::record('document', $parent.source_path)) AS doc_project
               FROM [{records}];",
            re = text(NOT_USER_MEMORY_RE),
            records = records.join(",")
        );
        let rows: Vec<BaseClaim> = first_statement(&sql, self.client.rows(&sql).await?)?;
        // Same re-check as `hydrate_hits`: the `claim` ranking row can lag `claim_row` and
        // `document`, which are the base rows.
        let mut by_key: HashMap<(String, String, i64), BaseClaim> = rows
            .into_iter()
            .map(|r| ((r.subject.clone(), r.predicate.clone(), r.valid_from_us), r))
            .collect();
        let mut missing = Vec::new();
        let mut dropped = 0_usize;
        let mut out = Vec::with_capacity(near.len());
        for n in near {
            let key = (n.subject, n.predicate, n.valid_from_us);
            let Some(row) = by_key.remove(&key) else {
                missing.push(format!("{}:{}", key.0, key.1));
                continue;
            };
            let kept = row.superseded_us.is_none()
                && row
                    .doc_origin
                    .as_ref()
                    .is_some_and(|o| !exclude_origins.contains(o))
                && project.is_none_or(|p| row.doc_project.as_deref() == Some(p))
                && kinds.is_none_or(|ks| ks.contains(&row.kind))
                && !row.generated
                && anchor_path.is_none_or(|prefix| anchor_under(row.anchor.as_deref(), prefix))
                && (include_stale || row.stale_us.is_none());
            if !kept {
                dropped += 1;
                continue;
            }
            let stale_at = row
                .stale_us
                .map(|us| micros_to_time(us, "claim_row stale_us"))
                .transpose()?;
            out.push(AnchoredClaim {
                claim: Claim {
                    subject: row.subject,
                    predicate: row.predicate,
                    value: row.value,
                    kind: row.kind,
                    confidence: row.confidence,
                    said_by: None,
                },
                anchor: row.anchor,
                era: row.era,
                stale_at,
                stale_reason: row.stale_reason,
            });
        }
        ensure!(missing.is_empty(), drift("claim", "claim_row", &missing));
        report_dropped("claim", dropped);
        Ok(out)
    }

    async fn vector_search(&self, vec: &[f32], k: usize) -> Result<Vec<Hit>> {
        let ef = self.client.ef();
        let sql = format!(
            "SELECT cid, vector::distance::knn() AS d FROM chunk \
             WHERE embedding <|{},{ef}|> {} ORDER BY d ASC;",
            k.max(1),
            floats(vec)
        );
        self.chunk_knn(&sql, k, None, None).await
    }

    async fn vector_search_filtered(
        &self,
        vec: &[f32],
        k: usize,
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Result<Vec<Hit>> {
        let ef = self.client.ef();
        let mut conds = vec![format!("embedding <|{},{ef}|> {}", k.max(1), floats(vec))];
        let source = self.client.chunk_source(project).await?;
        if let (ChunkSource::Global, Some(p)) = (&source, project) {
            conds.push(format!("project = {}", text(p)));
        }
        if let Some(hours) = since_hours {
            conds.push(format!(
                "doc_updated_us >= {}",
                now_micros()? - i64::from(hours) * 3_600_000_000
            ));
        }
        let sql = format!(
            "SELECT cid, vector::distance::knn() AS d FROM {} WHERE {} ORDER BY d ASC;",
            source.table(),
            conds.join(" AND ")
        );
        self.chunk_knn(&sql, k, project, since_hours).await
    }

    async fn nearest_document(&self, vec: &[f32], max_dist: f64) -> Result<Option<(String, f64)>> {
        let ef = self.client.ef();
        let nearest: Vec<NearDoc> = self
            .client
            .rows(&format!(
                "SELECT source_path, vector::distance::knn() AS d FROM chunk \
                 WHERE embedding <|1,{ef}|> {};",
                floats(vec)
            ))
            .await?
            .into_iter()
            .next()
            .unwrap_or_default();
        Ok(nearest
            .into_iter()
            .next()
            .filter(|n| n.d <= max_dist)
            .map(|n| (n.source_path, n.d)))
    }
}

fn now_micros() -> Result<i64> {
    let since_epoch = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .context("system clock before 1970")?;
    i64::try_from(since_epoch.as_micros()).context("clock micros overflow")
}

// ── the one-shot fill ─────────────────────────────────────────────────────────────────────────

/// Row count and key-set hash of one table on one side.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeySet {
    pub rows: usize,
    pub hash: String,
}

impl KeySet {
    fn of(mut keys: Vec<String>) -> Self {
        keys.sort_unstable();
        let mut hasher = Sha256::new();
        for key in &keys {
            hasher.update(key.as_bytes());
            hasher.update(b"\n");
        }
        Self {
            rows: keys.len(),
            hash: hex::encode(hasher.finalize()),
        }
    }
}

/// One table of the fill: what pgvector holds, what Surreal now holds.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FillTable {
    pub table: &'static str,
    pub pg: KeySet,
    pub surreal: KeySet,
}

impl FillTable {
    #[must_use]
    pub fn same(&self) -> bool {
        self.pg == self.surreal
    }

    #[must_use]
    pub fn line(&self) -> String {
        format!(
            "{table}: pg rows={pr} hash={ph} | surreal rows={sr} hash={sh} | {verdict}",
            table = self.table,
            pr = self.pg.rows,
            ph = self.pg.hash,
            sr = self.surreal.rows,
            sh = self.surreal.hash,
            verdict = if self.same() { "same" } else { "DIFFERENT" },
        )
    }
}

mod claim_reads;
mod claim_tx;
mod doc_reads;
mod doc_tx;
mod graph_tx;
pub use claim_tx::{ClaimWrite, SplitStanding, TX_RETRIES, TxCount, TxPlace};
pub use doc_tx::{NoteChunk, NoteWrite};
pub use graph_tx::GraphOp;
mod reads;
mod recurrences;

#[cfg(test)]
mod claim_row_tests;

#[cfg(test)]
mod text_terms_tests {
    use super::text_terms;

    #[test]
    fn punctuation_is_not_a_term_and_a_query_of_only_punctuation_is_empty() {
        assert_eq!(
            text_terms("hello, world! 한글123 a_b"),
            "hello world 한글123 a b"
        );
        assert_eq!(text_terms("!!! ... ?"), "");
    }
}

/// Rows per page when reading `claim_row` back whole (a row carries the claim's `value`, not 11 KB
/// of floats, but 20,000 of them at once is the answer size `KEY_PAGE` already ruled out).
const CLAIM_ROW_PAGE: usize = 2_000;

/// `claim_row` in pg against Surreal, row by row and column by column.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ClaimRowDiff {
    pub pg_rows: usize,
    pub surreal_rows: usize,
    /// Keys pg holds and Surreal lacks.
    pub missing: Vec<String>,
    /// Keys Surreal holds that pg lacks.
    pub extra: Vec<String>,
    /// Keys both hold whose columns differ (a seal or stale mark that moved only on one side).
    pub different: Vec<String>,
    /// The record literals to write to bring `missing` and `different` level.
    pub redo: Vec<String>,
}

/// A row a Surreal write made or moved (`claim_row.via`): the diff leaves it alone.
fn written_on_surreal(row: &Value) -> bool {
    row.get("via").and_then(Value::as_str) == Some("surreal")
}

impl ClaimRowDiff {
    /// Compare pg's rows (by key) with what Surreal read back, keyed by `cid`.
    #[must_use]
    pub fn of(pg: &HashMap<String, Value>, surreal: &HashMap<String, Value>) -> Self {
        let mut diff = Self {
            pg_rows: pg.len(),
            surreal_rows: surreal.len(),
            ..Self::default()
        };
        let mut keys: Vec<&String> = pg.keys().collect();
        keys.sort_unstable();
        for key in keys {
            let want = &pg[key];
            match surreal.get(key) {
                None => {
                    diff.missing.push(key.clone());
                    diff.redo.push(want.to_string());
                }
                Some(have) if written_on_surreal(have) => {}
                Some(have) if have != want => {
                    diff.different.push(key.clone());
                    diff.redo.push(want.to_string());
                }
                Some(_) => {}
            }
        }
        diff.extra = surreal
            .iter()
            .filter(|(k, row)| !pg.contains_key(*k) && !written_on_surreal(row))
            .map(|(k, _)| k.clone())
            .collect();
        diff.extra.sort_unstable();
        diff
    }

    #[must_use]
    pub fn same(&self) -> bool {
        self.missing.is_empty() && self.extra.is_empty() && self.different.is_empty()
    }

    #[must_use]
    pub fn line(&self) -> String {
        format!(
            "claim_row values: pg rows={} | surreal rows={} | missing {} · extra {} · \
             different {} | {}",
            self.pg_rows,
            self.surreal_rows,
            self.missing.len(),
            self.extra.len(),
            self.different.len(),
            if self.same() { "same" } else { "DIFFERENT" }
        )
    }
}

fn claim_key(subject: &str, predicate: &str, valid_from_us: i64) -> String {
    format!("{subject}{KEY_SEP}{predicate}{KEY_SEP}{valid_from_us}")
}

const CHUNK_FROM: &str = "FROM chunk c JOIN document d ON d.source_path = c.source_path \
                          WHERE c.embedding IS NOT NULL";
const CLAIM_FROM: &str = "FROM claim c JOIN document d ON d.source_path = c.source_path \
                          WHERE c.superseded_at IS NULL AND c.embedding IS NOT NULL";
const EPOCH_US: &str = "(extract(epoch from {col}) * 1000000)::bigint";

fn epoch_us(col: &str) -> String {
    EPOCH_US.replace("{col}", col)
}

/// Why a failed graph (`node`/`edge`) write has no recovery by fill: since C0b1 bundle 2 those rows
/// exist only in Surreal, so pg holds no copy of them to fill from. What pg already committed in
/// the same request (a document row, for one) stays; the graph rows of the request do not exist.
pub const SURREAL_GRAPH_FAILED: &str = "Surreal 그래프(node·edge) 쓰기 실패. 그래프 행은 pg 에 없음, 같은 요청에서 pg 가 이미 커밋한 행(문서 등)만 남아 있음";

#[derive(Deserialize)]
struct DocOrigin {
    source_path: String,
    origin: String,
    project: String,
}

/// Surreal answers one statement per statement sent; none at all is a broken reply, and reading it
/// as "no rows" would hide a failed statement.
pub(super) fn first_statement<T>(query: &str, statements: Vec<Vec<T>>) -> Result<Vec<T>> {
    statements
        .into_iter()
        .next()
        .ok_or_else(|| anyhow!("surreal answered no statement to: {query}"))
}

fn chunk_select(tail: &str) -> String {
    format!(
        "SELECT c.id, c.source_path, c.project, {updated}, c.embedding, c.content, c.origin \
         {CHUNK_FROM} {tail}",
        updated = epoch_us("d.updated_at")
    )
}

fn claim_select(tail: &str) -> String {
    format!(
        "SELECT c.subject, c.predicate, {valid}, c.source_path, d.origin, d.project,
                c.kind, c.anchor, {stale}, c.embedding {CLAIM_FROM} {tail}",
        valid = epoch_us("c.valid_from"),
        stale = epoch_us("c.stale_at")
    )
}

/// `(key, SurrealQL record literal)` of one `chunk_select` row.
fn chunk_literal(r: &Row) -> (String, String) {
    let cid: String = r.get(0);
    let embedding: Vector = r.get(4);
    let literal = format!(
        "{{cid: {}, source_path: {}, project: {}, doc_updated_us: {}, content: {}, origin: {}, \
         embedding: {}}}",
        text(&cid),
        text(&r.get::<_, String>(1)),
        text(&r.get::<_, String>(2)),
        r.get::<_, i64>(3),
        text(&r.get::<_, String>(5)),
        text(&r.get::<_, String>(6)),
        floats(embedding.as_slice())
    );
    (cid, literal)
}

/// `(key, SurrealQL record literal)` of one `claim_select` row.
fn claim_literal(r: &Row) -> (String, String) {
    claim_literal_of(r, &r.get::<_, String>(4), &r.get::<_, String>(5))
}

/// `claim_literal` with the note's `origin` and `project` given, not read from the row: on
/// `backend=surreal` they come from Surreal's `document` (`claim_select_bare`).
fn claim_literal_of(r: &Row, origin: &str, project: &str) -> (String, String) {
    let subject: String = r.get(0);
    let predicate: String = r.get(1);
    let valid_from_us: i64 = r.get(2);
    let embedding: Vector = r.get(9);
    let cid = claim_key(&subject, &predicate, valid_from_us);
    let anchor = r
        .get::<_, Option<String>>(7)
        .map_or_else(String::new, |a| format!("anchor: {},", text(&a)));
    let stale = r
        .get::<_, Option<i64>>(8)
        .map_or_else(String::new, |s| format!("stale_us: {s},"));
    let literal = format!(
        "{{cid: {}, subject: {}, predicate: {}, valid_from_us: {valid_from_us}, \
         source_path: {}, origin: {}, project: {}, kind: {}, {anchor} {stale} \
         embedding: {}}}",
        text(&cid),
        text(&subject),
        text(&predicate),
        text(&r.get::<_, String>(3)),
        text(origin),
        text(project),
        text(&r.get::<_, String>(6)),
        floats(embedding.as_slice())
    );
    (cid, literal)
}

/// `origin` and `project` of notes as Surreal's `document` holds them, by `source_path`.
type DocOrigins = HashMap<String, (String, String)>;

/// A claim row's `origin` and `project` set from `docs`; a note with no document row leaves them
/// unset, as pg's `LEFT JOIN document` did.
fn with_doc_origin(mut row: Value, docs: &DocOrigins) -> Value {
    let found = row
        .get("source_path")
        .and_then(Value::as_str)
        .and_then(|p| docs.get(p))
        .cloned();
    if let Some(o) = row.as_object_mut() {
        if let Some((origin, project)) = found {
            o.insert("origin".into(), json!(origin));
            o.insert("project".into(), json!(project));
        } else {
            o.remove("origin");
            o.remove("project");
        }
    }
    row
}

fn document_select(tail: &str) -> String {
    format!(
        "SELECT d.source_path, d.origin, d.project, d.kind, d.title, d.tags, d.sha, \
                d.extracted_sha, {updated}, d.author FROM document d {tail}",
        updated = epoch_us("d.updated_at")
    )
}

/// `(key, SurrealQL record literal)` of one `document_select` row. The key is the `source_path`;
/// a note without a title leaves the `title` field out (it is `option<string>` on both sides).
fn document_literal(r: &Row) -> (String, String) {
    let path: String = r.get(0);
    let title = r
        .get::<_, Option<String>>(4)
        .map_or_else(String::new, |t| format!("title: {},", text(&t)));
    let literal = format!(
        "{{cid: {p}, source_path: {p}, origin: {}, project: {}, kind: {}, {title} tags: {}, \
         sha: {}, extracted_sha: {}, updated_us: {}, author: {}}}",
        text(&r.get::<_, String>(1)),
        text(&r.get::<_, String>(2)),
        text(&r.get::<_, String>(3)),
        texts(&r.get::<_, Vec<String>>(5)),
        text(&r.get::<_, String>(6)),
        text(&r.get::<_, String>(7)),
        r.get::<_, i64>(8),
        text(&r.get::<_, String>(9)),
        p = text(&path),
    );
    (path, literal)
}

/// Every pg `claim` row, sealed ones included, with the origin and project of its note. The
/// `claim` table above holds only open, embedded claims for the vector search; this one is the
/// copy the registers read, so it keeps `superseded_at`. Its embedding is read by
/// `claim_row_embedded_select` only: the whole-table compare (`claim_row_diff`) must not page
/// 4 KB of floats per row. A separate table,
/// so `swap_if_changed` (a fixed list of HNSW tables) never sees it and its index is untouched.
/// A claim whose note has no `document` row keeps `origin`/`project` unset: the register join
/// would drop it, and the register query here requires `origin` to exist.
fn claim_row_select(tail: &str) -> String {
    claim_row_select_with("", tail)
}

/// `claim_row_select` plus `c.embedding` as column 17: pg keeps the embedding on sealed rows too,
/// and a sealed row promoted again in Surreal needs it to re-enter the vector `claim` table.
fn claim_row_embedded_select(tail: &str) -> String {
    claim_row_select_with(", c.embedding", tail)
}

fn claim_row_select_with(extra: &str, tail: &str) -> String {
    format!(
        "SELECT c.subject, c.predicate, c.value, c.source_path, {valid}, {sealed}, c.kind,
                c.confidence, c.anchor, {stale}, c.stale_reason, c.era, c.anchor_hash,
                c.anchor_symbol, c.said_by, d.origin, d.project{extra}
           FROM claim c LEFT JOIN document d ON d.source_path = c.source_path {tail}",
        valid = epoch_us("c.valid_from"),
        sealed = epoch_us("c.superseded_at"),
        stale = epoch_us("c.stale_at"),
    )
}

/// `claim_row_json` of a `claim_row_embedded_select` row, with `embedding` when pg holds one.
fn claim_row_embedded_json(r: &Row) -> (String, Value) {
    let (key, mut row) = claim_row_json(r);
    if let Some(e) = r.get::<_, Option<Vector>>(17) {
        row["embedding"] = json!(e.as_slice());
    }
    (key, row)
}

/// `(key, row)` of one `claim_row_select` row; a NULL column is left out of the object, which is
/// how Surreal holds `option<..>` NONE. The same object is written (as its JSON text, a valid
/// SurrealQL object literal) and compared against what Surreal reads back.
fn claim_row_json(r: &Row) -> (String, Value) {
    let subject: String = r.get(0);
    let predicate: String = r.get(1);
    let valid_from_us: i64 = r.get(4);
    let key = claim_key(&subject, &predicate, valid_from_us);
    let mut o = serde_json::Map::new();
    o.insert("cid".into(), json!(key));
    o.insert("subject".into(), json!(subject));
    o.insert("predicate".into(), json!(predicate));
    o.insert("value".into(), json!(r.get::<_, String>(2)));
    o.insert("source_path".into(), json!(r.get::<_, String>(3)));
    o.insert("valid_from_us".into(), json!(valid_from_us));
    o.insert("kind".into(), json!(r.get::<_, String>(6)));
    o.insert("confidence".into(), json!(r.get::<_, String>(7)));
    o.insert("era".into(), json!(r.get::<_, String>(11)));
    let optional_int = [(5, "superseded_us"), (9, "stale_us")];
    for (i, name) in optional_int {
        if let Some(v) = r.get::<_, Option<i64>>(i) {
            o.insert(name.into(), json!(v));
        }
    }
    let optional_text = [
        (8, "anchor"),
        (10, "stale_reason"),
        (12, "anchor_hash"),
        (13, "anchor_symbol"),
        (14, "said_by"),
        (15, "origin"),
        (16, "project"),
    ];
    for (i, name) in optional_text {
        if let Some(v) = r.get::<_, Option<String>>(i) {
            o.insert(name.into(), json!(v));
        }
    }
    (key, Value::Object(o))
}

const CLAIM_ROW_DDL: &str = "DEFINE TABLE claim_row SCHEMAFULL;
         DEFINE FIELD cid ON claim_row TYPE string;
         DEFINE FIELD subject ON claim_row TYPE string;
         DEFINE FIELD predicate ON claim_row TYPE string;
         DEFINE FIELD value ON claim_row TYPE string;
         DEFINE FIELD source_path ON claim_row TYPE string;
         DEFINE FIELD valid_from_us ON claim_row TYPE int;
         DEFINE FIELD superseded_us ON claim_row TYPE option<int>;
         DEFINE FIELD kind ON claim_row TYPE string;
         DEFINE FIELD confidence ON claim_row TYPE string;
         DEFINE FIELD anchor ON claim_row TYPE option<string>;
         DEFINE FIELD stale_us ON claim_row TYPE option<int>;
         DEFINE FIELD stale_reason ON claim_row TYPE option<string>;
         DEFINE FIELD era ON claim_row TYPE string;
         DEFINE FIELD anchor_hash ON claim_row TYPE option<string>;
         DEFINE FIELD anchor_symbol ON claim_row TYPE option<string>;
         DEFINE FIELD said_by ON claim_row TYPE option<string>;
         DEFINE FIELD origin ON claim_row TYPE option<string>;
         DEFINE FIELD project ON claim_row TYPE option<string>;
         DEFINE FIELD via ON claim_row TYPE option<string>;
         DEFINE FIELD embedding ON claim_row TYPE option<array<float>>;
         DEFINE INDEX claim_row_path ON claim_row FIELDS source_path;
         DEFINE INDEX claim_row_slot ON claim_row FIELDS subject, predicate;";

/// What one register read asks for, in the terms of `Store::recent_register_rows`.
pub struct RegisterAsk<'a> {
    pub k: i64,
    pub project: Option<&'a str>,
    pub kinds: Option<&'a [String]>,
    pub exclude_origins: &'a [String],
}

/// The Surreal text of `Store::recent_register_rows`' WHERE and ORDER BY, condition for condition:
/// open (`superseded_us` unset), the note exists (`origin` set, as the pg inner join), project,
/// kinds, `NOT origin = ANY(exclude)`, source_path not a generated note, and the rank (a short
/// value, a predicate restating its kind, one each) before `valid_from` newest-first. `subject`,
/// `predicate` close a tie in byte order; pg's two register queries say the same with
/// `COLLATE "C"`, so equal `valid_from` rows come out in one order on both sides. The regexes are the same constants pg takes;
/// `(?i)` is pg's `~*`.
pub(super) fn register_where(ask: &RegisterAsk<'_>) -> String {
    let mut conds = vec![
        "superseded_us = NONE".to_owned(),
        "origin != NONE".to_owned(),
        format!("!(origin IN {})", texts(ask.exclude_origins)),
        format!(
            "!string::matches(source_path, <regex> {})",
            text(NOT_USER_MEMORY_RE)
        ),
    ];
    if let Some(p) = ask.project {
        conds.push(format!("project = {}", text(p)));
    }
    if let Some(ks) = ask.kinds {
        conds.push(format!("kind IN {}", texts(ks)));
    }
    conds.join(" AND ")
}

fn register_select(ask: &RegisterAsk<'_>) -> String {
    format!(
        "SELECT subject, predicate, value, kind, confidence, valid_from_us, project ?? '' AS project,
                (IF string::len(value) < {informative} {{ 1 }} ELSE {{ 0 }})
                + (IF string::matches(predicate, <regex> {taut}) {{ 1 }} ELSE {{ 0 }}) AS rank
           FROM claim_row WHERE {conds}
          ORDER BY rank ASC, valid_from_us DESC, subject ASC, predicate ASC LIMIT {k};",
        informative = INFORMATIVE_VALUE_CHARS,
        taut = text(&format!("(?i){TAUTOLOGICAL_PREDICATES}")),
        conds = register_where(ask),
        k = ask.k.max(0),
    )
}

fn register_count(ask: &RegisterAsk<'_>) -> String {
    format!(
        "SELECT count() AS n FROM claim_row WHERE {} GROUP ALL;",
        register_where(ask)
    )
}

#[derive(Deserialize)]
struct RegisterHit {
    subject: String,
    predicate: String,
    value: String,
    kind: String,
    confidence: String,
    valid_from_us: i64,
    project: String,
}

#[derive(Deserialize)]
struct Count {
    n: i64,
}

fn node_select(tail: &str) -> String {
    format!("SELECT n.id, n.kind, n.label, n.outcome FROM node n {tail}")
}

/// `(key, SurrealQL record literal)` of one `node_select` row. The key is the node id; a node
/// without an `outcome` leaves the field out.
fn node_literal(r: &Row) -> (String, String) {
    let id: String = r.get(0);
    let outcome = r
        .get::<_, Option<String>>(3)
        .map_or_else(String::new, |o| format!("outcome: {},", text(&o)));
    let literal = format!(
        "{{cid: {}, kind: {}, label: {}, {outcome}}}",
        text(&id),
        text(&r.get::<_, String>(1)),
        text(&r.get::<_, String>(2)),
    );
    (id, literal)
}

fn edge_key(src: &str, dst: &str, kind: &str) -> String {
    format!("{src}{KEY_SEP}{dst}{KEY_SEP}{kind}")
}

fn edge_select(tail: &str) -> String {
    format!(
        "SELECT e.src, e.dst, e.kind, e.judge, {first_seen} FROM edge e {tail}",
        first_seen = epoch_us("e.first_seen_at")
    )
}

/// `(key, SurrealQL record literal)` of one `edge_select` row; `judge` and `first_seen_us` are
/// left out when pg holds NULL (rows written before those columns existed).
fn edge_literal(r: &Row) -> (String, String) {
    let src: String = r.get(0);
    let dst: String = r.get(1);
    let kind: String = r.get(2);
    let key = edge_key(&src, &dst, &kind);
    let judge = r
        .get::<_, Option<String>>(3)
        .map_or_else(String::new, |j| format!("judge: {},", text(&j)));
    let first_seen = r
        .get::<_, Option<i64>>(4)
        .map_or_else(String::new, |t| format!("first_seen_us: {t},"));
    let literal = format!(
        "{{cid: {}, src: {}, dst: {}, kind: {}, {judge} {first_seen}}}",
        text(&key),
        text(&src),
        text(&dst),
        text(&kind),
    );
    (key, literal)
}

const NODE_DDL: &str = "DEFINE TABLE node SCHEMAFULL;
         DEFINE FIELD cid ON node TYPE string;
         DEFINE FIELD kind ON node TYPE string;
         DEFINE FIELD label ON node TYPE string;
         DEFINE FIELD outcome ON node TYPE option<string>;";

const EDGE_DDL: &str = "DEFINE TABLE edge SCHEMAFULL;
         DEFINE FIELD cid ON edge TYPE string;
         DEFINE FIELD src ON edge TYPE string;
         DEFINE FIELD dst ON edge TYPE string;
         DEFINE FIELD kind ON edge TYPE string;
         DEFINE FIELD judge ON edge TYPE option<string>;
         DEFINE FIELD first_seen_us ON edge TYPE option<int>;
         DEFINE INDEX edge_src ON edge FIELDS src;
         DEFINE INDEX edge_dst ON edge FIELDS dst;";

const DOCUMENT_DDL: &str = "DEFINE TABLE document SCHEMAFULL;
         DEFINE FIELD cid ON document TYPE string;
         DEFINE FIELD source_path ON document TYPE string;
         DEFINE FIELD origin ON document TYPE string;
         DEFINE FIELD project ON document TYPE string;
         DEFINE FIELD kind ON document TYPE string;
         DEFINE FIELD title ON document TYPE option<string>;
         DEFINE FIELD tags ON document TYPE array<string>;
         DEFINE FIELD sha ON document TYPE string;
         DEFINE FIELD extracted_sha ON document TYPE string;
         DEFINE FIELD updated_us ON document TYPE int;
         DEFINE FIELD author ON document TYPE string;
         DEFINE FIELD via ON document TYPE option<string>;";

/// pg's `(document, node, edge)` keys, in the shapes Surreal keys them by. The fill's compare
/// reads all three (`catch_up` no longer reads any: C0b1 copy off).
async fn pg_graph_keys(
    db: &tokio_postgres::Client,
) -> Result<(Vec<String>, Vec<String>, Vec<String>)> {
    let documents = db
        .query("SELECT source_path FROM document;", &[])
        .await
        .context("read document keys from pgvector")?
        .iter()
        .map(|r| r.get(0))
        .collect();
    let (nodes, edges) = pg_node_edge_keys(db).await?;
    Ok((documents, nodes, edges))
}

/// pg's `(node, edge)` keys, in the shapes Surreal keys them by.
async fn pg_node_edge_keys(db: &tokio_postgres::Client) -> Result<(Vec<String>, Vec<String>)> {
    let nodes = db
        .query("SELECT id FROM node;", &[])
        .await
        .context("read node ids from pgvector")?
        .iter()
        .map(|r| r.get(0))
        .collect();
    let edges = db
        .query("SELECT src, dst, kind FROM edge;", &[])
        .await
        .context("read edge keys from pgvector")?
        .iter()
        .map(|r| {
            edge_key(
                &r.get::<_, String>(0),
                &r.get::<_, String>(1),
                &r.get::<_, String>(2),
            )
        })
        .collect();
    Ok((nodes, edges))
}

/// A `claim_row` as Surreal reads it back, without the record id, under its `cid`.
fn without_id(mut row: Value) -> (String, Value) {
    if let Some(o) = row.as_object_mut() {
        o.remove("id");
    }
    let cid = row
        .get("cid")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    (cid, row)
}

/// pg's vector `chunk` keys (the rows the fill copies: embedded, with a document).
async fn pg_chunk_keys(db: &tokio_postgres::Client) -> Result<Vec<String>> {
    Ok(db
        .query(&format!("SELECT c.id {CHUNK_FROM};"), &[])
        .await
        .context("read chunk ids from pgvector")?
        .iter()
        .map(|r| r.get(0))
        .collect())
}

/// pg's vector `claim` keys (open, embedded, with a document), in the shape Surreal keys them by.
async fn pg_claim_keys(db: &tokio_postgres::Client) -> Result<Vec<String>> {
    Ok(db
        .query(
            &format!(
                "SELECT c.subject, c.predicate, {valid} {CLAIM_FROM};",
                valid = epoch_us("c.valid_from")
            ),
            &[],
        )
        .await
        .context("read open claim keys from pgvector")?
        .iter()
        .map(|r| claim_key(&r.get::<_, String>(0), &r.get::<_, String>(1), r.get(2)))
        .collect())
}

/// The keys one side holds, per table.
struct TableKeys {
    documents: Vec<String>,
    nodes: Vec<String>,
    edges: Vec<String>,
    chunks: Vec<String>,
    claims: Vec<String>,
    claim_rows: Vec<String>,
    /// `claim(open)` keys with `stale_us`, the value that moves under a key both sides hold.
    claim_stale: Vec<String>,
}

/// `stale_us` of each vector claim by key (`None` = open).
type ClaimStales = HashMap<String, Option<i64>>;

fn stale_entries(stales: &ClaimStales) -> Vec<String> {
    stales
        .iter()
        .map(|(key, stale)| match stale {
            Some(us) => format!("{key}{KEY_SEP}{us}"),
            None => format!("{key}{KEY_SEP}-"),
        })
        .collect()
}

/// pg's `stale_us` for every vector claim (the rows `pg_claim_keys` names).
async fn pg_claim_stales(db: &tokio_postgres::Client) -> Result<ClaimStales> {
    Ok(db
        .query(
            &format!(
                "SELECT c.subject, c.predicate, {valid}, {stale} {CLAIM_FROM};",
                valid = epoch_us("c.valid_from"),
                stale = epoch_us("c.stale_at")
            ),
            &[],
        )
        .await
        .context("read open claim stale_at from pgvector")?
        .iter()
        .map(|r| {
            (
                claim_key(&r.get::<_, String>(0), &r.get::<_, String>(1), r.get(2)),
                r.get::<_, Option<i64>>(3),
            )
        })
        .collect())
}

#[derive(Deserialize)]
struct CidStale {
    cid: String,
    stale_us: Option<i64>,
}

fn tables(pg: TableKeys, surreal: TableKeys) -> Vec<FillTable> {
    vec![
        FillTable {
            table: "document",
            pg: KeySet::of(pg.documents),
            surreal: KeySet::of(surreal.documents),
        },
        FillTable {
            table: "node",
            pg: KeySet::of(pg.nodes),
            surreal: KeySet::of(surreal.nodes),
        },
        FillTable {
            table: "edge",
            pg: KeySet::of(pg.edges),
            surreal: KeySet::of(surreal.edges),
        },
        FillTable {
            table: "chunk",
            pg: KeySet::of(pg.chunks),
            surreal: KeySet::of(surreal.chunks),
        },
        FillTable {
            table: "claim(open)",
            pg: KeySet::of(pg.claims),
            surreal: KeySet::of(surreal.claims),
        },
        FillTable {
            table: "claim(stale_at)",
            pg: KeySet::of(pg.claim_stale),
            surreal: KeySet::of(surreal.claim_stale),
        },
        FillTable {
            table: "claim_row(all)",
            pg: KeySet::of(pg.claim_rows),
            surreal: KeySet::of(surreal.claim_rows),
        },
    ]
}

impl SurrealClient {
    /// Names of the per-project chunk tables the database has now (`INFO FOR DB`).
    async fn project_tables(&self) -> Result<Vec<String>> {
        let info = "INFO FOR DB;";
        let statements = self.run(info).await?;
        let first = statements
            .into_iter()
            .next()
            .ok_or_else(|| anyhow!("surreal answered no statement to: {info}"))?;
        let tables = first
            .get("tables")
            .and_then(Value::as_object)
            .ok_or_else(|| anyhow!("INFO FOR DB has no tables object: {first}"))?;
        Ok(tables
            .keys()
            .filter(|n| n.starts_with(PROJECT_TABLE_PREFIX))
            .cloned()
            .collect())
    }

    /// The project's table when its slug can name one and the table exists; otherwise the global
    /// table (a slug outside `[a-z0-9_-]` never has a table; a project not yet filled has none).
    async fn chunk_source(&self, project: Option<&str>) -> Result<ChunkSource> {
        match project.map(ProjectTable::parse) {
            Some(Ok(table)) => {
                let have = self.project_tables().await?;
                if have.iter().any(|n| n == table.name()) {
                    Ok(ChunkSource::Project(table))
                } else {
                    Ok(ChunkSource::Global)
                }
            }
            Some(Err(_)) | None => Ok(ChunkSource::Global),
        }
    }

    /// Swap-in rebuild of every HNSW index whose table had rows change since the last swap: define
    /// a new-named index of the same definition, confirm the server reports it built, then remove
    /// the old one. `REBUILD INDEX` is never used: in place it answers 0 rows silently while it
    /// builds, and defining the new name first leaves the old index answering until the new one is
    /// ready. One failed table does not stop the others; each result is a `Swap`.
    ///
    /// "Rows changed since the last swap" lives in Surreal itself, as `vector_meta:<table>.dirty`
    /// (bumped inside each write transaction, `dirty_bump` in `doc_tx.rs` and `claim_tx.rs`): the counter sits in the same store as the index it
    /// describes, so it survives an engine restart and `fill_from` (which rebuilds the tables)
    /// resets it. A file or an in-process counter would drift from the index it is about.
    pub async fn swap_if_changed(&self) -> Result<Vec<Swap>> {
        let mut tables = vec!["chunk".to_owned(), "claim".to_owned()];
        tables.extend(self.project_tables().await?);
        let mut out = Vec::with_capacity(tables.len() + 1);
        if let Err(e) = self.ensure_text_index().await {
            out.push(Swap::Failed {
                table: "chunk(text)".to_owned(),
                error: format!("{e:#}"),
            });
        }
        for table in tables {
            out.push(match self.swap_table(&table).await {
                Ok(swap) => swap,
                Err(e) => Swap::Failed {
                    table,
                    error: format!("{e:#}"),
                },
            });
        }
        Ok(out)
    }

    async fn dirty(&self, table: &str) -> Result<u64> {
        let query = format!("SELECT VALUE dirty FROM type::record('vector_meta', '{table}');");
        let rows = first_statement(&query, self.rows::<Option<u64>>(&query).await?)?;
        Ok(rows.into_iter().flatten().next().unwrap_or(0))
    }

    /// The table's HNSW index: its name and the server's own `DEFINE INDEX` text for it.
    async fn hnsw_index(&self, table: &str) -> Result<(String, String)> {
        let hnsw = self.hnsw_indexes(table).await?;
        match <[(String, String); 1]>::try_from(hnsw) {
            Ok([one]) => Ok(one),
            Err(found) => Err(anyhow!(
                "{table} 에 HNSW 인덱스가 정확히 하나여야 함: {}",
                found
                    .iter()
                    .map(|(n, _)| n.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            )),
        }
    }

    /// Define the table's HNSW index when it has none (a rebuild that was refused or timed out
    /// left it bare). Returns how long the table had been without one in this call's reach, or
    /// `None` when it already had one.
    async fn ensure_hnsw(&self, table: &str, dim: usize) -> Result<Option<Duration>> {
        if self.hnsw_indexes(table).await?.is_empty() {
            let started = std::time::Instant::now();
            let old = format!("{table}_embedding");
            let def = format!(
                "DEFINE INDEX {} ON {} FIELDS embedding HNSW DIMENSION {dim} DIST COSINE TYPE F32 EFC 64 M 16",
                quote_ident(&old),
                quote_ident(table)
            );
            self.define_next_generation(table, &old, &def).await?;
            Ok(Some(started.elapsed()))
        } else {
            Ok(None)
        }
    }

    async fn hnsw_indexes(&self, table: &str) -> Result<Vec<(String, String)>> {
        Ok(self
            .index_defs(table)
            .await?
            .into_iter()
            .filter(|(_, def)| def.contains(" HNSW "))
            .collect())
    }

    /// Define the BM25 index on `chunk` when the table has none, and wait until it is `ready`.
    /// Returns whether it was defined by this call.
    async fn ensure_text_index(&self) -> Result<bool> {
        let have = self
            .index_defs("chunk")
            .await?
            .iter()
            .any(|(_, def)| def.contains(" FULLTEXT "));
        if have {
            return Ok(false);
        }
        self.run(TEXT_ANALYZER_DDL).await?;
        self.run(&format!("{TEXT_INDEX_DDL} CONCURRENTLY;"))
            .await
            .context("전문검색 인덱스 정의 실패")?;
        self.await_ready(&quote_ident("chunk_content_ft"), &quote_ident("chunk"))
            .await?;
        Ok(true)
    }

    async fn index_defs(&self, table: &str) -> Result<Vec<(String, String)>> {
        let info = format!("INFO FOR TABLE {};", quote_ident(table));
        let first = self
            .run(&info)
            .await?
            .into_iter()
            .next()
            .ok_or_else(|| anyhow!("surreal answered no statement to: {info}"))?;
        let indexes = first
            .get("indexes")
            .and_then(Value::as_object)
            .ok_or_else(|| anyhow!("{info} has no indexes object: {first}"))?;
        Ok(indexes
            .iter()
            .filter_map(|(name, def)| def.as_str().map(|d| (name.clone(), d.to_owned())))
            .collect())
    }

    /// Poll `INFO FOR INDEX` until `building.status` is `ready`; `error`/`failed`, a missing
    /// `building`, or the deadline is an `Err`.
    async fn await_ready(&self, q_index: &str, q_table: &str) -> Result<Value> {
        let info = format!("INFO FOR INDEX {q_index} ON {q_table};");
        let polls = INDEX_BUILD_WAIT.as_millis() / INDEX_BUILD_POLL.as_millis();
        for _ in 0..polls {
            let building = self
                .run(&info)
                .await?
                .into_iter()
                .next()
                .and_then(|v| v.get("building").cloned())
                .ok_or_else(|| anyhow!("{info} 에 building 이 없음"))?;
            match building.get("status").and_then(Value::as_str) {
                Some("ready") => return Ok(building),
                Some("error" | "failed") => return Err(anyhow!("인덱스 빌드 실패: {building}")),
                _ => tokio::time::sleep(INDEX_BUILD_POLL).await,
            }
        }
        Err(anyhow!(
            "{}s 안에 ready 가 되지 않음",
            INDEX_BUILD_WAIT.as_secs()
        ))
    }

    async fn swap_table(&self, table: &str) -> Result<Swap> {
        let changed = self.dirty(table).await?;
        if changed == 0 {
            return Ok(Swap::Skipped {
                table: table.to_owned(),
            });
        }
        let (old, def) = self.hnsw_index(table).await?;
        let new = next_generation(&old);
        // The server prints the name escaped when it needs it, so the name is cut by position
        // (`DEFINE INDEX <name> ON <rest>`), not matched by text.
        let rest = def
            .strip_prefix("DEFINE INDEX ")
            .and_then(|d| d.split_once(" ON "))
            .map(|(_, rest)| rest)
            .ok_or_else(|| anyhow!("{old} 의 정의를 읽지 못함: {def}"))?;
        let define = format!("DEFINE INDEX {} ON {rest}", quote_ident(&new));
        let (q_table, q_old, q_new) = (quote_ident(table), quote_ident(&old), quote_ident(&new));
        // A plain DEFINE builds inside one transaction; on a populated table (claim, 27,722 rows,
        // v3.3.5) RocksDB answers TransactionConflict (MemTable newer than the txn) and the old
        // index stays. CONCURRENTLY builds in the background, so the reply is immediate and the
        // build is polled until `ready`. Anything else, or no `ready` within INDEX_BUILD_WAIT, is
        // a failure: the new index is removed, the old one never touched.
        self.run(&format!("{define} CONCURRENTLY;"))
            .await
            .with_context(|| format!("새 인덱스 {new} 정의 실패"))?;
        let building = match self.await_ready(&q_new, &q_table).await {
            Ok(b) => b,
            Err(e) => {
                self.run(&format!("REMOVE INDEX {q_new} ON {q_table};"))
                    .await?;
                return Err(e.context(format!("새 인덱스 {new} 가 ready 가 되지 못함")));
            }
        };
        self.run(&format!("REMOVE INDEX {q_old} ON {q_table};"))
            .await?;
        // Rows mirrored while the build ran stay counted for the next swap.
        self.run(&format!(
            "UPSERT type::record('vector_meta', '{table}') SET dirty = math::max([(dirty ?? 0) - {changed}, 0]);"
        ))
        .await?;
        Ok(Swap::Swapped {
            table: table.to_owned(),
            old,
            new,
            changed,
            status: building.to_string(),
        })
    }

    /// Define the next generation of an HNSW index from the old one's definition text and wait
    /// until it is `ready`; a build that does not get there is removed and is the error.
    async fn define_next_generation(&self, table: &str, old: &str, def: &str) -> Result<Value> {
        let new = next_generation(old);
        let rest = def
            .strip_prefix("DEFINE INDEX ")
            .and_then(|d| d.split_once(" ON "))
            .map(|(_, rest)| rest)
            .ok_or_else(|| anyhow!("{old} 의 정의를 읽지 못함: {def}"))?;
        let (q_table, q_new) = (quote_ident(table), quote_ident(&new));
        self.define_when_reclaimed(
            table,
            &format!("DEFINE INDEX {q_new} ON {rest} CONCURRENTLY;"),
        )
        .await
        .with_context(|| format!("새 인덱스 {new} 정의 실패"))?;
        match self.await_ready(&q_new, &q_table).await {
            Ok(building) => Ok(building),
            Err(e) => {
                self.run(&format!("REMOVE INDEX {q_new} ON {q_table};"))
                    .await?;
                Err(e.context(format!("새 인덱스 {new} 가 ready 가 되지 못함")))
            }
        }
    }

    /// Root cause: after a mass delete the server frees the table's document-ID space in the
    /// background and refuses `DEFINE INDEX` until that ends ("retry DEFINE INDEX after cleanup
    /// completes"); the end is not observable from the client, so it is the server's own
    /// instruction that is followed, bounded like the build wait. Past the bound the caller gets
    /// `ReclaimTimeout`, not a hang.
    async fn define_when_reclaimed(&self, table: &str, define: &str) -> Result<()> {
        let polls = INDEX_BUILD_WAIT.as_millis() / INDEX_BUILD_POLL.as_millis();
        for _ in 0..polls {
            match self.run(define).await {
                Ok(_) => return Ok(()),
                Err(e) if matches!(e.downcast_ref::<Refusal>(), Some(Refusal::Reclaiming(_))) => {
                    tokio::time::sleep(INDEX_BUILD_POLL).await;
                }
                Err(e) => return Err(e),
            }
        }
        Err(anyhow::Error::new(ReclaimTimeout {
            table: table.to_owned(),
            waited_s: INDEX_BUILD_WAIT.as_secs(),
        }))
    }

    async fn upsert_rows(&self, table: &str, literals: &[String]) -> Result<()> {
        for batch in literals.chunks(FILL_BATCH) {
            self.run(&format!(
                "FOR $r IN [{}] {{ UPSERT type::record('{table}', $r.cid) CONTENT $r; }};",
                batch.join(",")
            ))
            .await?;
        }
        Ok(())
    }

    pub(super) async fn delete_rows(&self, table: &str, keys: &[String]) -> Result<()> {
        for batch in keys.chunks(FILL_BATCH) {
            self.run(&format!(
                "FOR $k IN {} {{ DELETE type::record('{table}', $k); }};",
                texts(batch)
            ))
            .await?;
        }
        Ok(())
    }

    /// `origin` and `project` of these notes from Surreal's own `document` rows (C0b1: pg no longer
    /// receives them, so the `claim_row_diff` against pg cannot join them there).
    async fn doc_origins(&self, paths: &[String]) -> Result<DocOrigins> {
        let mut unique: Vec<String> = paths.to_vec();
        unique.sort_unstable();
        unique.dedup();
        let mut found = DocOrigins::new();
        for batch in unique.chunks(CATCH_UP_BATCH) {
            let sql = format!(
                "SELECT source_path, origin, project FROM document WHERE source_path INSIDE {};",
                texts(batch)
            );
            for d in first_statement(&sql, self.rows::<DocOrigin>(&sql).await?)? {
                found.insert(d.source_path, (d.origin, d.project));
            }
        }
        Ok(found)
    }

    /// Every claim row on both sides, compared by key and by value (all columns, `superseded_us`
    /// included). Only meaningful right after `fill_from` (Z4): from C0b1 bundle 3 on the engine
    /// writes `claim_row` straight into Surreal, so pg's copy goes stale by design.
    ///
    /// Two paged passes, so memory holds one page of rows and not the table (measured on 66.8k
    /// rows: the two whole-table maps were +300 MB of a 477 MB sync, 130 MB of it the pg side):
    /// pg's rows a page at a time, each looked up in Surreal by key (missing, different), then
    /// Surreal's keys a page at a time, each looked up in pg (extra).
    pub async fn claim_row_diff(&self, store: &Store) -> Result<ClaimRowDiff> {
        let db = store.db().await?;
        let gone = self.surreal_gone().await?;
        let mut diff = ClaimRowDiff::default();
        let mut offset = 0_i64;
        loop {
            let rows = db
                .query(
                    &claim_row_select(
                        "ORDER BY c.subject, c.predicate, c.valid_from LIMIT $1 OFFSET $2;",
                    ),
                    &[&i64::try_from(CLAIM_ROW_PAGE)?, &offset],
                )
                .await
                .context("read a page of claim rows from pgvector")?;
            if rows.is_empty() {
                break;
            }
            offset += i64::try_from(rows.len())?;
            let row_paths: Vec<String> = rows.iter().map(|r| r.get::<_, String>(3)).collect();
            let docs = self.doc_origins(&row_paths).await?;
            let pg: HashMap<String, Value> = rows
                .iter()
                .map(|r| {
                    let (key, row) = claim_row_json(r);
                    (key, with_doc_origin(row, &docs))
                })
                .filter(|(key, _)| !gone.contains(key))
                .collect();
            let keys: Vec<String> = pg.keys().cloned().collect();
            let query = format!("SELECT * FROM claim_row WHERE cid INSIDE {};", texts(&keys));
            let found: Vec<Value> = first_statement(&query, self.rows::<Value>(&query).await?)?;
            let surreal: HashMap<String, Value> = found
                .into_iter()
                .map(without_id)
                .map(|(key, mut row)| {
                    if let Some(o) = row.as_object_mut() {
                        o.remove("embedding");
                    }
                    (key, row)
                })
                .collect();
            let page = ClaimRowDiff::of(&pg, &surreal);
            diff.pg_rows += pg.len();
            diff.missing.extend(page.missing);
            diff.different.extend(page.different);
            diff.redo.extend(page.redo);
        }
        let mut seen = 0_usize;
        loop {
            let query = format!(
                "SELECT VALUE cid FROM claim_row WHERE via != 'surreal' \
                 LIMIT {CLAIM_ROW_PAGE} START {seen};"
            );
            let keys: Vec<String> = first_statement(&query, self.rows::<String>(&query).await?)?;
            seen += keys.len();
            let held: std::collections::HashSet<String> = db
                .query(
                    &format!(
                        "SELECT c.subject || chr(31) || c.predicate || chr(31) || {valid} \
                         FROM claim c WHERE c.subject || chr(31) || c.predicate || chr(31) || \
                         {valid} = ANY($1);",
                        valid = epoch_us("c.valid_from")
                    ),
                    &[&keys],
                )
                .await
                .context("look up a page of Surreal claim_row keys in pgvector")?
                .iter()
                .map(|r| r.get::<_, String>(0))
                .collect();
            diff.extra
                .extend(keys.iter().filter(|k| !held.contains(*k)).cloned());
            if keys.len() < CLAIM_ROW_PAGE {
                break;
            }
        }
        diff.surreal_rows = seen;
        diff.missing.sort_unstable();
        diff.different.sort_unstable();
        diff.extra.sort_unstable();
        Ok(diff)
    }

    /// Every `cid` of one table, a page at a time in the table's own key order: one answer
    /// carrying the 272k edge keys at once is what a 8GB Surreal VM could not hold.
    async fn table_keys(&self, table: &str) -> Result<Vec<String>> {
        let mut keys = Vec::new();
        loop {
            let query = format!(
                "SELECT VALUE cid FROM {table} LIMIT {KEY_PAGE} START {};",
                keys.len()
            );
            let page = first_statement(&query, self.rows::<String>(&query).await?)?;
            let done = page.len() < KEY_PAGE;
            keys.extend(page);
            if done {
                return Ok(keys);
            }
        }
    }

    /// `stale_us` of every row of the vector `claim` table, a page at a time like `table_keys`.
    async fn surreal_claim_stales(&self) -> Result<ClaimStales> {
        let mut stales = ClaimStales::new();
        loop {
            let query = format!(
                "SELECT cid, stale_us FROM claim LIMIT {KEY_PAGE} START {};",
                stales.len()
            );
            let page: Vec<CidStale> =
                first_statement(&query, self.rows::<CidStale>(&query).await?)?;
            let done = page.len() < KEY_PAGE;
            stales.extend(page.into_iter().map(|r| (r.cid, r.stale_us)));
            if done {
                return Ok(stales);
            }
        }
    }

    /// The keys of the claim rows a Surreal write deleted (`claim_gone`, written by the split
    /// merge) while pg still holds them. `claim_row_diff` skips these keys. A table nobody wrote
    /// reads as empty.
    pub(super) async fn surreal_gone(&self) -> Result<std::collections::HashSet<String>> {
        self.ensure_via_field().await?;
        let query = "SELECT VALUE cid FROM claim_gone;";
        Ok(first_statement(query, self.rows::<String>(query).await?)?
            .into_iter()
            .collect())
    }

    async fn surreal_keys(&self) -> Result<TableKeys> {
        Ok(TableKeys {
            documents: self.table_keys("document").await?,
            nodes: self.table_keys("node").await?,
            edges: self.table_keys("edge").await?,
            chunks: self.table_keys("chunk").await?,
            claims: self.table_keys("claim").await?,
            claim_rows: self.table_keys("claim_row").await?,
            claim_stale: stale_entries(&self.surreal_claim_stales().await?),
        })
    }

    /// Only the HNSW indexes are checked: every table (C0b1 bundles 1-3) is written straight into
    /// Surreal, so a key pg lacks is the row, not a stale one. `fill_from` is the one copy left.
    /// Returns `(written, deleted)`, always `(0, 0)`.
    pub async fn catch_up(&self, store: &Store) -> Result<(usize, usize)> {
        self.ensure_all_hnsw(store).await?;
        Ok((0, 0))
    }

    /// `ensure_hnsw` on every vector table; each table that had none is logged with the time the
    /// define took. The first failure is returned after every table was tried.
    async fn ensure_all_hnsw(&self, store: &Store) -> Result<()> {
        let mut tables = vec!["chunk".to_owned(), "claim".to_owned()];
        tables.extend(self.project_tables().await?);
        let mut result = Ok(());
        for table in &tables {
            match self.ensure_hnsw(table, store.dim).await {
                Ok(Some(took)) => eprintln!(
                    "[vector] {table}: HNSW 인덱스가 없어 새로 정의함 ({:.1}s)",
                    took.as_secs_f64()
                ),
                Ok(None) => {}
                Err(e) => {
                    eprintln!("[vector] {table}: HNSW 인덱스 없음 — 정의하지 못함: {e:#}");
                    result = result.and(Err(e));
                }
            }
        }
        result
    }

    /// `Store::recent_register_rows` / `recent_claims` against the `claim_row` copy: the same
    /// conditions and order (`register_select`), plus the full match count. Only
    /// `backend=surreal` calls this; a Surreal failure is an error, never a pg fallback.
    pub async fn register_rows(&self, ask: &RegisterAsk<'_>) -> Result<RegisterRows> {
        let select = register_select(ask);
        let count = register_count(ask);
        let hits: Vec<RegisterHit> =
            first_statement(&select, self.rows::<RegisterHit>(&select).await?)?;
        let counted: Vec<Count> = first_statement(&count, self.rows::<Count>(&count).await?)?;
        let rows = hits
            .into_iter()
            .map(|h| {
                let micros = u64::try_from(h.valid_from_us).with_context(|| {
                    format!("claim_row valid_from_us {} is before 1970", h.valid_from_us)
                })?;
                Ok(RegisterRow::new(
                    h.subject,
                    h.predicate,
                    h.value,
                    h.kind,
                    h.confidence,
                    std::time::UNIX_EPOCH + Duration::from_micros(micros),
                    h.project,
                ))
            })
            .collect::<Result<Vec<_>>>()?;
        Ok(RegisterRows {
            rows,
            total_matching: counted.first().map_or(0, |c| c.n),
        })
    }

    /// Read-only: row count and key-set hash per table, pg against Surreal, over what is there
    /// now (the doctor line). Same tables and key shapes as the fill's readback.
    pub async fn compare(&self, store: &Store) -> Result<Vec<FillTable>> {
        let db = store.db().await?;
        let (pg_documents, pg_nodes, pg_edges) = pg_graph_keys(&db).await?;
        let pg_chunks = pg_chunk_keys(&db).await?;
        let pg_claims = pg_claim_keys(&db).await?;
        let pg_claim_rows: Vec<String> = db
            .query(
                &format!(
                    "SELECT subject, predicate, {valid} FROM claim;",
                    valid = epoch_us("valid_from")
                ),
                &[],
            )
            .await
            .context("read claim keys from pgvector")?
            .iter()
            .map(|r| claim_key(&r.get::<_, String>(0), &r.get::<_, String>(1), r.get(2)))
            .collect();
        Ok(tables(
            TableKeys {
                documents: pg_documents,
                nodes: pg_nodes,
                edges: pg_edges,
                chunks: pg_chunks,
                claims: pg_claims,
                claim_rows: pg_claim_rows,
                claim_stale: stale_entries(&pg_claim_stales(&db).await?),
            },
            self.surreal_keys().await?,
        ))
    }

    /// Replace Surreal's chunk and open-claim tables with pgvector's, then report row count and
    /// key-set hash per table from both sides. Idempotent: a re-run starts from empty tables.
    ///
    /// Z4: after bundle B pg does not receive Surreal writes (document, chunk), so the rehearsal
    /// "all-table diff 0/N" is measured after this fill aligns them right before the switch-over.
    /// It is the switch-over tool only: no normal path calls it (`main.rs` `VectorFill`).
    #[allow(clippy::too_many_lines)] // schema DDL + two paged copies + the readback: one linear procedure
    pub async fn fill_from(&self, store: &Store) -> Result<Vec<FillTable>> {
        let dim = store.dim;
        self.run(&format!(
            "DEFINE NAMESPACE IF NOT EXISTS {ns}; DEFINE DATABASE IF NOT EXISTS {db};",
            ns = self.settings.namespace.as_str(),
            db = self.settings.database.as_str()
        ))
        .await?;
        let old_projects: String = self
            .project_tables()
            .await?
            .iter()
            .map(|t| format!("REMOVE TABLE IF EXISTS {};", quote_ident(t)))
            .collect::<Vec<_>>()
            .concat();
        let pg_projects: Vec<(String, String, String)> = store
            .db()
            .await?
            .query(&format!("SELECT DISTINCT c.project {CHUNK_FROM};"), &[])
            .await
            .context("read the projects of the chunks from pgvector")?
            .iter()
            .map(|r| (r.get::<_, String>(0), String::new(), String::new()))
            .collect();
        let project_tables = Placed::of(pg_projects);
        project_tables.report();
        let project_ddl: String = project_tables
            .by_table
            .keys()
            .map(|t| chunk_ddl(t.name(), dim))
            .collect();
        let global_ddl = chunk_ddl("chunk", dim);
        self.run(&format!(
            "{old_projects}
             REMOVE TABLE IF EXISTS vector_meta;
             DEFINE TABLE vector_meta SCHEMALESS;
             REMOVE TABLE IF EXISTS document;
             {DOCUMENT_DDL}
             REMOVE TABLE IF EXISTS node;
             {NODE_DDL}
             REMOVE TABLE IF EXISTS edge;
             {EDGE_DDL}
             REMOVE TABLE IF EXISTS chunk;
             {global_ddl}
             {project_ddl}
             REMOVE TABLE IF EXISTS claim_row;
             {CLAIM_ROW_DDL}
             REMOVE TABLE IF EXISTS claim_gone;
             DEFINE TABLE claim_gone SCHEMALESS;
             REMOVE TABLE IF EXISTS claim;
             DEFINE TABLE claim SCHEMAFULL;
             DEFINE FIELD cid ON claim TYPE string;
             DEFINE FIELD subject ON claim TYPE string;
             DEFINE FIELD predicate ON claim TYPE string;
             DEFINE FIELD valid_from_us ON claim TYPE int;
             DEFINE FIELD source_path ON claim TYPE string;
             DEFINE FIELD origin ON claim TYPE string;
             DEFINE FIELD project ON claim TYPE string;
             DEFINE FIELD kind ON claim TYPE string;
             DEFINE FIELD anchor ON claim TYPE option<string>;
             DEFINE FIELD stale_us ON claim TYPE option<int>;
             DEFINE FIELD embedding ON claim TYPE array<float>;
             DEFINE INDEX claim_embedding ON claim FIELDS embedding
                 HNSW DIMENSION {dim} DIST COSINE TYPE F32 EFC 64 M 16;"
        ))
        .await?;

        let mut document_keys = Vec::new();
        let mut after = String::new();
        loop {
            let rows = store
                .db()
                .await?
                .query(
                    &document_select("WHERE d.source_path > $1 ORDER BY d.source_path LIMIT $2;"),
                    &[&after, &i64::try_from(FILL_BATCH)?],
                )
                .await
                .context("read documents from pgvector")?;
            let Some(last) = rows.last() else { break };
            after = last.get(0);
            let (keys, literals): (Vec<String>, Vec<String>) =
                rows.iter().map(document_literal).unzip();
            document_keys.extend(keys);
            self.upsert_rows("document", &literals).await?;
        }

        let mut node_keys = Vec::new();
        let mut after = String::new();
        loop {
            let rows = store
                .db()
                .await?
                .query(
                    &node_select("WHERE n.id > $1 ORDER BY n.id LIMIT $2;"),
                    &[&after, &i64::try_from(FILL_BATCH)?],
                )
                .await
                .context("read nodes from pgvector")?;
            let Some(last) = rows.last() else { break };
            after = last.get(0);
            let (keys, literals): (Vec<String>, Vec<String>) =
                rows.iter().map(node_literal).unzip();
            node_keys.extend(keys);
            self.upsert_rows("node", &literals).await?;
        }

        let mut edge_keys = Vec::new();
        let mut after = (String::new(), String::new(), String::new());
        loop {
            let rows = store
                .db()
                .await?
                .query(
                    &edge_select(
                        "WHERE (e.src, e.dst, e.kind) > ($1, $2, $3) \
                         ORDER BY e.src, e.dst, e.kind LIMIT $4;",
                    ),
                    &[&after.0, &after.1, &after.2, &i64::try_from(FILL_BATCH)?],
                )
                .await
                .context("read edges from pgvector")?;
            let Some(last) = rows.last() else { break };
            after = (last.get(0), last.get(1), last.get(2));
            let (keys, literals): (Vec<String>, Vec<String>) =
                rows.iter().map(edge_literal).unzip();
            edge_keys.extend(keys);
            self.upsert_rows("edge", &literals).await?;
        }

        let mut chunk_keys = Vec::new();
        let mut after = String::new();
        loop {
            let rows = store
                .db()
                .await?
                .query(
                    &chunk_select("AND c.id > $1 ORDER BY c.id LIMIT $2;"),
                    &[&after, &i64::try_from(FILL_BATCH)?],
                )
                .await
                .context("read chunks from pgvector")?;
            let Some(last) = rows.last() else { break };
            after = last.get(0);
            let (keys, literals): (Vec<String>, Vec<String>) =
                rows.iter().map(chunk_literal).unzip();
            chunk_keys.extend(keys);
            self.upsert_rows("chunk", &literals).await?;
            let placed = Placed::of(
                rows.iter()
                    .map(|r| {
                        let (key, literal) = chunk_literal(r);
                        (r.get::<_, String>(2), key, literal)
                    })
                    .collect(),
            );
            for (table, rows) in placed.by_table {
                let literals: Vec<String> = rows.into_iter().map(|(_, l)| l).collect();
                self.upsert_rows(table.name(), &literals).await?;
            }
        }

        self.ensure_text_index().await?;

        let mut claim_keys = Vec::new();
        let mut offset = 0_i64;
        loop {
            let rows = store
                .db()
                .await?
                .query(
                    &claim_select(
                        "ORDER BY c.subject, c.predicate, c.valid_from LIMIT $1 OFFSET $2;",
                    ),
                    &[&i64::try_from(FILL_BATCH)?, &offset],
                )
                .await
                .context("read claims from pgvector")?;
            if rows.is_empty() {
                break;
            }
            offset += i64::try_from(rows.len())?;
            let (keys, literals): (Vec<String>, Vec<String>) =
                rows.iter().map(claim_literal).unzip();
            claim_keys.extend(keys);
            self.upsert_rows("claim", &literals).await?;
        }

        let mut claim_row_keys = Vec::new();
        let mut offset = 0_i64;
        loop {
            let rows = store
                .db()
                .await?
                .query(
                    &claim_row_embedded_select(
                        "ORDER BY c.subject, c.predicate, c.valid_from LIMIT $1 OFFSET $2;",
                    ),
                    &[&i64::try_from(FILL_BATCH)?, &offset],
                )
                .await
                .context("read every claim row from pgvector")?;
            if rows.is_empty() {
                break;
            }
            offset += i64::try_from(rows.len())?;
            let (keys, literals): (Vec<String>, Vec<String>) = rows
                .iter()
                .map(|r| {
                    let (key, row) = claim_row_embedded_json(r);
                    (key, row.to_string())
                })
                .unzip();
            claim_row_keys.extend(keys);
            self.upsert_rows("claim_row", &literals).await?;
        }

        Ok(tables(
            TableKeys {
                documents: document_keys,
                nodes: node_keys,
                edges: edge_keys,
                chunks: chunk_keys,
                claims: claim_keys,
                claim_rows: claim_row_keys,
                claim_stale: stale_entries(&pg_claim_stales(&*store.db().await?).await?),
            },
            self.surreal_keys().await?,
        ))
    }
}
