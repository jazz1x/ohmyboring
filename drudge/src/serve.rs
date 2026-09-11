//! Serve — HTTP resident daemon (axum) + background sync scheduler.
//!
//! Cross-reference: design decision D3 (write door gated / read door open).
//!
//! Architecture:
//! - Shares `Store` + `Llm` via `Arc` (the Postgres client supports concurrent use).
//! - axum router: /health · /ask · /brief · /search · /graph · /audit · /sync
//! - Background scheduler: `BORING_SYNC_HOURS` (default 4h) interval + one immediate run at startup.
//! - Error propagation: `AppError` → explicit HTTP status + JSON body.
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU8, Ordering};

use tokio::sync::Mutex;

use anyhow::Result;
use axum::Json;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::code_index::CodeIndexStore;
use crate::config;
use crate::llm::Llm;
use crate::pii;
use crate::store::{LoggedHit, Store};
use crate::wiki_recall;

mod http;
mod mcp;
mod scheduler;

// ── shared state ──────────────────────────────────────────────────────────────

/// Last-observed DB health state for /health transition logging. Stored as an AtomicU8 so concurrent
/// /health probes can detect a flip with a single atomic RMW — no load-then-store race.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum DbHealthState {
    Unknown,
    Healthy,
    Unhealthy,
}

impl DbHealthState {
    const fn to_u8(self) -> u8 {
        match self {
            Self::Unknown => 0,
            Self::Healthy => 1,
            Self::Unhealthy => 2,
        }
    }

    const fn from_u8(value: u8) -> Self {
        match value {
            1 => Self::Healthy,
            2 => Self::Unhealthy,
            _ => Self::Unknown,
        }
    }
}

#[derive(Clone)]
pub struct AppState {
    /// pgvector backend. If `None`, `BORING_VECTOR=off` — retrieval is direct vault/wiki reads (wiki_recall),
    /// and remember writes the wiki note as first-class memory (no embed/graph). Vector/graph-dependent endpoints reject explicitly.
    pub(crate) store: Option<Arc<Store>>,
    /// Explicit source-code corpus. It is never backed by the vault/wiki or memory tables.
    pub(crate) code_index: Option<Arc<CodeIndexStore>>,
    pub(crate) llm: Arc<Llm>,
    /// vault root (`BORING_VAULT_DIR`). The remember target (`<vault>/wiki/wiki-NNNN.md`) + the relates_to projection root.
    pub(crate) vault_dir: Arc<Option<PathBuf>>,
    /// PII / sensitive-data gate. None when no rule files are present.
    pub(crate) pii: Arc<Option<pii::PiiScanner>>,
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
    /// The last compact failure, or `None` when the last attempt succeeded. `REINDEX CONCURRENTLY`
    /// had been failing on every run for weeks against a container's default 64 MB of /dev/shm,
    /// and the only trace was `eprintln!` in the docker log: `/health` did not carry it, `doctor`
    /// did not ask, and the scheduler reset its window as though the run had worked. A failure
    /// nothing can observe is indistinguishable from no failure.
    pub(crate) compact_failure: Arc<Mutex<Option<CompactFailure>>>,
    /// Last observed DB health state for transition logging. `Unknown` until the first /health probe.
    /// Wrapped in Arc so AppState remains Clone while the atomic is shared across cloned state handles.
    pub(crate) db_healthy_last: Arc<AtomicU8>,
}

impl AppState {
    /// vault/wiki directory (the retrieval target for `BORING_VECTOR=off`). None if vault is unset.
    pub(crate) fn wiki_dir(&self) -> Option<PathBuf> {
        (*self.vault_dir).as_ref().map(|v| v.join("wiki"))
    }

    /// Cached wiki recall: refresh the resident index (mtime-incremental — only changed files are
    /// re-read, so this stays honest, not stale) then score in memory. Empty when the vault is unset.
    pub(crate) fn wiki_recall(
        &self,
        query: &str,
        k: usize,
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Result<Vec<wiki_recall::WikiHit>> {
        let Some(dir) = self.wiki_dir() else {
            return Ok(Vec::new());
        };
        // Recover a poisoned lock instead of unwrapping (a prior panic must not wedge recall).
        let mut idx = self
            .wiki_index
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        idx.refresh(&dir)?;
        Ok(idx.search(query, k, project, since_hours))
    }

    /// Probe all resident DB clients and return `(db_healthy, optional_error_text)`.
    /// `db_healthy` is `None` when no DB client is configured (vector off + code_index off).
    /// `Some(false)` means at least one configured client failed its liveness probe.
    pub(crate) async fn probe_db_health(&self) -> (Option<bool>, Option<String>) {
        let mut errors = Vec::new();

        if let Some(store) = self.store.as_ref()
            && let Err(error) = store.liveness_probe().await
        {
            errors.push(format!("store: {error:#}"));
        }

        if let Some(code_index) = self.code_index.as_ref()
            && let Err(error) = code_index.liveness_probe().await
        {
            errors.push(format!("code_index: {error:#}"));
        }

        if self.store.is_none() && self.code_index.is_none() {
            (None, None)
        } else if errors.is_empty() {
            (Some(true), None)
        } else {
            (Some(false), Some(errors.join("; ")))
        }
    }

    /// Update the atomic last-observed state and emit a transition log line only on flip.
    /// Returns the status string for the HTTP response ("ok" or "degraded").
    pub(crate) async fn check_db_health(&self) -> (Option<bool>, &'static str) {
        let (db_healthy, error_text) = self.probe_db_health().await;
        let next_state = DbHealthState::from_option(db_healthy);
        let prev_state = DbHealthState::from_u8(
            self.db_healthy_last
                .swap(next_state.to_u8(), Ordering::SeqCst),
        );
        if let Some(log) = transition_log(prev_state, next_state, error_text.as_deref()) {
            eprintln!("{log}");
        }
        let status = if db_healthy == Some(false) {
            "degraded"
        } else {
            "ok"
        };
        (db_healthy, status)
    }
}

impl DbHealthState {
    const fn from_option(value: Option<bool>) -> Self {
        match value {
            Some(true) => Self::Healthy,
            Some(false) => Self::Unhealthy,
            None => Self::Unknown,
        }
    }
}

/// Pure transition logger. Returns `None` when the state hasn't changed or when the change doesn't
/// deserve a log line (e.g. initial Unknown → Healthy at startup).
fn transition_log(prev: DbHealthState, next: DbHealthState, error: Option<&str>) -> Option<String> {
    if prev == next {
        return None;
    }
    match (prev, next) {
        (_, DbHealthState::Unhealthy) => {
            let err = error.unwrap_or("unknown error");
            Some(format!("[health] db degraded: {err}"))
        }
        (DbHealthState::Unhealthy, DbHealthState::Healthy) => {
            Some("[health] db recovered".to_owned())
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use super::{CompactFailure, DbHealthState, HealthResp, SyncState, transition_log};
    use axum::http::StatusCode;
    use axum::response::IntoResponse;

    #[test]
    fn same_state_is_silent() {
        assert!(transition_log(DbHealthState::Healthy, DbHealthState::Healthy, None).is_none());
        assert!(
            transition_log(
                DbHealthState::Unhealthy,
                DbHealthState::Unhealthy,
                Some("x")
            )
            .is_none()
        );
        assert!(transition_log(DbHealthState::Unknown, DbHealthState::Unknown, None).is_none());
    }

    #[test]
    fn failure_transition_includes_error_text() {
        let log = transition_log(
            DbHealthState::Healthy,
            DbHealthState::Unhealthy,
            Some("connection closed"),
        )
        .unwrap();
        assert!(log.starts_with("[health] db degraded:"));
        assert!(log.contains("connection closed"));
    }

    #[test]
    fn recovery_transition_is_one_line() {
        let log = transition_log(DbHealthState::Unhealthy, DbHealthState::Healthy, None).unwrap();
        assert_eq!(log, "[health] db recovered");
    }

    #[test]
    fn unknown_to_unhealthy_logs_failure() {
        let log = transition_log(
            DbHealthState::Unknown,
            DbHealthState::Unhealthy,
            Some("cannot connect"),
        )
        .unwrap();
        assert!(log.starts_with("[health] db degraded:"));
        assert!(log.contains("cannot connect"));
    }

    /// The number the docker log could never carry. A compact that fails once is a hiccup;
    /// the 64 MB case (#304) had been failing on every run for weeks, and nothing counted.
    #[test]
    fn consecutive_failures_accumulate_until_a_run_succeeds() {
        let first = CompactFailure::next(None, "2026-09-09T00:00:00Z".into(), "shm".into());
        assert_eq!(first.consecutive, 1);
        let second =
            CompactFailure::next(Some(&first), "2026-09-09T04:00:00Z".into(), "shm".into());
        assert_eq!(second.consecutive, 2, "a second failure is not a first one");
        assert_eq!(
            second.at, "2026-09-09T04:00:00Z",
            "the timestamp is of the latest attempt, not the first"
        );
        let capped = CompactFailure::next(
            Some(&CompactFailure {
                at: String::new(),
                consecutive: u32::MAX,
                error: String::new(),
            }),
            String::new(),
            String::new(),
        );
        assert_eq!(
            capped.consecutive,
            u32::MAX,
            "saturates rather than wrapping to 0"
        );
    }

    /// Health carries the failure only while there is one: the field's presence is the alarm,
    /// and `doctor` (a2c) tests presence rather than parsing a status word.
    #[test]
    fn health_omits_the_failure_once_a_compact_succeeds() {
        let mut resp = HealthResp {
            status: "ok",
            vector: true,
            sync: SyncState::Idle,
            corpus_count: None,
            db_healthy: None,
            build_sha: None,
            compact_failure: Some(CompactFailure::next(
                None,
                "2026-09-09T00:00:00Z".into(),
                "boom".into(),
            )),
        };
        let failing = serde_json::to_string(&resp).unwrap();
        assert!(failing.contains("compact_failure"), "was {failing}");
        resp.compact_failure = None;
        let healthy = serde_json::to_string(&resp).unwrap();
        assert!(
            !healthy.contains("compact_failure"),
            "a healthy engine's /health must read exactly as it did before: {healthy}"
        );
    }

    #[test]
    fn unknown_to_healthy_is_silent_at_startup() {
        assert!(transition_log(DbHealthState::Unknown, DbHealthState::Healthy, None).is_none());
    }

    #[test]
    fn search_req_defaults_to_todays_shape_and_clamps_related() {
        let req: super::SearchReq = serde_json::from_str(r#"{"query":"x"}"#).unwrap();
        assert_eq!(
            req.related, 0,
            "absent means today's behaviour, byte-for-byte"
        );
        assert_eq!(req.related_heads, 2);

        let req: super::SearchReq = serde_json::from_str(r#"{"query":"x","related":9}"#).unwrap();
        assert_eq!(req.related(), 3, "the walk per hit is capped");
        assert_eq!(
            req.related_heads(usize::MAX),
            2,
            "the default head count is within range, untouched"
        );
        let req: super::SearchReq =
            serde_json::from_str(r#"{"query":"x","related_heads":99}"#).unwrap();
        assert_eq!(req.related_heads(5), 5, "cannot exceed max_results");
    }

    #[test]
    fn search_hit_omits_related_key_until_one_is_attached() {
        let mut hit = super::SearchHit {
            id: "1".into(),
            origin: "wiki".into(),
            project: "p".into(),
            source_path: "a.md".into(),
            snippet: "s".into(),
            dist: None,
            dist_kind: None,
            related: vec![],
            superseded_by: vec![],
            used_count: 0,
            contested_count: 0,
        };
        let json = serde_json::to_string(&hit).unwrap();
        assert!(
            !json.contains("related"),
            "callers that did not ask see no new key: {json}"
        );
        hit.related.push(super::RelatedNote {
            source_path: "b.md".into(),
            snippet: "older".into(),
        });
        let json = serde_json::to_string(&hit).unwrap();
        assert!(json.contains("\"related\":"), "was {json}");
    }

    /// `superseded_by` follows the `related` contract: no key until a replacement is recorded,
    /// then the replacing doc's `source_path`.
    #[test]
    fn search_hit_omits_superseded_by_key_until_one_exists() {
        let mut hit = super::SearchHit {
            id: "1".into(),
            origin: "wiki".into(),
            project: "p".into(),
            source_path: "a.md".into(),
            snippet: "s".into(),
            dist: None,
            dist_kind: None,
            related: vec![],
            superseded_by: vec![],
            used_count: 0,
            contested_count: 0,
        };
        let json = serde_json::to_string(&hit).unwrap();
        assert!(
            !json.contains("superseded_by"),
            "callers see no new key until a replacement exists: {json}"
        );
        hit.superseded_by.push("/w/b.md".into());
        let json = serde_json::to_string(&hit).unwrap();
        assert!(
            json.contains("\"superseded_by\":[\"/w/b.md\"]"),
            "was {json}"
        );
    }

    /// Consumption counts are part of the accuracy contract with the recall hook: always present,
    /// integers, even when nothing has consumed the note yet.
    #[test]
    fn search_hit_always_carries_consumption_counts() {
        let hit = super::SearchHit {
            id: "1".into(),
            origin: "wiki".into(),
            project: "p".into(),
            source_path: "a.md".into(),
            snippet: "s".into(),
            dist: None,
            dist_kind: None,
            related: vec![],
            superseded_by: vec![],
            used_count: 3,
            contested_count: 1,
        };
        let json = serde_json::to_value(&hit).unwrap();
        assert_eq!(json["used_count"], serde_json::json!(3));
        assert_eq!(json["contested_count"], serde_json::json!(1));
        assert!(
            json["used_count"].is_i64() && json["contested_count"].is_i64(),
            "counts must serialize as integers: {json}"
        );

        let zero = super::SearchHit {
            used_count: 0,
            contested_count: 0,
            ..hit
        };
        let json = serde_json::to_string(&zero).unwrap();
        assert!(
            json.contains("\"used_count\":0") && json.contains("\"contested_count\":0"),
            "absence of consumption is 0, not a missing key: {json}"
        );
    }

    fn consumption_req(
        session_id: &str,
        observed_at: &str,
        used: Vec<String>,
        contested: Vec<String>,
    ) -> super::ConsumptionReq {
        super::ConsumptionReq {
            session_id: session_id.to_owned(),
            observed_at: observed_at.to_owned(),
            used,
            contested,
            supersedes: vec![],
        }
    }

    #[test]
    fn consumption_rejects_empty_session_id() {
        let req = consumption_req("   ", "2026-09-11T05:12:00+00:00", vec![], vec![]);
        let err = super::validate_consumption_req(&req).unwrap_err();
        assert_eq!(err.into_response().status(), StatusCode::BAD_REQUEST);
    }

    #[test]
    fn consumption_rejects_unparsable_observed_at() {
        for bad in ["yesterday", "2026-09-11", "2026-09-11 05:12:00"] {
            let req = consumption_req("s-1", bad, vec![], vec![]);
            let err = super::validate_consumption_req(&req).unwrap_err();
            assert_eq!(
                err.into_response().status(),
                StatusCode::BAD_REQUEST,
                "{bad:?} must be rejected"
            );
        }
        let ok = consumption_req("s-1", "2026-09-11T05:12:00+00:00", vec![], vec![]);
        assert!(super::validate_consumption_req(&ok).is_ok());
    }

    #[test]
    fn consumption_rejects_more_than_200_paths_per_list() {
        let paths = vec!["/vault/wiki/wiki-0001.md".to_owned(); super::CONSUMPTION_MAX_PATHS + 1];
        for (used, contested) in [(paths.clone(), vec![]), (vec![], paths.clone())] {
            let req = consumption_req("s-1", "2026-09-11T05:12:00+00:00", used, contested);
            let err = super::validate_consumption_req(&req).unwrap_err();
            assert_eq!(err.into_response().status(), StatusCode::BAD_REQUEST);
        }
        let at_cap = vec!["/vault/wiki/wiki-0001.md".to_owned(); super::CONSUMPTION_MAX_PATHS];
        let ok = consumption_req("s-1", "2026-09-11T05:12:00+00:00", at_cap, vec![]);
        assert!(super::validate_consumption_req(&ok).is_ok());
    }

    /// `supersedes` pairs share the same ceiling as the path lists — the scorer reports one
    /// session at a time.
    #[test]
    fn consumption_rejects_more_than_200_supersedes_pairs() {
        let pair = [
            "/vault/wiki/wiki-new.md".to_owned(),
            "/vault/wiki/wiki-old.md".to_owned(),
        ];
        let mut req = consumption_req("s-1", "2026-09-11T05:12:00+00:00", vec![], vec![]);
        req.supersedes = vec![pair.clone(); super::CONSUMPTION_MAX_PATHS + 1];
        let err = super::validate_consumption_req(&req).unwrap_err();
        assert_eq!(err.into_response().status(), StatusCode::BAD_REQUEST);

        req.supersedes = vec![pair; super::CONSUMPTION_MAX_PATHS];
        assert!(super::validate_consumption_req(&req).is_ok());
    }
}

/// Fire-and-forget query logging. Latency and result context are recorded for
/// memory-utility analytics; failures are logged to stderr and never fail the request.
#[allow(clippy::needless_borrow)] // tokio-postgres needs &&str to coerce to &dyn ToSql.
pub(crate) fn spawn_query_log(
    store: Option<Arc<Store>>,
    endpoint: impl Into<String>,
    query: String,
    hits: Vec<LoggedHit>,
    sources: Vec<String>,
    answer_snippet: String,
    elapsed: std::time::Duration,
) {
    let Some(store) = store else {
        return;
    };
    let endpoint = endpoint.into();
    tokio::spawn(async move {
        let latency_ms = i32::try_from(elapsed.as_millis()).ok();
        if let Err(e) = store
            .log_query(
                &endpoint,
                &query,
                &hits,
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

// ── error type (ROP: AppError → HTTP status) ────────────────────────────────

pub(crate) struct AppError {
    status: StatusCode,
    error: anyhow::Error,
}

impl AppError {
    pub(crate) fn bad_request(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            error: anyhow::anyhow!(message.into()),
        }
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        #[derive(Serialize)]
        struct ErrBody {
            error: String,
        }
        let body = ErrBody {
            error: format!("{:#}", self.error),
        };
        (self.status, Json(body)).into_response()
    }
}

impl<E: Into<anyhow::Error>> From<E> for AppError {
    fn from(e: E) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            error: e.into(),
        }
    }
}

// ── request/response types ─────────────────────────────────────────────────

#[derive(Deserialize)]
pub(crate) struct AskReq {
    pub(crate) question: String,
    #[serde(default)]
    pub(crate) project: Option<String>,
    #[serde(default)]
    pub(crate) since_hours: Option<i32>,
}

#[derive(Serialize)]
pub(crate) struct AskResp {
    pub(crate) answer: String,
    pub(crate) sources: Vec<String>,
    /// Claims placed in the prompt, `kind|subject|predicate|value`. Present so a consumer can
    /// ask "did the answer keep what it was given?" — the briefing dropped both injected
    /// `blocked` claims on 2026-08-14 and nothing downstream could tell. Empty on paths that
    /// inject none, so existing consumers are unaffected.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub(crate) injected_claims: Vec<String>,
}

#[derive(Deserialize)]
pub(crate) struct SearchReq {
    pub(crate) query: String,
    #[serde(default = "default_max_results")]
    pub(crate) max_results: usize,
    #[serde(default = "default_max_tokens")]
    pub(crate) max_tokens: usize,
    #[serde(default)]
    pub(crate) project: Option<String>,
    #[serde(default)]
    pub(crate) since_hours: Option<i32>,
    #[serde(default)]
    pub(crate) related: usize,
    #[serde(default = "default_related_heads")]
    pub(crate) related_heads: usize,
}

impl SearchReq {
    /// Related notes per hit, clamped: each one is a graph walk, and an unbounded count
    /// would let one search fan out into the whole neighbourhood.
    pub(crate) fn related(&self) -> usize {
        self.related.min(3)
    }

    /// How many of the top hits get related notes. Cannot exceed `max_results` — there is
    /// no hit past that to attach anything to.
    pub(crate) fn related_heads(&self, max_results: usize) -> usize {
        self.related_heads.min(max_results)
    }
}

fn default_related_heads() -> usize {
    2
}

fn default_max_results() -> usize {
    5
}

fn default_max_tokens() -> usize {
    2000
}

#[derive(Serialize)]
pub(crate) struct RelatedNote {
    pub(crate) source_path: String,
    /// Older note content, truncated to 1200 chars on a char boundary — the same cap
    /// `ask` uses for graph context.
    pub(crate) snippet: String,
}

#[derive(Serialize)]
pub(crate) struct SearchHit {
    pub(crate) id: String,
    pub(crate) origin: String,
    pub(crate) project: String,
    pub(crate) source_path: String,
    pub(crate) snippet: String,
    /// Relevance signal for this hit — see `dist_kind` for what it means. `None` when the serving
    /// path (wiki-recall fallback, `BORING_VECTOR=off`) has no comparable number to offer.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) dist: Option<f32>,
    /// What `dist` measures. Cosine distance and full-text rank are not the same scale — a
    /// consumer must branch on this before comparing `dist` against a threshold.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) dist_kind: Option<crate::store::DistKind>,
    /// Older notes sharing a concept with this hit, walked from the graph rather than
    /// retrieved by the query. Present only when the request asked for them — a caller
    /// that did not set `related` sees no new key.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub(crate) related: Vec<RelatedNote>,
    /// `source_path`s of the docs that declared themselves the replacement for this hit
    /// (`supersedes` edges with this doc as dst). Absent until one exists.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub(crate) superseded_by: Vec<String>,
    /// How many sessions recorded this note as used (`used` edges with this doc as dst).
    /// Always present; 0 on the wiki-recall fallback, where the graph cannot say.
    pub(crate) used_count: i64,
    /// Same for `contested` edges — the note was injected and the session pushed back.
    pub(crate) contested_count: i64,
}

#[derive(Serialize)]
pub(crate) struct SearchResp {
    pub(crate) hits: Vec<SearchHit>,
}

/// Hard ceiling on paths per list in one `/consumption` call — the scorer reports one session at a time.
pub(crate) const CONSUMPTION_MAX_PATHS: usize = 200;

#[derive(Deserialize)]
pub(crate) struct ConsumptionReq {
    pub(crate) session_id: String,
    /// RFC 3339 — becomes the `session:<id>` node's label.
    pub(crate) observed_at: String,
    #[serde(default)]
    pub(crate) used: Vec<String>,
    #[serde(default)]
    pub(crate) contested: Vec<String>,
    /// `[newer_path, older_path]` pairs — for each known pair, one
    /// `(doc:<newer>) -[supersedes]-> (doc:<older>)` edge.
    #[serde(default)]
    pub(crate) supersedes: Vec<[String; 2]>,
}

#[derive(Serialize)]
pub(crate) struct ConsumptionResp {
    pub(crate) session: String,
    pub(crate) used: usize,
    pub(crate) contested: usize,
    /// `supersedes` edges written from the request's pairs.
    pub(crate) supersedes: usize,
    /// Paths skipped because no `document` row exists for them (or a pair named one path twice).
    pub(crate) unknown: usize,
}

/// Parse-don't-validate at the HTTP boundary: an unparsable timestamp or an oversized list is
/// rejected here rather than stored and surfaced later as a wrong count.
pub(crate) fn validate_consumption_req(req: &ConsumptionReq) -> Result<(), AppError> {
    if req.session_id.trim().is_empty() {
        return Err(AppError::bad_request("session_id must not be empty"));
    }
    if chrono::DateTime::parse_from_rfc3339(&req.observed_at).is_err() {
        return Err(AppError::bad_request(format!(
            "observed_at must be RFC 3339, got {:?}",
            req.observed_at
        )));
    }
    for (name, len) in [
        ("used", req.used.len()),
        ("contested", req.contested.len()),
        ("supersedes", req.supersedes.len()),
    ] {
        if len > CONSUMPTION_MAX_PATHS {
            return Err(AppError::bad_request(format!(
                "{name}: at most {CONSUMPTION_MAX_PATHS} paths, got {len}"
            )));
        }
    }
    Ok(())
}

#[derive(Deserialize)]
pub(crate) struct GraphReq {
    pub(crate) query: String,
}

#[derive(Deserialize)]
pub(crate) struct WeeklyReq {}

#[derive(Deserialize)]
pub(crate) struct StatusReq {
    pub(crate) project: String,
}

#[derive(Deserialize)]
pub(crate) struct DecisionsReq {
    pub(crate) project: Option<String>,
}

#[derive(Deserialize)]
pub(crate) struct RisksReq {
    pub(crate) project: Option<String>,
}

#[derive(Deserialize)]
pub(crate) struct NextActionsReq {
    pub(crate) project: Option<String>,
}

#[derive(Deserialize)]
pub(crate) struct StalledReq {
    pub(crate) project: Option<String>,
    pub(crate) older_than_days: Option<u32>,
}

#[derive(Deserialize)]
pub(crate) struct ContextReq {
    pub(crate) project: Option<String>,
    #[serde(default)]
    pub(crate) exclude_origins: Vec<String>,
    #[serde(default = "default_context_max_items")]
    pub(crate) max_items: usize,
}

fn default_context_max_items() -> usize {
    5
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
    /// Notes the walk reached but did not ingest. Non-zero means the corpus is smaller than the
    /// vault and the difference is NOT visible anywhere else — `new/updated/deleted` all stay
    /// consistent while notes go missing. On 2026-08-14 three real notes vanished this way and
    /// the only reason it was caught was a manual file-vs-DB diff.
    pub(crate) ingest_skipped: usize,
    /// Notes that errored on parse/ingest. The sync deliberately does not abort (resilience),
    /// so this is the only signal that it happened.
    pub(crate) ingest_failed: usize,
    /// Notes whose frontmatter was rewritten on disk before re-ingesting. A silent mutation of
    /// the user's files, so it is reported even when it succeeded.
    pub(crate) ingest_repaired: usize,
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
    // Distances were persisted by #209 but never surfaced here, so a caller could see WHICH notes
    // were injected and not how far they were — the labeller needs both to sample the band where a
    // relevance decision would actually bite. Absent stays `null`, never 0.
    pub(crate) hit_dists: Vec<Option<f32>>,
    pub(crate) hit_dist_kinds: Vec<Option<String>>,
    pub(crate) sources: Vec<String>,
    pub(crate) answer_snippet: String,
    pub(crate) latency_ms: Option<i32>,
}

#[derive(Deserialize)]
pub(crate) struct RecallLabelReq {
    pub(crate) query_log_id: i32,
    pub(crate) hit_index: i32,
    pub(crate) judge: String,
    pub(crate) verdict: String,
    #[serde(default)]
    pub(crate) model: String,
    #[serde(default)]
    pub(crate) note: String,
}

#[derive(Deserialize)]
pub(crate) struct RecallLabelsReq {
    #[serde(default = "default_query_log_limit")]
    pub(crate) limit: i64,
}

#[derive(Serialize)]
pub(crate) struct RecallLabelsResp {
    pub(crate) entries: Vec<RecallLabelEntry>,
}

#[derive(Serialize)]
pub(crate) struct RecallLabelEntry {
    pub(crate) query_log_id: i32,
    pub(crate) hit_index: i32,
    pub(crate) judge: String,
    pub(crate) verdict: String,
    pub(crate) model: String,
    pub(crate) note: String,
}

#[derive(Serialize)]
pub(crate) struct ProjectsResp {
    pub(crate) projects: Vec<String>,
}

#[derive(Serialize)]
pub(crate) struct RecallLabelStatsResp {
    pub(crate) judges: Vec<RecallLabelJudgeStats>,
    /// Hits where both judges gave a real verdict, and how many of those matched. Reported even
    /// when tiny, because a precision number from an unaudited LLM judge is not evidence.
    pub(crate) agreed: i64,
    pub(crate) compared: i64,
}

#[derive(Serialize)]
pub(crate) struct RecallLabelJudgeStats {
    pub(crate) judge: String,
    pub(crate) relevant: i64,
    pub(crate) irrelevant: i64,
    pub(crate) unsure: i64,
}

#[derive(Deserialize)]
pub(crate) struct EventLogReq {
    #[serde(default = "default_event_log_limit")]
    pub(crate) limit: i64,
    #[serde(default)]
    pub(crate) component: Option<String>,
    #[serde(default, rename = "event")]
    pub(crate) event_name: Option<String>,
    #[serde(default)]
    pub(crate) status: Option<String>,
    #[serde(default)]
    pub(crate) run_id: Option<String>,
    #[serde(default)]
    pub(crate) workflow: Option<String>,
    #[serde(default)]
    pub(crate) since_hours: Option<i32>,
}

fn default_event_log_limit() -> i64 {
    50
}

#[derive(Serialize)]
pub(crate) struct EventLogResp {
    pub(crate) entries: Vec<EventLogEntry>,
    /// The limit actually used, after clamping to `EVENT_LOG_MAX_LIMIT`. A caller asking for 5000
    /// got 1000 and no word about it, so the pre-registered verdict was being computed on 54% of
    /// its own window with nothing anywhere saying so (2026-09-10). Truncation and completeness
    /// look identical from the client's side; only the server knows which one it sent.
    pub(crate) limit_applied: i64,
    /// `true` when the page came back full at that limit, so there may be more behind it. Says
    /// "there may be more", never "there are more" -- a page that happens to end exactly on the
    /// boundary is complete, and claiming otherwise would be the same guess in the other
    /// direction.
    pub(crate) maybe_truncated: bool,
}

#[derive(Serialize)]
pub(crate) struct EventLogEntry {
    pub(crate) id: i64,
    pub(crate) observed_at: String,
    pub(crate) time_unix_nano: Option<i64>,
    pub(crate) severity_text: String,
    pub(crate) severity_number: i32,
    pub(crate) service_name: String,
    pub(crate) component: String,
    #[serde(rename = "event")]
    pub(crate) event_name: String,
    pub(crate) status: String,
    pub(crate) trace_id: Option<String>,
    pub(crate) span_id: Option<String>,
    pub(crate) run_id: Option<String>,
    pub(crate) session_id: Option<String>,
    pub(crate) workflow: Option<String>,
    pub(crate) workflow_node: Option<String>,
    pub(crate) workflow_outcome: Option<String>,
    pub(crate) body: Value,
    pub(crate) attributes: Value,
    pub(crate) resource: Value,
    pub(crate) otel: Value,
}

#[derive(Serialize)]
pub(crate) struct EventIngestResp {
    pub(crate) accepted: usize,
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
    /// `true` when all configured DB clients answer `SELECT 1`, `false` when any fail,
    /// omitted when no DB client is configured (vector off + code_index off) to avoid false alarms.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) db_healthy: Option<bool>,
    /// The commit this binary was built from, stamped by the image build (`BORING_BUILD_SHA`).
    /// `null` when the builder did not pass one — merging is not deploying, and an absent sha
    /// says "unknown" rather than asserting a wrong one. Compare against `git rev-parse HEAD`
    /// to see whether what is running is what was merged; `scripts/doctor.sh` does exactly that.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) build_sha: Option<String>,
    /// Present only while the last compact attempt failed, so a healthy engine's /health is
    /// unchanged and the field's mere presence is the alarm. `scripts/doctor.sh` (d7) reads it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) compact_failure: Option<CompactFailure>,
}

impl CompactFailure {
    /// The next failure record, given whatever the last one was. Pulled out of the two call
    /// sites (the scheduler and hand-run `/compact`) so the count is defined in one place and
    /// can be tested without a database: the number that matters is not "it failed" but "it has
    /// been failing since July", and that is the one a run-by-run `eprintln!` can never carry.
    pub(crate) fn next(previous: Option<&Self>, at: String, error: String) -> Self {
        Self {
            at,
            consecutive: previous.map_or(1, |f| f.consecutive.saturating_add(1)),
            error,
        }
    }
}

/// A compact run that did not finish, kept until one does.
#[derive(Debug, Clone, serde::Serialize)]
pub(crate) struct CompactFailure {
    /// RFC3339, so the reader can tell "failing since breakfast" from "failing since July".
    pub(crate) at: String,
    /// Consecutive failures. One is a hiccup; the 64 MB case would have been counting for weeks.
    pub(crate) consecutive: u32,
    pub(crate) error: String,
}

/// Reads the build stamp. Empty or unset both mean "not stamped" — an empty string would
/// otherwise serialize as a sha of `""` and compare unequal to everything, which reads as
/// drift when the truth is that nobody recorded it.
pub(crate) fn build_sha() -> Option<String> {
    std::env::var("BORING_BUILD_SHA")
        .ok()
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty())
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
    anyhow::anyhow!(
        "BORING_VECTOR=off — this feature requires the vector backend (pgvector). Set BORING_VECTOR=on and start Postgres."
    )
    .into()
}

/// The same rejection mapped into the MCP `(code, message)` tuple — for vector-only tools
/// (neighbors/claims/corpus_status). SSOT with `vector_disabled`; never `unwrap` the store (ROP).
pub(crate) fn vec_off_rpc() -> (i32, String) {
    (-32603, format!("{:#}", vector_disabled().error))
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
    let compact_failure = Arc::new(Mutex::new(None));
    let pii = vault_dir
        .as_ref()
        .map(|vd| crate::pii::PiiScanner::load_from_vault(vd))
        .transpose()?
        .flatten();
    let code_index = if cfg
        .code_index
        .sources
        .iter()
        .any(config::CodeIndexSource::enabled)
    {
        let dsn = config::pg_dsn();
        Some(Arc::new(CodeIndexStore::connect(&dsn)?))
    } else {
        None
    };
    let state = AppState {
        store: store.map(Arc::new),
        code_index,
        llm: Arc::new(llm),
        vault_dir: Arc::new(vault_dir),
        pii: Arc::new(pii),
        cfg: Arc::new(cfg),
        cfg_path: Arc::new(cfg_path),
        sync_lock: Arc::new(Mutex::new(())),
        last_compact: Arc::clone(&last_compact),
        compact_failure: Arc::clone(&compact_failure),
        wiki_index: Arc::new(std::sync::Mutex::new(wiki_recall::WikiIndex::default())),
        db_healthy_last: Arc::new(AtomicU8::new(DbHealthState::Unknown.to_u8())),
    };

    scheduler::spawn_scheduler(
        state.store.clone(),
        Arc::clone(&state.llm),
        Arc::clone(&state.vault_dir),
        Arc::clone(&state.cfg),
        Arc::clone(&state.sync_lock),
        Arc::clone(&last_compact),
        Arc::clone(&compact_failure),
    );
    // cfg_path is only used by the HTTP/MCP handlers; the scheduler does not need it.

    let router = axum::Router::new()
        .route("/health", get(http::health))
        .route("/ask", post(http::handle_ask))
        .route("/brief", post(http::handle_brief))
        .route("/weekly", post(http::handle_weekly))
        .route("/status", post(http::handle_project_status))
        .route("/decisions", post(http::handle_decisions))
        .route("/risks", post(http::handle_risks))
        .route("/next_actions", post(http::handle_next_actions))
        .route("/stalled", post(http::handle_stalled))
        .route("/context", post(http::handle_context))
        .route("/search", post(http::handle_search))
        .route("/consumption", post(http::handle_consumption))
        .route("/graph", post(http::handle_graph))
        .route("/audit", get(http::handle_audit))
        .route("/query-log", get(http::handle_query_log))
        .route(
            "/recall-labels",
            get(http::handle_recall_labels).post(http::handle_recall_label_record),
        )
        .route("/projects", get(http::handle_projects))
        .route("/recall-label-stats", get(http::handle_recall_label_stats))
        .route(
            "/events",
            get(http::handle_events).post(http::handle_event_ingest),
        )
        .route(
            "/otel-events",
            get(http::handle_events).post(http::handle_event_ingest),
        )
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
