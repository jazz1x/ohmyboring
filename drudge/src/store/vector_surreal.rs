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
//! `surrealdb/surrealdb:v3.3.4@sha256:649a60a3…` — the image the stage-0 measurement ran on.
//!
//! Index and query parameters are stage-0's: HNSW `DIMENSION <embed_dim> DIST COSINE TYPE F32 EFC 64
//! M 16`, query `EF 40` (pgvector's m16 / ef_construction 64 / ef_search 40).
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
use std::time::{Duration, SystemTime};

use anyhow::{Context, Result, anyhow, ensure};
use pgvector::Vector;
use serde::Deserialize;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio_postgres::Row;

use super::{AnchoredClaim, DistKind, Hit, NOT_USER_MEMORY_RE, Recurrence, Store};
use crate::config::SurrealSettings;
use crate::frontmatter::Claim;
use crate::vector::VectorIndex;

/// Query-time candidate list size (stage 0: EF 40, matching pgvector's `ef_search`).
const EF: usize = 40;
/// Rows per fill request: 1024 floats are ~11 KB of JSON, and the server refuses a request body over
/// 1 MiB (HTTP 413).
const FILL_BATCH: usize = 40;
/// Per-request ceiling for the I/O boundary; an HNSW insert batch is the slowest request.
const REQUEST_TIMEOUT: Duration = Duration::from_mins(5);
const KEY_SEP: char = '\u{1f}';

/// Connection to the SurrealDB server.
#[derive(Debug, Clone)]
pub struct SurrealClient {
    http: reqwest::Client,
    settings: SurrealSettings,
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
    pub fn new(settings: SurrealSettings) -> Result<Self> {
        let http = reqwest::Client::builder()
            .timeout(REQUEST_TIMEOUT)
            .build()
            .context("build surreal http client")?;
        Ok(Self { http, settings })
    }

    /// Run SurrealQL; one JSON value per statement. Any statement that did not answer `OK` fails
    /// the whole call with the server's message.
    ///
    /// Values go into the query text as literals (`text`, `floats`), never as RPC bind variables:
    /// the server reads a JSON string that looks like a record id (`mimir:satellite`, a claim
    /// anchor), a datetime or a uuid as that type, which broke the first fill on the copy.
    pub async fn run(&self, sql: &str) -> Result<Vec<Value>> {
        let body = json!({"id": 1, "method": "query", "params": [sql]});
        let response = self
            .http
            .post(format!("{}/rpc", self.settings.url.trim_end_matches('/')))
            .basic_auth(&self.settings.user, Some(&self.settings.password))
            .header("Surreal-NS", self.settings.namespace.as_str())
            .header("Surreal-DB", self.settings.database.as_str())
            .header("Accept", "application/json")
            .json(&body)
            .send()
            .await
            .context("surreal request")?;
        let status = response.status();
        let text = response.text().await.context("surreal response body")?;
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
        let sql: String = sources
            .iter()
            .map(|emb| {
                format!(
                    "SELECT source_path, vector::distance::knn() AS d FROM chunk \
                     WHERE embedding <|{EF},{EF}|> {} AND source_path != {};",
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
        let mut conds = vec![format!(
            "embedding <|{},{EF}|> {}",
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
        let sql = format!(
            "SELECT cid, vector::distance::knn() AS d FROM chunk \
             WHERE embedding <|{},{EF}|> {} ORDER BY d ASC;",
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
        let mut conds = vec![format!("embedding <|{},{EF}|> {}", k.max(1), floats(vec))];
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
            "SELECT cid, vector::distance::knn() AS d FROM chunk WHERE {} ORDER BY d ASC;",
            conds.join(" AND ")
        );
        self.chunk_knn(&sql, k, project, since_hours).await
    }

    async fn nearest_document(&self, vec: &[f32], max_dist: f64) -> Result<Option<(String, f64)>> {
        let nearest: Vec<NearDoc> = self
            .client
            .rows(&format!(
                "SELECT source_path, vector::distance::knn() AS d FROM chunk \
                 WHERE embedding <|1,{EF}|> {};",
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

fn first_statement<T>(statements: Vec<Vec<T>>) -> Vec<T> {
    statements.into_iter().next().unwrap_or_default()
}

fn chunk_select(tail: &str) -> String {
    format!(
        "SELECT c.id, c.source_path, c.project, {updated}, c.embedding {CHUNK_FROM} {tail}",
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
        "{{cid: {}, source_path: {}, project: {}, doc_updated_us: {}, embedding: {}}}",
        text(&cid),
        text(&r.get::<_, String>(1)),
        text(&r.get::<_, String>(2)),
        r.get::<_, i64>(3),
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

fn tables(
    pg_chunks: Vec<String>,
    pg_claims: Vec<String>,
    surreal_chunks: Vec<String>,
    surreal_claims: Vec<String>,
) -> Vec<FillTable> {
    vec![
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

    /// Make Surreal's rows in one scope equal pg's: delete the keys pg no longer has, upsert the
    /// ones it has.
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
        self.delete_rows(table, &stale).await?;
        let literals: Vec<String> = wanted.into_iter().map(|(_, l)| l).collect();
        self.upsert_rows(table, &literals).await
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
        let existing: Vec<String> = first_statement(
            self.rows::<String>(&format!(
                "SELECT VALUE cid FROM chunk WHERE source_path INSIDE {};",
                texts(paths)
            ))
            .await?,
        );
        self.reconcile("chunk", wanted, existing).await
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
        let surreal_slots: Vec<Slot> = first_statement(
            self.rows::<Slot>(&format!(
                "SELECT subject, predicate FROM claim WHERE source_path INSIDE {};",
                texts(&scope.paths)
            ))
            .await?,
        );
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
        let mut in_scope = vec![format!("source_path INSIDE {}", texts(&scope.paths))];
        in_scope.extend(
            slots
                .iter()
                .map(|(s, p)| format!("(subject = {} AND predicate = {})", text(s), text(p))),
        );
        let existing: Vec<String> = first_statement(
            self.rows::<String>(&format!(
                "SELECT VALUE cid FROM claim WHERE {};",
                in_scope.join(" OR ")
            ))
            .await?,
        );
        self.reconcile("claim", wanted, existing).await
    }

    async fn surreal_keys(&self) -> Result<(Vec<String>, Vec<String>)> {
        let chunks = first_statement(self.rows::<String>("SELECT VALUE cid FROM chunk;").await?);
        let claims = first_statement(self.rows::<String>("SELECT VALUE cid FROM claim;").await?);
        Ok((chunks, claims))
    }

    /// Read-only: row count and key-set hash per table, pg against Surreal, over what is there
    /// now (the doctor line). Same tables and key shapes as the fill's readback.
    pub async fn compare(&self, store: &Store) -> Result<Vec<FillTable>> {
        let db = store.db().await?;
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
        let (surreal_chunks, surreal_claims) = self.surreal_keys().await?;
        Ok(tables(pg_chunks, pg_claims, surreal_chunks, surreal_claims))
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
        self.run(&format!(
            "REMOVE TABLE IF EXISTS chunk;
             DEFINE TABLE chunk SCHEMAFULL;
             DEFINE FIELD cid ON chunk TYPE string;
             DEFINE FIELD source_path ON chunk TYPE string;
             DEFINE FIELD project ON chunk TYPE string;
             DEFINE FIELD doc_updated_us ON chunk TYPE int;
             DEFINE FIELD embedding ON chunk TYPE array<float>;
             DEFINE INDEX chunk_source_path ON chunk FIELDS source_path;
             DEFINE INDEX chunk_embedding ON chunk FIELDS embedding
                 HNSW DIMENSION {dim} DIST COSINE TYPE F32 EFC 64 M 16;
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

        let (surreal_chunks, surreal_claims) = self.surreal_keys().await?;
        Ok(tables(
            chunk_keys,
            claim_keys,
            surreal_chunks,
            surreal_claims,
        ))
    }
}
