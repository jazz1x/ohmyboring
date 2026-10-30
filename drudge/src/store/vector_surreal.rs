//! SurrealDB implementation of five of the `VectorIndex` queries, plus the one-shot fill that
//! copies the vectors out of pgvector.
//!
//! SurrealDB holds only what ranking needs: the row's key, its embedding, and the columns the pgvector
//! SQL filters on. pgvector stays the original — every hit is hydrated from it by key, so the answer
//! carries the same text, origin and anchor the pgvector path returns. A key Surreal returns that
//! pgvector no longer has is an error naming the drift, never a silently shorter answer. The copy
//! follows pg's writes (`SurrealClient::mirror`, called after each committed pg write); a drift is
//! a Surreal write that failed (the error said so) or a pg change made outside the engine.
//!
//! Wire: SurrealDB's HTTP JSON-RPC (`POST /rpc`, method `query`) with bind variables, over the
//! `reqwest` the engine already carries. The server is pinned in compose to
//! `surrealdb/surrealdb:v3.3.5@sha256:3db515b8…` (v3.3.4 lost recall after delete/overwrite churn; v3.3.5 fixes part of it).
//!
//! Index and query parameters are stage-0's: HNSW `DIMENSION <embed_dim> DIST COSINE TYPE F32 EFC 64
//! M 16`; query EF is `vector.surreal.ef` (default 400: at 100 and 200 one golden query pgvector finds is missed, at 400 it is not; stage 0 measured 40 = pgvector's ef_search).
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

use super::{AnchoredClaim, DistKind, Hit, NOT_USER_MEMORY_RE, Recurrence, Store};
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
/// Prefix of a per-project chunk table. The global `chunk` table keeps every chunk; a project table
/// keeps that project's chunks only, so a project-filtered KNN scans the project's graph instead of
/// filtering inside the global one (measured p95 120-150 ms for the in-KNN filter).
const PROJECT_TABLE_PREFIX: &str = "chunk_p_";

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
                format!("{table}: FAILED — 옛 인덱스 그대로: {error}")
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
            (Some(e), _) => Err(anyhow!("surreal: {}", e.message)),
            (None, Some(statements)) => statements
                .into_iter()
                .map(|s| {
                    if s.status == "OK" {
                        Ok(s.result)
                    } else {
                        Err(anyhow!("surreal statement {}: {}", s.status, s.result))
                    }
                })
                .collect(),
            (None, None) => Err(anyhow!(
                "surreal reply (http {status}) has neither result nor error"
            )),
        }
    }

    async fn rows<T: DeserializeOwned>(&self, sql: &str) -> Result<Vec<Vec<T>>> {
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

fn texts(items: &[String]) -> String {
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

/// The surreal side of the route: Surreal ranks, pgvector hydrates.
pub(super) struct SurrealView<'a> {
    pub(super) client: &'a SurrealClient,
    pub(super) pg: &'a Store,
}

fn drift(what: &str, missing: &[String]) -> anyhow::Error {
    anyhow!(
        "surreal 이 가리킨 {what} {n}개가 pg 에 없음 (한 번 채우기 이후 갈림, 예: {first}) — vector-fill 을 다시 돌릴 것",
        n = missing.len(),
        first = missing.first().map_or("", String::as_str)
    )
}

/// A dropped row is a visible event, not a silent filter: the answer is shorter than asked.
fn report_dropped(what: &str, dropped: usize) {
    if dropped > 0 {
        eprintln!("[vector] surreal: pg 재확인이 {what} {dropped}개를 뺌 (채운 뒤 바뀜)");
    }
}

impl SurrealView<'_> {
    /// pgvector is the source and the Surreal copy can lag it (a failed Surreal write, a pg change
    /// made outside the engine), so every key Surreal returns is re-checked here against the live
    /// pg row with the same predicates the pg path applies (the #121 origin boundary: an origin or
    /// project changed after the fill must not leak through a stale copy). A row that fails is dropped, not replaced — the answer may come
    /// back shorter than `k`. A key pg no longer has is drift and an error.
    async fn hydrate_hits(
        &self,
        near: Vec<NearChunk>,
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Result<Vec<Hit>> {
        let ids: Vec<String> = near.iter().map(|n| n.cid.clone()).collect();
        let rows = self
            .pg
            .db()
            .await?
            .query(
                "SELECT c.id, c.content, c.origin, c.project, c.source_path,
                        (($2::text IS NULL OR c.project = $2)
                         AND ($3::int IS NULL OR d.updated_at >= now() - make_interval(hours => $3)))
                 FROM chunk c JOIN document d ON d.source_path = c.source_path
                 WHERE c.id = ANY($1);",
                &[&ids, &project, &since_hours],
            )
            .await
            .context("hydrate chunk hits")?;
        let mut by_id: HashMap<String, (String, String, String, String, bool)> = rows
            .iter()
            .map(|r| (r.get(0), (r.get(1), r.get(2), r.get(3), r.get(4), r.get(5))))
            .collect();
        let mut missing = Vec::new();
        let mut dropped = 0_usize;
        let mut hits = Vec::with_capacity(near.len());
        for n in near {
            match by_id.remove(&n.cid) {
                Some((content, origin, project, source_path, true)) => hits.push(Hit {
                    id: n.cid,
                    content,
                    origin,
                    project,
                    source_path,
                    #[allow(clippy::cast_possible_truncation)]
                    dist: n.d as f32,
                    dist_kind: DistKind::VectorCosine,
                }),
                Some((.., false)) => dropped += 1,
                None => missing.push(n.cid),
            }
        }
        ensure!(missing.is_empty(), drift("chunk", &missing));
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
        self.hydrate_hits(near, project, since_hours).await
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

    fn recurrences(
        &self,
        _days: i64,
        _project: Option<&str>,
        _limit: i64,
    ) -> impl std::future::Future<Output = Result<Vec<Recurrence>>> {
        std::future::ready(Err(anyhow!(
            "recurrences 는 surreal 에 구현되지 않음 — Store 가 pgvector 로 보낸다"
        )))
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
        let subjects: Vec<String> = near.iter().map(|n| n.subject.clone()).collect();
        let predicates: Vec<String> = near.iter().map(|n| n.predicate.clone()).collect();
        let froms: Vec<i64> = near.iter().map(|n| n.valid_from_us).collect();
        let rows = self
            .pg
            .db()
            .await?
            .query(
                "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence, c.anchor, c.era,
                        c.stale_at, c.stale_reason,
                        (extract(epoch from c.valid_from) * 1000000)::bigint,
                        (NOT (d.origin = ANY($4))
                         AND ($5::text IS NULL OR d.project = $5)
                         AND ($6::text[] IS NULL OR c.kind = ANY($6))
                         AND d.source_path !~ $7
                         AND ($8::text IS NULL OR split_part(c.anchor, ':', 2) LIKE replace(replace($8, '%', '\\%'), '_', '\\_') || '%' ESCAPE '\\')
                         AND ($9::bool OR c.stale_at IS NULL))
                 FROM claim c
                 JOIN document d ON d.source_path = c.source_path
                 JOIN unnest($1::text[], $2::text[], $3::bigint[]) AS k(s, p, v)
                   ON c.subject = k.s AND c.predicate = k.p
                  AND (extract(epoch from c.valid_from) * 1000000)::bigint = k.v
                 WHERE c.superseded_at IS NULL;",
                &[
                    &subjects,
                    &predicates,
                    &froms,
                    &exclude_origins,
                    &project,
                    &kinds,
                    &NOT_USER_MEMORY_RE,
                    &anchor_path,
                    &include_stale,
                ],
            )
            .await
            .context("hydrate claims")?;
        // Same re-check as `hydrate_hits`: the copy can lag, pg is the source.
        let mut by_key: HashMap<(String, String, i64), (bool, AnchoredClaim)> = rows
            .iter()
            .map(|r| {
                (
                    (r.get(0), r.get(1), r.get(9)),
                    (
                        r.get::<_, bool>(10),
                        AnchoredClaim {
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
                        },
                    ),
                )
            })
            .collect();
        let mut missing = Vec::new();
        let mut dropped = 0_usize;
        let mut out = Vec::with_capacity(near.len());
        for n in near {
            let key = (n.subject, n.predicate, n.valid_from_us);
            match by_key.remove(&key) {
                Some((true, claim)) => out.push(claim),
                Some((false, _)) => dropped += 1,
                None => missing.push(format!("{}:{}", key.0, key.1)),
            }
        }
        ensure!(missing.is_empty(), drift("claim", &missing));
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

/// Why a Surreal write that follows a committed pg write is an error and not a retry: pg already
/// holds the change, so repeating the request would duplicate it (a second note, a second
/// supersede). The one recovery is the fill, which rebuilds Surreal from pg.
pub const SURREAL_BEHIND: &str =
    "pg 에는 기록됨, Surreal 만 빠짐. 같은 요청을 다시 보내지 말고 채우기를 다시 돌릴 것";

/// What a committed pg write touched, in the terms Surreal's rows are keyed by: the notes whose
/// chunks and claims were written, and the `(subject, predicate)` slots whose current row may
/// have moved.
#[derive(Debug, Clone, Default)]
pub struct Scope {
    paths: Vec<String>,
    slots: Vec<(String, String)>,
}

impl Scope {
    /// Every chunk and claim row of these notes, plus the slots those claims sit in (a fact slot
    /// is sealed across notes, so another note's row can stop being current).
    #[must_use]
    pub fn notes<I: IntoIterator<Item = String>>(paths: I) -> Self {
        let mut paths: Vec<String> = paths.into_iter().collect();
        paths.sort_unstable();
        paths.dedup();
        Self {
            paths,
            slots: Vec::new(),
        }
    }

    /// One slot, for a write that names claim rows by key and not by note.
    #[must_use]
    pub fn slot(subject: &str, predicate: &str) -> Self {
        Self {
            paths: Vec::new(),
            slots: vec![(subject.to_owned(), predicate.to_owned())],
        }
    }

    fn is_empty(&self) -> bool {
        self.paths.is_empty() && self.slots.is_empty()
    }
}

#[derive(Deserialize)]
struct Slot {
    subject: String,
    predicate: String,
}

/// Surreal answers one statement per statement sent; none at all is a broken reply, and reading it
/// as "no rows" would make `mirror` skip the deletes of stale keys.
fn first_statement<T>(query: &str, statements: Vec<Vec<T>>) -> Result<Vec<T>> {
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
        text(&r.get::<_, String>(4)),
        text(&r.get::<_, String>(5)),
        text(&r.get::<_, String>(6)),
        floats(embedding.as_slice())
    );
    (cid, literal)
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
         DEFINE FIELD author ON document TYPE string;";

fn tables(
    pg_documents: Vec<String>,
    surreal_documents: Vec<String>,
    pg_chunks: Vec<String>,
    pg_claims: Vec<String>,
    surreal_chunks: Vec<String>,
    surreal_claims: Vec<String>,
) -> Vec<FillTable> {
    vec![
        FillTable {
            table: "document",
            pg: KeySet::of(pg_documents),
            surreal: KeySet::of(surreal_documents),
        },
        FillTable {
            table: "chunk",
            pg: KeySet::of(pg_chunks),
            surreal: KeySet::of(surreal_chunks),
        },
        FillTable {
            table: "claim(open)",
            pg: KeySet::of(pg_claims),
            surreal: KeySet::of(surreal_claims),
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
    /// (`bump_dirty`, written by `reconcile`): the counter sits in the same store as the index it
    /// describes, so it survives an engine restart and `fill_from` (which rebuilds the tables)
    /// resets it. A file or an in-process counter would drift from the index it is about.
    pub async fn swap_if_changed(&self) -> Result<Vec<Swap>> {
        let mut tables = vec!["chunk".to_owned(), "claim".to_owned()];
        tables.extend(self.project_tables().await?);
        let mut out = Vec::with_capacity(tables.len());
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

    async fn bump_dirty(&self, table: &str, changed: usize) -> Result<()> {
        if changed == 0 {
            Ok(())
        } else {
            self.run(&format!(
                "UPSERT type::record('vector_meta', '{table}') SET dirty = (dirty ?? 0) + {changed};"
            ))
            .await
            .map(|_| ())
        }
    }

    async fn dirty(&self, table: &str) -> Result<u64> {
        let query = format!("SELECT VALUE dirty FROM type::record('vector_meta', '{table}');");
        let rows = first_statement(&query, self.rows::<Option<u64>>(&query).await?)?;
        Ok(rows.into_iter().flatten().next().unwrap_or(0))
    }

    /// The table's HNSW index: its name and the server's own `DEFINE INDEX` text for it.
    async fn hnsw_index(&self, table: &str) -> Result<(String, String)> {
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
        let hnsw: Vec<(String, String)> = indexes
            .iter()
            .filter_map(|(name, def)| def.as_str().map(|d| (name.clone(), d.to_owned())))
            .filter(|(_, def)| def.contains(" HNSW "))
            .collect();
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

    /// A project table is only read when it exists, so it must be complete when it appears: define
    /// it, then copy every chunk pg holds for the project.
    async fn create_project_table(&self, store: &Store, table: &ProjectTable) -> Result<()> {
        self.run(&chunk_ddl(table.name(), store.dim)).await?;
        let mut after = String::new();
        loop {
            let rows = store
                .db()
                .await?
                .query(
                    &chunk_select("AND c.project = $1 AND c.id > $2 ORDER BY c.id LIMIT $3;"),
                    &[&table.slug(), &after, &i64::try_from(FILL_BATCH)?],
                )
                .await
                .context("read the project's chunks from pgvector")?;
            let Some(last) = rows.last() else { break };
            after = last.get(0);
            let literals: Vec<String> = rows.iter().map(|r| chunk_literal(r).1).collect();
            self.upsert_rows(table.name(), &literals).await?;
        }
        Ok(())
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

    async fn delete_rows(&self, table: &str, keys: &[String]) -> Result<()> {
        for batch in keys.chunks(FILL_BATCH) {
            self.run(&format!(
                "FOR $k IN {} {{ DELETE type::record('{table}', $k); }};",
                texts(batch)
            ))
            .await?;
        }
        Ok(())
    }

    /// Make Surreal's rows in one scope equal pg's: upsert the ones pg has, then delete the keys
    /// pg no longer has. Upsert first so a failure half way leaves Surreal with more rows, never
    /// fewer; no transaction, because pg is the original and a re-run converges.
    async fn reconcile(
        &self,
        table: &str,
        wanted: Vec<(String, String)>,
        existing: Vec<String>,
    ) -> Result<()> {
        let keep: std::collections::HashSet<&str> =
            wanted.iter().map(|(k, _)| k.as_str()).collect();
        let stale: Vec<String> = existing
            .into_iter()
            .filter(|k| !keep.contains(k.as_str()))
            .collect();
        let literals: Vec<String> = wanted.into_iter().map(|(_, l)| l).collect();
        self.upsert_rows(table, &literals).await?;
        self.delete_rows(table, &stale).await?;
        self.bump_dirty(table, literals.len() + stale.len()).await
    }

    /// `reconcile` for a table with no HNSW index (`document`): there is no swap to schedule, so no
    /// dirty count is kept for it.
    async fn reconcile_documents(&self, store: &Store, paths: &[String]) -> Result<()> {
        let rows = store
            .db()
            .await?
            .query(&document_select("WHERE d.source_path = ANY($1)"), &[&paths])
            .await
            .context("read the written notes' documents from pgvector")?;
        let wanted: Vec<(String, String)> = rows.iter().map(document_literal).collect();
        let existing_query = format!(
            "SELECT VALUE cid FROM document WHERE source_path INSIDE {};",
            texts(paths)
        );
        let existing: Vec<String> =
            first_statement(&existing_query, self.rows::<String>(&existing_query).await?)?;
        let keep: std::collections::HashSet<&str> =
            wanted.iter().map(|(k, _)| k.as_str()).collect();
        let stale: Vec<String> = existing
            .into_iter()
            .filter(|k| !keep.contains(k.as_str()))
            .collect();
        let literals: Vec<String> = wanted.into_iter().map(|(_, l)| l).collect();
        self.upsert_rows("document", &literals).await?;
        self.delete_rows("document", &stale).await
    }

    /// Write-through: after pg committed a change, copy pg's state of everything it touched into
    /// Surreal. pg is the original, so Surreal is rebuilt from what pg holds now, never from what
    /// the request carried. Covers an upsert (new rows and changed columns), a supersede or
    /// unseal (a claim that stopped being open leaves Surreal, one that became open enters), and
    /// a delete (a key pg lacks leaves Surreal).
    pub async fn mirror(&self, store: &Store, scope: &Scope) -> Result<()> {
        if scope.is_empty() {
            Ok(())
        } else {
            self.reconcile_documents(store, &scope.paths).await?;
            self.mirror_chunks(store, &scope.paths).await?;
            self.mirror_claims(store, scope).await
        }
    }

    async fn mirror_chunks(&self, store: &Store, paths: &[String]) -> Result<()> {
        let rows = store
            .db()
            .await?
            .query(&chunk_select("AND c.source_path = ANY($1)"), &[&paths])
            .await
            .context("read the written notes' chunks from pgvector")?;
        let wanted: Vec<(String, String)> = rows.iter().map(chunk_literal).collect();
        let placed = Placed::of(
            rows.iter()
                .map(|r| {
                    let (key, literal) = chunk_literal(r);
                    (r.get::<_, String>(2), key, literal)
                })
                .collect(),
        );
        self.reconcile_chunk_table("chunk", wanted, paths).await?;
        self.mirror_project_tables(store, placed, paths).await
    }

    async fn reconcile_chunk_table(
        &self,
        table: &str,
        wanted: Vec<(String, String)>,
        paths: &[String],
    ) -> Result<()> {
        let existing_chunks = format!(
            "SELECT VALUE cid FROM {} WHERE source_path INSIDE {};",
            quote_ident(table),
            texts(paths)
        );
        let existing: Vec<String> = first_statement(
            &existing_chunks,
            self.rows::<String>(&existing_chunks).await?,
        )?;
        self.reconcile(table, wanted, existing).await
    }

    /// Every project table that exists or is needed: a table the scope's rows now belong to is
    /// created whole (see `create_project_table`) and reconciled; a table they left (the note's
    /// project changed, the note was deleted) is reconciled to empty for these paths.
    async fn mirror_project_tables(
        &self,
        store: &Store,
        placed: Placed,
        paths: &[String],
    ) -> Result<()> {
        placed.report();
        let have = self.project_tables().await?;
        for table in placed.by_table.keys() {
            if !have.iter().any(|n| n == table.name()) {
                self.create_project_table(store, table).await?;
            }
        }
        let mut names: std::collections::BTreeSet<String> = have.into_iter().collect();
        names.extend(placed.by_table.keys().map(|t| t.name().to_owned()));
        for name in names {
            let wanted = placed
                .by_table
                .iter()
                .find(|(t, _)| t.name() == name)
                .map(|(_, rows)| rows.clone())
                .unwrap_or_default();
            self.reconcile_chunk_table(&name, wanted, paths).await?;
        }
        Ok(())
    }

    async fn mirror_claims(&self, store: &Store, scope: &Scope) -> Result<()> {
        let db = store.db().await?;
        let pg_slots = db
            .query(
                "SELECT DISTINCT subject, predicate FROM claim WHERE source_path = ANY($1);",
                &[&scope.paths],
            )
            .await
            .context("read the written notes' claim slots from pgvector")?;
        let slots_query = format!(
            "SELECT subject, predicate FROM claim WHERE source_path INSIDE {};",
            texts(&scope.paths)
        );
        let surreal_slots: Vec<Slot> =
            first_statement(&slots_query, self.rows::<Slot>(&slots_query).await?)?;
        let mut slots: Vec<(String, String)> = scope.slots.clone();
        slots.extend(pg_slots.iter().map(|r| (r.get(0), r.get(1))));
        slots.extend(surreal_slots.into_iter().map(|s| (s.subject, s.predicate)));
        slots.sort_unstable();
        slots.dedup();
        let subjects: Vec<String> = slots.iter().map(|s| s.0.clone()).collect();
        let predicates: Vec<String> = slots.iter().map(|s| s.1.clone()).collect();
        let rows = db
            .query(
                &claim_select(
                    "AND (c.source_path = ANY($1) OR (c.subject, c.predicate) IN
                          (SELECT s, p FROM unnest($2::text[], $3::text[]) AS k(s, p)))",
                ),
                &[&scope.paths, &subjects, &predicates],
            )
            .await
            .context("read the written notes' open claims from pgvector")?;
        let wanted: Vec<(String, String)> = rows.iter().map(claim_literal).collect();
        // One membership test, not an `OR` chain: Surreal's parser refuses a chain of ~120 terms
        // ("nests or chains operators too deeply"), and a 1,280-note scope has more slots than that.
        let slot_pairs: Vec<String> = slots
            .iter()
            .map(|(s, p)| format!("[{},{}]", text(s), text(p)))
            .collect();
        let existing_claims = format!(
            "SELECT VALUE cid FROM claim WHERE source_path INSIDE {} \
             OR [subject, predicate] INSIDE [{}];",
            texts(&scope.paths),
            slot_pairs.join(",")
        );
        let existing: Vec<String> = first_statement(
            &existing_claims,
            self.rows::<String>(&existing_claims).await?,
        )?;
        self.reconcile("claim", wanted, existing).await
    }

    async fn surreal_keys(&self) -> Result<(Vec<String>, Vec<String>, Vec<String>)> {
        let document_query = "SELECT VALUE cid FROM document;";
        let documents =
            first_statement(document_query, self.rows::<String>(document_query).await?)?;
        let chunk_query = "SELECT VALUE cid FROM chunk;";
        let chunks = first_statement(chunk_query, self.rows::<String>(chunk_query).await?)?;
        let claim_query = "SELECT VALUE cid FROM claim;";
        let claims = first_statement(claim_query, self.rows::<String>(claim_query).await?)?;
        Ok((documents, chunks, claims))
    }

    /// Read-only: row count and key-set hash per table, pg against Surreal, over what is there
    /// now (the doctor line). Same tables and key shapes as the fill's readback.
    pub async fn compare(&self, store: &Store) -> Result<Vec<FillTable>> {
        let db = store.db().await?;
        let pg_documents: Vec<String> = db
            .query("SELECT source_path FROM document;", &[])
            .await
            .context("read document keys from pgvector")?
            .iter()
            .map(|r| r.get(0))
            .collect();
        let pg_chunks: Vec<String> = db
            .query(&format!("SELECT c.id {CHUNK_FROM};"), &[])
            .await
            .context("read chunk ids from pgvector")?
            .iter()
            .map(|r| r.get(0))
            .collect();
        let pg_claims: Vec<String> = db
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
            .collect();
        let (surreal_documents, surreal_chunks, surreal_claims) = self.surreal_keys().await?;
        Ok(tables(
            pg_documents,
            surreal_documents,
            pg_chunks,
            pg_claims,
            surreal_chunks,
            surreal_claims,
        ))
    }

    /// Replace Surreal's chunk and open-claim tables with pgvector's, then report row count and
    /// key-set hash per table from both sides. Idempotent: a re-run starts from empty tables.
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
             REMOVE TABLE IF EXISTS chunk;
             {global_ddl}
             {project_ddl}
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

        let (surreal_documents, surreal_chunks, surreal_claims) = self.surreal_keys().await?;
        Ok(tables(
            document_keys,
            surreal_documents,
            chunk_keys,
            claim_keys,
            surreal_chunks,
            surreal_claims,
        ))
    }
}
