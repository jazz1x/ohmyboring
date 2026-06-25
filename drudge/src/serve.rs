//! Serve — HTTP resident daemon (axum) + background sync scheduler.
//!
//! Cross-reference: design decision D3 (write door gated / read door open).
//!
//! Architecture:
//! - Shares `Store` + `Llm` via `Arc` (the Postgres client supports concurrent use).
//! - axum router: /health · /ask · /brief · /search · /graph · /audit · /sync
//! - Background scheduler: `BORING_SYNC_HOURS` (default 4h) interval + one immediate run at startup.
//! - Error propagation: `AppError` (anyhow wrapper) → HTTP 500, JSON body.
use std::path::{Path, PathBuf};
use std::sync::Arc;

use tokio::sync::Mutex;

use anyhow::Result;
use axum::Json;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use serde::{Deserialize, Serialize};

use crate::config;
use crate::llm::Llm;
use crate::store::Store;
use crate::wiki_recall;

mod http;
mod mcp;
mod scheduler;

// ── shared state ──────────────────────────────────────────────────────────────

#[derive(Clone)]
pub struct AppState {
    /// pgvector backend. If `None`, `BORING_VECTOR=off` — retrieval is direct vault/wiki reads (wiki_recall),
    /// and remember writes the wiki note as first-class memory (no embed/graph). Vector/graph-dependent endpoints reject explicitly.
    pub(crate) store: Option<Arc<Store>>,
    pub(crate) llm: Arc<Llm>,
    /// vault root (`BORING_VAULT_DIR`). The remember target (`<vault>/wiki/wiki-NNNN.md`) + the relates_to projection root.
    pub(crate) vault_dir: Arc<Option<PathBuf>>,
    /// Policy config (`boring.json`).
    pub(crate) cfg: Arc<config::BoringConfig>,
    /// Resolved path to the loaded config, so `classify_repo` writes back to the same file.
    pub(crate) cfg_path: Arc<Option<PathBuf>>,
    /// Serializes startup, periodic, and HTTP-triggered syncs so they never overlap.
    /// `/sync` waits for an in-flight startup sync and returns its actual outcome.
    pub(crate) sync_lock: Arc<Mutex<()>>,
    /// Resident wiki recall index (BORING_VECTOR=off path). Persists parsed/lowercased notes across
    /// requests; `refresh()` re-reads only mtime-changed files, so repeated `/search` (the recall hook
    /// fires per prompt) scores in memory instead of re-reading the whole corpus. std Mutex — the
    /// critical section is sync (refresh+score) and never held across an await.
    pub(crate) wiki_index: Arc<std::sync::Mutex<wiki_recall::WikiIndex>>,
    /// Last successful compact time, shared with scheduler so manual `/compact` resets the window.
    pub(crate) last_compact: Arc<Mutex<Option<std::time::Instant>>>,
}

impl AppState {
    /// vault/wiki directory (the retrieval target for `BORING_VECTOR=off`). None if vault is unset.
    pub(crate) fn wiki_dir(&self) -> Option<PathBuf> {
        (*self.vault_dir).as_ref().map(|v| v.join("wiki"))
    }

    /// Cached wiki recall: refresh the resident index (mtime-incremental — only changed files are
    /// re-read, so this stays honest, not stale) then score in memory. Empty when the vault is unset.
    pub(crate) fn wiki_recall(&self, query: &str, k: usize) -> Result<Vec<wiki_recall::WikiHit>> {
        let Some(dir) = self.wiki_dir() else {
            return Ok(Vec::new());
        };
        // Recover a poisoned lock instead of unwrapping (a prior panic must not wedge recall).
        let mut idx = self
            .wiki_index
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        idx.refresh(&dir)?;
        Ok(idx.search(query, k))
    }
}

/// Fire-and-forget query logging. Latency and result context are recorded for
/// memory-utility analytics; failures are logged to stderr and never fail the request.
#[allow(clippy::needless_borrow)] // tokio-postgres needs &&str to coerce to &dyn ToSql.
pub(crate) fn spawn_query_log(
    store: Option<Arc<Store>>,
    endpoint: &'static str,
    query: String,
    hit_paths: Vec<String>,
    sources: Vec<String>,
    answer_snippet: String,
    elapsed: std::time::Duration,
) {
    let Some(store) = store else {
        return;
    };
    tokio::spawn(async move {
        let latency_ms = i32::try_from(elapsed.as_millis()).ok();
        if let Err(e) = store
            .log_query(
                &endpoint,
                &query,
                &hit_paths,
                &sources,
                &answer_snippet,
                latency_ms,
            )
            .await
        {
            eprintln!("[query_log] {e:#}");
        }
    });
}

// ── error type (ROP: AppError → HTTP 500) ───────────────────────────────────

pub(crate) struct AppError(anyhow::Error);

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
pub(crate) struct AskReq {
    pub(crate) question: String,
}

#[derive(Serialize)]
pub(crate) struct AskResp {
    pub(crate) answer: String,
    pub(crate) sources: Vec<String>,
}

#[derive(Deserialize)]
pub(crate) struct SearchReq {
    pub(crate) query: String,
    #[serde(default = "default_max_results")]
    pub(crate) max_results: usize,
    #[serde(default = "default_max_tokens")]
    pub(crate) max_tokens: usize,
}

fn default_max_results() -> usize {
    5
}

fn default_max_tokens() -> usize {
    2000
}

#[derive(Serialize)]
pub(crate) struct SearchHit {
    pub(crate) id: String,
    pub(crate) origin: String,
    pub(crate) project: String,
    pub(crate) source_path: String,
    pub(crate) snippet: String,
}

#[derive(Serialize)]
pub(crate) struct SearchResp {
    pub(crate) hits: Vec<SearchHit>,
}

#[derive(Deserialize)]
pub(crate) struct GraphReq {
    pub(crate) query: String,
}

#[derive(Serialize)]
pub(crate) struct GraphResp {
    pub(crate) hit: String,
    pub(crate) graph_neighbors: Vec<String>,
    pub(crate) semantic_neighbors: Vec<String>,
}

#[derive(Serialize)]
pub(crate) struct SyncResp {
    pub(crate) ingest_new: usize,
    pub(crate) ingest_updated: usize,
    pub(crate) ingest_deleted: usize,
    pub(crate) ingest_chunks: usize,
    pub(crate) graph_tools: usize,
    pub(crate) graph_concepts: usize,
    pub(crate) graph_claims: usize,
    pub(crate) graph_edges: usize,
    /// Total corpus size after sync (independent of whether this run produced deltas). `null` when the
    /// post-sync audit was unavailable — reported honestly as "not measured", never fabricated as 0.
    pub(crate) total_chunks: Option<usize>,
    pub(crate) total_edges: Option<usize>,
}

#[derive(Serialize)]
pub(crate) struct CompactResp {
    pub(crate) vacuum_ms: u128,
    pub(crate) reindex_ms: u128,
    pub(crate) prune_query_log: usize,
    pub(crate) gc_tool: usize,
    pub(crate) gc_concept: usize,
    pub(crate) total_ms: u128,
}

#[derive(Deserialize)]
pub(crate) struct QueryLogReq {
    #[serde(default = "default_query_log_limit")]
    pub(crate) limit: i64,
}

fn default_query_log_limit() -> i64 {
    50
}

#[derive(Serialize)]
pub(crate) struct QueryLogResp {
    pub(crate) entries: Vec<QueryLogEntry>,
}

#[derive(Serialize)]
pub(crate) struct QueryLogEntry {
    pub(crate) id: i32,
    pub(crate) created_at: String,
    pub(crate) endpoint: String,
    pub(crate) query: String,
    pub(crate) hit_paths: Vec<String>,
    pub(crate) sources: Vec<String>,
    pub(crate) answer_snippet: String,
    pub(crate) latency_ms: Option<i32>,
}

// ── shared handler helpers ──────────────────────────────────────────────────

/// Whether a sync/remember/forget is mid-flight (holds the sync lock). An enum, not a string —
/// the two states are closed at the type so an impossible third value can't exist (Layer 1: ADT).
#[derive(Serialize, Clone, Copy)]
#[serde(rename_all = "lowercase")]
pub(crate) enum SyncState {
    Running,
    Idle,
}

#[derive(Serialize)]
pub(crate) struct HealthResp {
    pub(crate) status: &'static str,
    pub(crate) vector: bool,
    /// "running" while a sync/remember/forget holds the sync lock, else "idle". Lets `make up` callers
    /// tell a still-warming corpus (empty results are expected) from a genuinely empty one.
    pub(crate) sync: SyncState,
    /// Wiki note count (vault/wiki/*.md) — the corpus size in both modes. `null` when the vault is
    /// unset/unreadable (kept best-effort so /health stays a liveness probe).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) corpus_count: Option<usize>,
}

/// Best-effort count of wiki notes (`vault/wiki/*.md`). `None` on any IO error — `/health` must stay a
/// liveness signal, so an unreadable/absent vault reports "unknown" (null), never fails the probe.
pub(crate) fn count_wiki_notes(wiki_dir: &Path) -> Option<usize> {
    let entries = std::fs::read_dir(wiki_dir).ok()?;
    Some(
        entries
            .filter_map(Result::ok)
            .filter(|e| e.path().extension().is_some_and(|x| x == "md"))
            .count(),
    )
}

/// The explicit rejection (not silence) that vector/graph-dependent endpoints return under `BORING_VECTOR=off`.
pub(crate) fn vector_disabled() -> AppError {
    AppError(anyhow::anyhow!(
        "BORING_VECTOR=off — this feature requires the vector backend (pgvector). Set BORING_VECTOR=on and start Postgres."
    ))
}

/// The same rejection mapped into the MCP `(code, message)` tuple — for vector-only tools
/// (neighbors/claims/corpus_status). SSOT with `vector_disabled`; never `unwrap` the store (ROP).
pub(crate) fn vec_off_rpc() -> (i32, String) {
    (-32603, format!("{:#}", vector_disabled().0))
}

/// Hard ceiling on agent-supplied recall budget to prevent token/DoS explosions.
pub(crate) const MCP_MAX_RESULTS: usize = 50;
pub(crate) const MCP_MAX_TOKENS: usize = 16_384;

// ── entry point ─────────────────────────────────────────────────────────────

pub async fn run(store: Option<Store>, llm: Llm, cfg: config::BoringConfig) -> Result<()> {
    // vault root — when set, sync includes the raw→wiki compile stage.
    let vault_dir: Option<PathBuf> = config::env_set("BORING_VAULT_DIR").map(PathBuf::from);

    // Remember which config file we loaded so `classify_repo` writes back to the same file.
    let cfg_path = config::discover_path();

    let addr = config::env_set("BORING_HTTP_ADDR").unwrap_or_else(|| "0.0.0.0:7700".to_owned());

    let last_compact = Arc::new(Mutex::new(None));
    let state = AppState {
        store: store.map(Arc::new),
        llm: Arc::new(llm),
        vault_dir: Arc::new(vault_dir),
        cfg: Arc::new(cfg),
        cfg_path: Arc::new(cfg_path),
        sync_lock: Arc::new(Mutex::new(())),
        last_compact: Arc::clone(&last_compact),
        wiki_index: Arc::new(std::sync::Mutex::new(wiki_recall::WikiIndex::default())),
    };

    scheduler::spawn_scheduler(
        state.store.clone(),
        Arc::clone(&state.llm),
        Arc::clone(&state.vault_dir),
        Arc::clone(&state.cfg),
        Arc::clone(&state.sync_lock),
        Arc::clone(&last_compact),
    );
    // cfg_path is only used by the HTTP/MCP handlers; the scheduler does not need it.

    let router = axum::Router::new()
        .route("/health", get(http::health))
        .route("/ask", post(http::handle_ask))
        .route("/brief", post(http::handle_brief))
        .route("/search", post(http::handle_search))
        .route("/graph", post(http::handle_graph))
        .route("/audit", get(http::handle_audit))
        .route("/query-log", get(http::handle_query_log))
        .route("/sync", post(http::handle_sync))
        .route("/compact", post(http::handle_compact))
        .route("/mcp", get(mcp::handle_mcp_get).post(mcp::handle_mcp)) // MCP-over-HTTP (Streamable HTTP: GET SSE + POST JSON-RPC)
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .map_err(|e| anyhow::anyhow!("bind {addr}: {e}"))?;
    eprintln!("[serve] listening on {addr}");

    axum::serve(listener, router)
        .await
        .map_err(|e| anyhow::anyhow!("axum serve: {e}"))
}
