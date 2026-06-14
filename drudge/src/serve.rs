//! Serve — HTTP resident daemon (axum) + background sync scheduler.
//!
//! Architecture:
//! - Shares `Store` + `Llm` via `Arc` (the Postgres client supports concurrent use).
//! - axum router: /health · /ask · /search · /graph · /audit · /sync
//! - Background scheduler: `DRUDGE_SYNC_HOURS` (default 4h) interval + one immediate run at startup.
//! - Error propagation: `AppError` (anyhow wrapper) → HTTP 500, JSON body.
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use axum::Json;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::ask;
use crate::audit;
use crate::distill;
use crate::extract;
use crate::graph;
use crate::ingest;
use crate::llm::Llm;
use crate::retrieve;
use crate::store::Store;
use crate::vault;
use crate::wiki_recall;

// ── shared state ──────────────────────────────────────────────────────────────

#[derive(Clone)]
pub struct AppState {
    /// pgvector backend. If `None`, `DRUDGE_VECTOR=off` — retrieval is direct vault/wiki reads (wiki_recall),
    /// and sync does compile (raw→wiki) only. Vector/graph-dependent endpoints reject explicitly.
    store: Option<Arc<Store>>,
    llm: Arc<Llm>,
    source_dirs: Arc<Vec<String>>,
    /// vault root (`DRUDGE_VAULT_DIR`). If `Some`, sync performs the raw→wiki compile.
    vault_dir: Arc<Option<PathBuf>>,
}

impl AppState {
    /// vault/wiki directory (the retrieval target for `DRUDGE_VECTOR=off`). None if vault is unset.
    fn wiki_dir(&self) -> Option<PathBuf> {
        (*self.vault_dir).as_ref().map(|v| v.join("wiki"))
    }
}

// ── error type (ROP: AppError → HTTP 500) ───────────────────────────────────

struct AppError(anyhow::Error);

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        #[derive(Serialize)]
        struct ErrBody {
            error: String,
        }
        let body = ErrBody {
            error: format!("{:#}", self.0),
        };
        (StatusCode::INTERNAL_SERVER_ERROR, Json(body)).into_response()
    }
}

impl<E: Into<anyhow::Error>> From<E> for AppError {
    fn from(e: E) -> Self {
        Self(e.into())
    }
}

// ── request/response types ─────────────────────────────────────────────────

#[derive(Deserialize)]
struct AskReq {
    question: String,
}

#[derive(Serialize)]
struct AskResp {
    answer: String,
    sources: Vec<String>,
}

#[derive(Deserialize)]
struct SearchReq {
    query: String,
}

#[derive(Serialize)]
struct SearchHit {
    id: String,
    origin: String,
    project: String,
    source_path: String,
    snippet: String,
}

#[derive(Serialize)]
struct SearchResp {
    hits: Vec<SearchHit>,
}

#[derive(Deserialize)]
struct GraphReq {
    query: String,
}

#[derive(Serialize)]
struct GraphResp {
    hit: String,
    graph_neighbors: Vec<String>,
    semantic_neighbors: Vec<String>,
}

/// `personal` default — used when the host leaves origin unspecified (company token unset).
fn default_origin() -> String {
    "personal".to_owned()
}

#[derive(Deserialize)]
struct DistillReq {
    /// Plain text the host extracted from the session transcript.
    text: String,
    #[serde(default)]
    session_id: String,
    #[serde(default = "default_origin")]
    origin: String,
    #[serde(default)]
    phase: String,
    #[serde(default)]
    repo: String,
    #[serde(default)]
    cwd: String,
}

#[derive(Serialize)]
struct DistillResp {
    /// KEEP/SKIP gate result — false means not worth storing, so discarded (not an error).
    written: bool,
    /// The recorded raw note's filename. The host joins it with RAW_DIR to fix the mtime + trigger sync.
    filename: Option<String>,
}

#[derive(Serialize)]
struct SyncResp {
    compile_total_raw: usize,
    compile_compiled: usize,
    compile_recompiled: usize,
    ingest_new: usize,
    ingest_updated: usize,
    ingest_deleted: usize,
    ingest_chunks: usize,
    extract_processed: usize,
    extract_skipped: usize,
    extract_nodes: usize,
    extract_edges: usize,
}

// ── handlers ──────────────────────────────────────────────────────────────────

async fn health() -> &'static str {
    "ok"
}

async fn handle_ask(
    State(s): State<AppState>,
    Json(req): Json<AskReq>,
) -> Result<Json<AskResp>, AppError> {
    // vector on → synthesize from vector+graph retrieval. off → synthesize from direct vault/wiki reads.
    let out = if let Some(store) = s.store.as_ref() {
        ask::answer(store, &s.llm, &req.question, &[]).await?
    } else {
        ask::answer_wiki(&s.llm, s.wiki_dir().as_deref(), &req.question).await?
    };
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Recency-first briefing — no question (recency retrieval). Called by the cron morning briefing.
/// Recency (updated_at) ordering depends on pgvector → rejected if `DRUDGE_VECTOR=off`.
async fn handle_brief(State(s): State<AppState>) -> Result<Json<AskResp>, AppError> {
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let out = ask::brief(store, &s.llm, &[]).await?;
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// The explicit rejection (not silence) that vector/graph-dependent endpoints return under `DRUDGE_VECTOR=off`.
fn vector_disabled() -> AppError {
    AppError(anyhow::anyhow!(
        "DRUDGE_VECTOR=off — 이 기능은 벡터 백엔드(pgvector)가 필요합니다. DRUDGE_VECTOR=on 으로 켜고 Postgres 를 띄우세요."
    ))
}

async fn handle_search(
    State(s): State<AppState>,
    Json(req): Json<SearchReq>,
) -> Result<Json<SearchResp>, AppError> {
    let mapped: Vec<SearchHit> = if let Some(store) = s.store.as_ref() {
        retrieve::retrieve(store, &s.llm, &req.query, 5, &[])
            .await?
            .into_iter()
            .map(|h| SearchHit {
                id: h.id,
                origin: h.origin,
                project: h.project,
                source_path: h.source_path,
                snippet: h.content.chars().take(200).collect(),
            })
            .collect()
    } else {
        // direct wiki read — origin/project don't exist in the wiki path, so empty (schema-compatible).
        wiki_recall_hits(s.wiki_dir().as_deref(), &req.query)?
            .into_iter()
            .map(|h| SearchHit {
                id: h.id,
                origin: String::new(),
                project: String::new(),
                source_path: h.source_path,
                snippet: h.snippet,
            })
            .collect()
    };
    Ok(Json(SearchResp { hits: mapped }))
}

/// wiki_recall call wrapper — empty result if wiki_dir is unset (graceful).
fn wiki_recall_hits(
    wiki_dir: Option<&Path>,
    query: &str,
) -> Result<Vec<wiki_recall::WikiHit>, AppError> {
    match wiki_dir {
        Some(dir) => Ok(wiki_recall::recall(dir, query, 5)?),
        None => Ok(Vec::new()),
    }
}

async fn handle_graph(
    State(s): State<AppState>,
    Json(req): Json<GraphReq>,
) -> Result<Json<GraphResp>, AppError> {
    let store = s.store.as_ref().ok_or_else(vector_disabled)?; // graph is pgvector-only
    let out = graph::query(store, &s.llm, &req.query).await?;
    Ok(Json(GraphResp {
        hit: out.hit,
        graph_neighbors: out.graph_neighbors,
        semantic_neighbors: out.semantic_neighbors,
    }))
}

async fn handle_audit(State(s): State<AppState>) -> Result<Json<audit::AuditStats>, AppError> {
    let store = s.store.as_ref().ok_or_else(vector_disabled)?; // ingest stats are pgvector-only
    let stats = audit::stats(store).await?;
    Ok(Json(stats))
}

/// Session distillation — takes text the host hook extracted, runs LLM distillation + scrub + raw note recording (SSOT).
/// If `DRUDGE_VAULT_DIR` is unset there is no recording target → error (the host absorbs it as a no-op).
async fn handle_distill(
    State(s): State<AppState>,
    Json(req): Json<DistillReq>,
) -> Result<Json<DistillResp>, AppError> {
    let Some(vault_root) = (*s.vault_dir).as_ref() else {
        return Err(AppError(anyhow::anyhow!(
            "DRUDGE_VAULT_DIR 미설정 — distill 기록 대상 없음"
        )));
    };
    let dreq = distill::DistillRequest {
        text: req.text,
        session_id: req.session_id,
        origin: req.origin,
        phase: req.phase,
        repo: req.repo,
        cwd: req.cwd,
    };
    let out = distill::run(&s.llm, vault_root, &dreq).await?;
    Ok(Json(DistillResp {
        written: out.written,
        filename: out.filename,
    }))
}

// ── MCP-over-HTTP (Nous Hermes Agent connection) ────────────────────────────
// JSON-RPC 2.0: initialize · tools/list · tools/call(recall). Notifications get 202 (no response).
// The `recall` tool = retrieve (vector+graph) → text → the agent retrieves from our self-augmenting KB.

const MCP_PROTOCOL_VERSION: &str = "2025-06-18";

async fn handle_mcp(State(s): State<AppState>, Json(req): Json<Value>) -> Response {
    let method = req
        .get("method")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if method.starts_with("notifications/") {
        return StatusCode::ACCEPTED.into_response(); // notifications get no response
    }
    let id = req.get("id").cloned().unwrap_or(Value::Null);
    let outcome = match method {
        "initialize" => Ok(mcp_initialize(&req)),
        "tools/list" => Ok(mcp_tools_list()),
        "ping" => Ok(json!({})),
        "tools/call" => mcp_call(&s, &req).await,
        other => Err((-32601_i32, format!("method not found: {other}"))),
    };
    let body = match outcome {
        Ok(result) => json!({"jsonrpc": "2.0", "id": id, "result": result}),
        Err((code, message)) => {
            json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
        }
    };
    Json(body).into_response()
}

/// Echo the protocolVersion the client sent (compatibility), or the default if absent.
fn mcp_initialize(req: &Value) -> Value {
    let pv = req
        .get("params")
        .and_then(|p| p.get("protocolVersion"))
        .and_then(Value::as_str)
        .unwrap_or(MCP_PROTOCOL_VERSION);
    json!({
        "protocolVersion": pv,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "drudge", "version": env!("CARGO_PKG_VERSION")}
    })
}

fn mcp_tools_list() -> Value {
    // Tools the agent (Nous Hermes Agent) uses to *drive* the engine. The engine systematizes the mechanical work
    // (lint·compile·ingest·embedding·graph), while the agent decides *when and what* to ingest/retrieve.
    json!({"tools": [
        {
            "name": "recall",
            "description": "사용자의 과거 작업 경험·결정·메모리를 자가증강 RAG(벡터+그래프)에서 회수한다. \
                            '전에 이거 어떻게 했지/결정했지' 류 기억이 필요할 때 사용.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "회수할 주제 또는 질문"}},
                "required": ["query"]
            }
        },
        {
            "name": "remember",
            "description": "지금 배운 것·결정·사실을 영속 메모리에 적재한다. vault/raw 에 노트로 기록되어 \
                            다음 sync 때 compile→임베딩→그래프로 흡수된다. 기록 후 회수(recall) 가능.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "기억할 내용(문제해결 서사·결정·사실)"},
                    "title": {"type": "string", "description": "선택. 노트 제목 한 줄"}
                },
                "required": ["text"]
            }
        },
        {
            "name": "sync",
            "description": "적재 파이프라인을 1회 돌린다: compile(raw→wiki 큐레이션) → 임베딩 → \
                            pgvector upsert → 그래프 추출. remember 로 쌓은 노트를 즉시 회수 가능하게 만든다.",
            "inputSchema": {"type": "object", "properties": {}}
        }
    ]})
}

/// tools/call dispatcher — routes by tool name. The entry point through which the agent drives the engine.
async fn mcp_call(s: &AppState, req: &Value) -> Result<Value, (i32, String)> {
    let params = req.get("params");
    let name = params
        .and_then(|p| p.get("name"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    let args = params.and_then(|p| p.get("arguments"));
    let text = match name {
        "recall" => mcp_recall(s, args).await?,
        "remember" => mcp_remember(s, args)?,
        "sync" => mcp_sync(s).await?,
        other => return Err((-32602, format!("unknown tool: {other}"))),
    };
    Ok(json!({"content": [{"type": "text", "text": text}], "isError": false}))
}

/// `recall` — vector+graph retrieval. Returns full chunks (no snippets) so the agent can synthesize the "why/how".
async fn mcp_recall(s: &AppState, args: Option<&Value>) -> Result<String, (i32, String)> {
    let query = args
        .and_then(|a| a.get("query"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    if query.is_empty() {
        return Err((-32602, "missing argument: query".to_owned()));
    }
    // vector on → vector+graph chunks. off → direct vault/wiki read snippets.
    let lines: Vec<(String, String)> = if let Some(store) = s.store.as_ref() {
        retrieve::retrieve(store, &s.llm, query, 5, &[])
            .await
            .map_err(|e| (-32603_i32, format!("retrieve: {e:#}")))?
            .into_iter()
            .map(|h| (h.source_path, h.content))
            .collect()
    } else {
        let dir = s.wiki_dir();
        let Some(dir) = dir.as_deref() else {
            return Ok("(vault 미설정 — 회수 대상 없음)".to_owned());
        };
        wiki_recall::recall(dir, query, 5)
            .map_err(|e| (-32603_i32, format!("wiki recall: {e:#}")))?
            .into_iter()
            .map(|h| (h.source_path, h.snippet))
            .collect()
    };
    if lines.is_empty() {
        return Ok("(회수된 경험 없음)".to_owned());
    }
    Ok(lines
        .iter()
        .map(|(path, body)| {
            let src = path.rsplit('/').next().unwrap_or(path.as_str());
            format!("- [{src}] {body}")
        })
        .collect::<Vec<_>>()
        .join("\n\n"))
}

/// `remember` — writes what the agent learned as a vault/raw note. Absorbed on the next `sync`.
/// Filename = content sha8 → idempotent on re-recording the same content (prevents duplicate notes). Synchronous IO (file write) only.
fn mcp_remember(s: &AppState, args: Option<&Value>) -> Result<String, (i32, String)> {
    let Some(vault_root) = (*s.vault_dir).as_ref() else {
        return Err((
            -32603,
            "DRUDGE_VAULT_DIR 미설정 — remember 기록 대상 없음".to_owned(),
        ));
    };
    let text = args
        .and_then(|a| a.get("text"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    if text.is_empty() {
        return Err((-32602, "missing argument: text".to_owned()));
    }
    let title = args
        .and_then(|a| a.get("title"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();

    let sha = {
        use sha2::{Digest, Sha256};
        let digest = Sha256::digest(text.as_bytes());
        hex::encode(&digest[..4]) // 8 hex chars
    };
    let raw_dir = vault_root.join("raw");
    std::fs::create_dir_all(&raw_dir).map_err(|e| (-32603_i32, format!("raw dir: {e}")))?;
    let path = raw_dir.join(format!("memo-{sha}.md"));
    let heading = if title.is_empty() {
        format!("# 메모 — {}", vault::today_utc())
    } else {
        format!("# {title}")
    };
    let body = format!("{heading}\n> origin: personal · via: agent(remember)\n\n{text}\n");
    std::fs::write(&path, body).map_err(|e| (-32603_i32, format!("raw note write: {e}")))?;
    Ok(format!(
        "기억함 → raw/memo-{sha}.md. sync 를 호출하면 compile→임베딩→회수가능 상태가 된다."
    ))
}

/// `sync` — one run of the ingest pipeline (compile→ingest→extract). The agent decides *when* to absorb.
async fn mcp_sync(s: &AppState) -> Result<String, (i32, String)> {
    let o = do_sync(
        s.store.as_deref(),
        &s.llm,
        &s.source_dirs,
        (*s.vault_dir).as_ref(),
    )
    .await
    .map_err(|e| (-32603_i32, format!("sync: {e:#}")))?;
    let compiled = o.compile.as_ref().map_or(0, |c| c.compiled + c.recompiled);
    let nodes = o.extract.problems
        + o.extract.solutions
        + o.extract.tools
        + o.extract.concepts
        + o.extract.attempts;
    Ok(format!(
        "sync 완료 — compile {compiled} · ingest(new {} updated {} chunks {}) · graph(nodes {nodes} edges {})",
        o.ingest.new, o.ingest.updated, o.ingest.chunks, o.extract.edges
    ))
}

async fn handle_sync(State(s): State<AppState>) -> Result<Json<SyncResp>, AppError> {
    let o = do_sync(
        s.store.as_deref(),
        &s.llm,
        &s.source_dirs,
        (*s.vault_dir).as_ref(),
    )
    .await?;
    let (c_raw, c_compiled, c_recompiled) = o
        .compile
        .as_ref()
        .map_or((0, 0, 0), |c| (c.total_raw, c.compiled, c.recompiled));
    Ok(Json(SyncResp {
        compile_total_raw: c_raw,
        compile_compiled: c_compiled,
        compile_recompiled: c_recompiled,
        ingest_new: o.ingest.new,
        ingest_updated: o.ingest.updated,
        ingest_deleted: o.ingest.deleted,
        ingest_chunks: o.ingest.chunks,
        extract_processed: o.extract.processed,
        extract_skipped: o.extract.skipped,
        extract_nodes: o.extract.problems
            + o.extract.solutions
            + o.extract.tools
            + o.extract.concepts
            + o.extract.attempts,
        extract_edges: o.extract.edges,
    }))
}

// ── one sync cycle (compile → ingest → extract) ─────────────────────────────

struct SyncOutcome {
    compile: Option<vault::CompileStats>,
    ingest: ingest::Stats,
    extract: extract::ExtractStats,
}

/// vault compile (raw→wiki). Graceful skip (None) when `vault_dir` is unset or the raw directory is absent.
/// A compile failure logs to stderr then returns None — ingest/extract (absorbing the existing wiki) still proceed (independent stages).
async fn run_compile_step(llm: &Llm, vault_dir: Option<&PathBuf>) -> Option<vault::CompileStats> {
    let vault_root = vault_dir?;
    let raw_dir = vault_root.join("raw");
    if !raw_dir.is_dir() {
        return None; // no distilled raw notes yet — nothing to compile (normal)
    }
    let today = vault::today_utc();
    match vault::run_compile(vault_root, &raw_dir, &today, llm).await {
        Ok(s) => {
            eprintln!(
                "[scheduler] compile: total_raw={} compiled={} recompiled={} skipped={}",
                s.total_raw, s.compiled, s.recompiled, s.skipped
            );
            Some(s)
        }
        Err(e) => {
            eprintln!("[scheduler] compile error: {e:#}");
            None
        }
    }
}

/// Self-augmenting cycle: raw→wiki compile → (only when vector is on) source→DB ingest → graph extract.
/// When `store=None` (DRUDGE_VECTOR=off), **compile only** — the wiki is first-class memory, so it alone supports recall.
/// Shared by HTTP `/sync` and the background scheduler (SSOT).
async fn do_sync(
    store: Option<&Store>,
    llm: &Llm,
    source_dirs: &[String],
    vault_dir: Option<&PathBuf>,
) -> Result<SyncOutcome> {
    let compile = run_compile_step(llm, vault_dir).await;
    let Some(store) = store else {
        // vector off — wiki compile alone is enough (no embedding/graph).
        return Ok(SyncOutcome {
            compile,
            ingest: ingest::Stats::default(),
            extract: extract::ExtractStats::default(),
        });
    };
    let ingest = ingest::run(store, llm, source_dirs).await?;
    let extract = extract::run(store, llm).await?;
    // Re-extraction deletes only old edges and leaves nodes orphaned (accumulates every sync → node explosion).
    // GC orphan semantic nodes at the end of every sync — keeps the graph lean (SSOT hygiene).
    match store.gc_orphans().await {
        Ok(g) => eprintln!("[scheduler] gc orphans: {}", g.total()),
        Err(e) => eprintln!("[scheduler] gc 경고(무시): {e:#}"),
    }
    // graph → Obsidian projection: doc↔doc relations as wiki relates_to wikilinks (only when vault exists).
    // Auxiliary visualization stage — on failure it does not break the core sync (ingest/extract), just logs.
    if let Some(vd) = vault_dir {
        match vault::project_links(store, vd, 6).await {
            Ok(n) => eprintln!("[scheduler] graph→obsidian: {n} wiki relates_to 갱신"),
            Err(e) => eprintln!("[scheduler] project_links 경고(무시): {e:#}"),
        }
    }
    Ok(SyncOutcome {
        compile,
        ingest,
        extract,
    })
}

// ── background scheduler ────────────────────────────────────────────────────

async fn run_sync(
    store: Option<&Store>,
    llm: &Llm,
    source_dirs: &[String],
    vault_dir: Option<&PathBuf>,
) {
    match do_sync(store, llm, source_dirs, vault_dir).await {
        Ok(o) => eprintln!(
            "[scheduler] sync done — ingest(new={} updated={} deleted={} chunks={}) extract(nodes={} edges={})",
            o.ingest.new,
            o.ingest.updated,
            o.ingest.deleted,
            o.ingest.chunks,
            o.extract.problems
                + o.extract.solutions
                + o.extract.tools
                + o.extract.concepts
                + o.extract.attempts,
            o.extract.edges
        ),
        Err(e) => eprintln!("[scheduler] sync error: {e:#}"),
    }
}

fn spawn_scheduler(
    store: Option<Arc<Store>>,
    llm: Arc<Llm>,
    source_dirs: Arc<Vec<String>>,
    vault_dir: Arc<Option<PathBuf>>,
) {
    // `.max(1)` — `DRUDGE_SYNC_HOURS=0` would make a zero Duration, and
    // tokio::time::interval panics on a zero period. Clamp to ≥1h.
    let sync_hours: u64 = std::env::var("DRUDGE_SYNC_HOURS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(4)
        .max(1);
    let interval = Duration::from_secs(sync_hours * 3600);

    tokio::spawn(async move {
        let store_ref = store.as_deref();
        // run once immediately at startup (compile only if vector off — refreshes wiki).
        eprintln!(
            "[scheduler] startup sync (interval={sync_hours}h, vector={})",
            store.is_some()
        );
        run_sync(store_ref, &llm, &source_dirs, (*vault_dir).as_ref()).await;

        let mut ticker = tokio::time::interval(interval);
        ticker.tick().await; // the first tick is immediate — discard it (already ran above)
        loop {
            ticker.tick().await;
            eprintln!("[scheduler] periodic sync");
            run_sync(store_ref, &llm, &source_dirs, (*vault_dir).as_ref()).await;
        }
    });
}

// ── entry point ─────────────────────────────────────────────────────────────

pub async fn run(store: Option<Store>, llm: Llm) -> Result<()> {
    let home = std::env::var("HOME").unwrap_or_default();
    let dirs_env = std::env::var("DRUDGE_SOURCE_DIRS")
        .unwrap_or_else(|_| format!("{home}/.claude/projects:{home}/oh-my-boring/data/notes"));
    let source_dirs: Vec<String> = dirs_env.split(':').map(str::to_owned).collect();

    // vault root — when set, sync includes the raw→wiki compile stage.
    let vault_dir: Option<PathBuf> = std::env::var("DRUDGE_VAULT_DIR").ok().map(PathBuf::from);

    let addr = std::env::var("DRUDGE_HTTP_ADDR").unwrap_or_else(|_| "0.0.0.0:7700".to_owned());

    let state = AppState {
        store: store.map(Arc::new),
        llm: Arc::new(llm),
        source_dirs: Arc::new(source_dirs),
        vault_dir: Arc::new(vault_dir),
    };

    spawn_scheduler(
        state.store.clone(),
        Arc::clone(&state.llm),
        Arc::clone(&state.source_dirs),
        Arc::clone(&state.vault_dir),
    );

    let router = axum::Router::new()
        .route("/health", get(health))
        .route("/ask", post(handle_ask))
        .route("/brief", post(handle_brief))
        .route("/search", post(handle_search))
        .route("/graph", post(handle_graph))
        .route("/audit", get(handle_audit))
        .route("/distill", post(handle_distill)) // session distillation (host hook → raw note SSOT)
        .route("/sync", post(handle_sync))
        .route("/mcp", post(handle_mcp)) // MCP-over-HTTP (Nous Hermes Agent calls via the recall tool)
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .map_err(|e| anyhow::anyhow!("bind {addr}: {e}"))?;
    eprintln!("[serve] listening on {addr}");

    axum::serve(listener, router)
        .await
        .map_err(|e| anyhow::anyhow!("axum serve: {e}"))
}
