//! Serve — HTTP resident daemon (axum) + background sync scheduler.
//!
//! 아키텍처:
//! - `Store` + `Ollama` 를 `Arc` 로 공유 (Postgres client 는 concurrent 사용 지원).
//! - axum 라우터: /health · /ask · /search · /graph · /audit · /sync
//! - 백그라운드 스케줄러: `DRUDGE_SYNC_HOURS`(기본 4h) 주기 + 기동 즉시 1회 실행.
//! - 에러 전파: `AppError` (anyhow wrapper) → HTTP 500, JSON body.
use std::path::PathBuf;
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
use crate::ollama::Ollama;
use crate::retrieve;
use crate::store::Store;
use crate::vault;

// ── 공유 상태 ─────────────────────────────────────────────────────────────────

#[derive(Clone)]
pub struct AppState {
    store: Arc<Store>,
    ollama: Arc<Ollama>,
    source_dirs: Arc<Vec<String>>,
    /// vault 루트(`DRUDGE_VAULT_DIR`). `Some`이면 sync 시 raw→wiki compile 수행.
    vault_dir: Arc<Option<PathBuf>>,
}

// ── 에러 타입 (ROP: AppError → HTTP 500) ────────────────────────────────────

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

// ── 요청/응답 타입 ───────────────────────────────────────────────────────────

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

/// `personal` 기본값 — 호스트가 origin 미지정 시(회사 토큰 미설정) 사용.
fn default_origin() -> String {
    "personal".to_owned()
}

#[derive(Deserialize)]
struct DistillReq {
    /// 세션 트랜스크립트에서 호스트가 추출한 평문.
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
    /// KEEP/SKIP 게이트 결과 — false 면 저장 가치 없어 폐기(에러 아님).
    written: bool,
    /// 기록된 raw 노트 파일명. 호스트가 RAW_DIR 와 조인해 mtime 보정 + sync 트리거.
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

// ── 핸들러 ───────────────────────────────────────────────────────────────────

async fn health() -> &'static str {
    "ok"
}

async fn handle_ask(
    State(s): State<AppState>,
    Json(req): Json<AskReq>,
) -> Result<Json<AskResp>, AppError> {
    let out = ask::answer(&s.store, &s.ollama, &req.question, &[]).await?;
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// 최신우선 브리핑 — 질문 없음(최근성 회수). cron 아침 브리핑이 호출.
async fn handle_brief(State(s): State<AppState>) -> Result<Json<AskResp>, AppError> {
    let out = ask::brief(&s.store, &s.ollama, &[]).await?;
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

async fn handle_search(
    State(s): State<AppState>,
    Json(req): Json<SearchReq>,
) -> Result<Json<SearchResp>, AppError> {
    let hits = retrieve::retrieve(&s.store, &s.ollama, &req.query, 5, &[]).await?;
    let mapped: Vec<SearchHit> = hits
        .into_iter()
        .map(|h| SearchHit {
            id: h.id,
            origin: h.origin,
            project: h.project,
            source_path: h.source_path,
            snippet: h.content.chars().take(200).collect(),
        })
        .collect();
    Ok(Json(SearchResp { hits: mapped }))
}

async fn handle_graph(
    State(s): State<AppState>,
    Json(req): Json<GraphReq>,
) -> Result<Json<GraphResp>, AppError> {
    let out = graph::query(&s.store, &s.ollama, &req.query).await?;
    Ok(Json(GraphResp {
        hit: out.hit,
        graph_neighbors: out.graph_neighbors,
        semantic_neighbors: out.semantic_neighbors,
    }))
}

async fn handle_audit(State(s): State<AppState>) -> Result<Json<audit::AuditStats>, AppError> {
    let stats = audit::stats(&s.store).await?;
    Ok(Json(stats))
}

/// 세션 증류 — 호스트 훅이 추출한 텍스트를 받아 LLM 증류 + 스크럽 + raw 노트 기록(SSOT).
/// `DRUDGE_VAULT_DIR` 미설정이면 기록 대상 없음 → 에러(호스트가 no-op 흡수).
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
    let out = distill::run(&s.ollama, vault_root, &dreq).await?;
    Ok(Json(DistillResp {
        written: out.written,
        filename: out.filename,
    }))
}

// ── MCP-over-HTTP (Nous Hermes Agent 연결) ──────────────────────────────────
// JSON-RPC 2.0: initialize · tools/list · tools/call(recall). 알림은 202(응답 없음).
// `recall` 툴 = retrieve(벡터+그래프) → 텍스트 → 에이전트가 우리 자가증강 KB를 회수.

const MCP_PROTOCOL_VERSION: &str = "2025-06-18";

async fn handle_mcp(State(s): State<AppState>, Json(req): Json<Value>) -> Response {
    let method = req
        .get("method")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if method.starts_with("notifications/") {
        return StatusCode::ACCEPTED.into_response(); // 알림은 응답 없음
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

/// 클라이언트가 보낸 protocolVersion 을 echo(호환), 없으면 기본값.
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
    // 에이전트(Nous Hermes Agent)가 엔진을 *모는* 도구들. 엔진은 기계작업(lint·compile·
    // ingest·임베딩·그래프)을 시스템화해 두고, *언제 무엇을* 적재/회수할지는 에이전트가 결정.
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

/// tools/call 디스패처 — 툴 이름으로 라우팅. 에이전트가 엔진을 모는 진입점.
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

/// `recall` — 벡터+그래프 회수. 풀 청크 반환(스니펫 금지) → 에이전트가 '왜/어떻게'까지 합성.
async fn mcp_recall(s: &AppState, args: Option<&Value>) -> Result<String, (i32, String)> {
    let query = args
        .and_then(|a| a.get("query"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    if query.is_empty() {
        return Err((-32602, "missing argument: query".to_owned()));
    }
    let hits = retrieve::retrieve(&s.store, &s.ollama, query, 5, &[])
        .await
        .map_err(|e| (-32603_i32, format!("retrieve: {e:#}")))?;
    if hits.is_empty() {
        return Ok("(회수된 경험 없음)".to_owned());
    }
    Ok(hits
        .iter()
        .map(|h| {
            let src = h
                .source_path
                .rsplit('/')
                .next()
                .unwrap_or(h.source_path.as_str());
            format!("- [{src}] {}", h.content)
        })
        .collect::<Vec<_>>()
        .join("\n\n"))
}

/// `remember` — 에이전트가 배운 것을 vault/raw 노트로 적재(쓰기). 다음 `sync` 때 흡수.
/// 파일명 = 내용 sha8 → 같은 내용 재기록 시 멱등(중복 노트 방지). 동기 IO(파일 쓰기)뿐.
fn mcp_remember(s: &AppState, args: Option<&Value>) -> Result<String, (i32, String)> {
    let Some(vault_root) = (*s.vault_dir).as_ref() else {
        return Err((
            -32603,
            "HERMES_VAULT_DIR 미설정 — remember 기록 대상 없음".to_owned(),
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

/// `sync` — 적재 파이프라인 1회(compile→ingest→extract). 에이전트가 *언제* 흡수할지 결정.
async fn mcp_sync(s: &AppState) -> Result<String, (i32, String)> {
    let o = do_sync(&s.store, &s.ollama, &s.source_dirs, (*s.vault_dir).as_ref())
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
    let o = do_sync(&s.store, &s.ollama, &s.source_dirs, (*s.vault_dir).as_ref()).await?;
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

// ── 동기화 1사이클 (compile → ingest → extract) ──────────────────────────────

struct SyncOutcome {
    compile: Option<vault::CompileStats>,
    ingest: ingest::Stats,
    extract: extract::ExtractStats,
}

/// vault compile(raw→wiki). `vault_dir` 미설정이거나 raw 디렉터리 부재 시 graceful skip(None).
/// compile 실패는 stderr 로깅 후 None — ingest/extract(기존 wiki 흡수)는 계속 진행(독립 단계).
async fn run_compile_step(
    ollama: &Ollama,
    vault_dir: Option<&PathBuf>,
) -> Option<vault::CompileStats> {
    let vault_root = vault_dir?;
    let raw_dir = vault_root.join("raw");
    if !raw_dir.is_dir() {
        return None; // 증류된 raw 노트 아직 없음 — 컴파일 대상 없음(정상)
    }
    let today = vault::today_utc();
    match vault::run_compile(vault_root, &raw_dir, &today, ollama).await {
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

/// 자가증강 사이클: raw→wiki compile → 소스→DB ingest → 시맨틱 그래프 extract.
/// HTTP `/sync` 와 백그라운드 스케줄러 공용(SSOT).
async fn do_sync(
    store: &Store,
    ollama: &Ollama,
    source_dirs: &[String],
    vault_dir: Option<&PathBuf>,
) -> Result<SyncOutcome> {
    let compile = run_compile_step(ollama, vault_dir).await;
    let ingest = ingest::run(store, ollama, source_dirs).await?;
    let extract = extract::run(store, ollama).await?;
    // 재추출은 옛 엣지만 지우고 노드는 고아로 남긴다(매 sync 누적 → 노드 폭발).
    // 매 sync 끝에 고아 시맨틱 노드 GC — 그래프를 마른 상태로 유지(SSOT 위생).
    match store.gc_orphans().await {
        Ok(g) => eprintln!("[scheduler] gc orphans: {}", g.total()),
        Err(e) => eprintln!("[scheduler] gc 경고(무시): {e:#}"),
    }
    // 그래프 → Obsidian 투영: doc↔doc 관계를 wiki relates_to 위키링크로(vault 있을 때만).
    // 보조 시각화 단계 — 실패해도 핵심 sync(ingest/extract)는 깨지 않고 로그만.
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

// ── 백그라운드 스케줄러 ───────────────────────────────────────────────────────

async fn run_sync(
    store: &Store,
    ollama: &Ollama,
    source_dirs: &[String],
    vault_dir: Option<&PathBuf>,
) {
    match do_sync(store, ollama, source_dirs, vault_dir).await {
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
    store: Arc<Store>,
    ollama: Arc<Ollama>,
    source_dirs: Arc<Vec<String>>,
    vault_dir: Arc<Option<PathBuf>>,
) {
    let sync_hours: u64 = std::env::var("DRUDGE_SYNC_HOURS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(4);
    let interval = Duration::from_secs(sync_hours * 3600);

    tokio::spawn(async move {
        // 기동 즉시 1회 실행
        eprintln!("[scheduler] startup sync (interval={sync_hours}h)");
        run_sync(&store, &ollama, &source_dirs, (*vault_dir).as_ref()).await;

        let mut ticker = tokio::time::interval(interval);
        ticker.tick().await; // 첫 tick 은 즉시 — 버림 (위에서 이미 실행)
        loop {
            ticker.tick().await;
            eprintln!("[scheduler] periodic sync");
            run_sync(&store, &ollama, &source_dirs, (*vault_dir).as_ref()).await;
        }
    });
}

// ── 진입점 ──────────────────────────────────────────────────────────────────

pub async fn run(store: Store, ollama: Ollama) -> Result<()> {
    let home = std::env::var("HOME").unwrap_or_default();
    let dirs_env = std::env::var("DRUDGE_SOURCE_DIRS")
        .unwrap_or_else(|_| format!("{home}/.claude/projects:{home}/oh-my-boring/data/notes"));
    let source_dirs: Vec<String> = dirs_env.split(':').map(str::to_owned).collect();

    // vault 루트 — 설정 시 sync 가 raw→wiki compile 단계를 포함한다.
    let vault_dir: Option<PathBuf> = std::env::var("DRUDGE_VAULT_DIR").ok().map(PathBuf::from);

    let addr = std::env::var("DRUDGE_HTTP_ADDR").unwrap_or_else(|_| "0.0.0.0:7700".to_owned());

    let state = AppState {
        store: Arc::new(store),
        ollama: Arc::new(ollama),
        source_dirs: Arc::new(source_dirs),
        vault_dir: Arc::new(vault_dir),
    };

    spawn_scheduler(
        Arc::clone(&state.store),
        Arc::clone(&state.ollama),
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
        .route("/distill", post(handle_distill)) // 세션 증류(호스트 훅 → raw 노트 SSOT)
        .route("/sync", post(handle_sync))
        .route("/mcp", post(handle_mcp)) // MCP-over-HTTP (Nous Hermes Agent 가 recall 툴로 호출)
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .map_err(|e| anyhow::anyhow!("bind {addr}: {e}"))?;
    eprintln!("[serve] listening on {addr}");

    axum::serve(listener, router)
        .await
        .map_err(|e| anyhow::anyhow!("axum serve: {e}"))
}
