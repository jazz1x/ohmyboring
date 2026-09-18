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
    pub(crate) store: Option<Arc<Store>>,
    pub(crate) code_index: Option<Arc<CodeIndexStore>>,
    pub(crate) llm: Arc<Llm>,
    pub(crate) vault_dir: Arc<Option<PathBuf>>,
    pub(crate) pii: Arc<Option<pii::PiiScanner>>,
    pub(crate) cfg: Arc<config::BoringConfig>,
    pub(crate) cfg_path: Arc<Option<PathBuf>>,
    pub(crate) sync_lock: Arc<Mutex<()>>,
    pub(crate) wiki_index: Arc<std::sync::Mutex<wiki_recall::WikiIndex>>,
    pub(crate) last_compact: Arc<Mutex<Option<std::time::Instant>>>,
    pub(crate) compact_failure: Arc<Mutex<Option<CompactFailure>>>,
    pub(crate) db_healthy_last: Arc<AtomicU8>,
}

impl AppState {
    pub(crate) fn wiki_dir(&self) -> Option<PathBuf> {
        (*self.vault_dir).as_ref().map(|v| v.join("wiki"))
    }

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
        let mut idx = self
            .wiki_index
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        idx.refresh(&dir)?;
        Ok(idx.search(query, k, project, since_hours))
    }

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
    fn search_req_claims_defaults_to_zero_and_clamps() {
        let absent: super::SearchReq = serde_json::from_str(r#"{"query":"x"}"#).unwrap();
        let zero: super::SearchReq = serde_json::from_str(r#"{"query":"x","claims":0}"#).unwrap();
        assert_eq!(
            absent.claims(),
            0,
            "absent means no claims, like explicit 0"
        );
        assert_eq!(zero.claims(), 0);
        let req: super::SearchReq = serde_json::from_str(r#"{"query":"x","claims":99}"#).unwrap();
        assert_eq!(
            req.claims(),
            super::SEARCH_MAX_CLAIMS,
            "the per-hit handover is capped"
        );
    }

    #[test]
    fn search_hit_omits_claims_keys_until_claims_attached() {
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
            claims: vec![],
            claims_total: None,
        };
        let json = serde_json::to_string(&hit).unwrap();
        assert!(
            !json.contains("claims"),
            "callers that did not ask see no claims keys: {json}"
        );
        hit.claims_total = Some(1);
        hit.claims.push(crate::store::RegisterRow::new(
            "subject".into(),
            "decision".into(),
            "value".into(),
            "decision".into(),
            "high".into(),
            std::time::SystemTime::UNIX_EPOCH,
            "p".into(),
        ));
        let json = serde_json::to_string(&hit).unwrap();
        assert!(
            json.contains("\"claims_total\":1") && json.contains("\"claims\":"),
            "was {json}"
        );
        assert!(
            json.contains("\"node_id\":\"claim:subject:decision\""),
            "the canonical node id rides along: {json}"
        );
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
            claims: vec![],
            claims_total: None,
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
            claims: vec![],
            claims_total: None,
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
            claims: vec![],
            claims_total: None,
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

#[allow(clippy::needless_borrow)]
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

#[derive(Debug)]
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
    #[serde(default)]
    pub(crate) claims: Option<u32>,
}

impl SearchReq {
    pub(crate) fn related(&self) -> usize {
        self.related.min(3)
    }

    pub(crate) fn related_heads(&self, max_results: usize) -> usize {
        self.related_heads.min(max_results)
    }

    /// Opt-in claims handover per hit. Absent and `0` are the same request — the response a
    /// caller gets today, byte for byte; the hook relies on that until the freeze lifts.
    pub(crate) fn claims(&self) -> u32 {
        self.claims.unwrap_or(0).min(SEARCH_MAX_CLAIMS)
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
    pub(crate) snippet: String,
}

#[derive(Serialize)]
pub(crate) struct SearchHit {
    pub(crate) id: String,
    pub(crate) origin: String,
    pub(crate) project: String,
    pub(crate) source_path: String,
    pub(crate) snippet: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) dist: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) dist_kind: Option<crate::store::DistKind>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub(crate) related: Vec<RelatedNote>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub(crate) superseded_by: Vec<String>,
    pub(crate) used_count: i64,
    pub(crate) contested_count: i64,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub(crate) claims: Vec<crate::store::RegisterRow>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) claims_total: Option<i64>,
}

#[derive(Serialize)]
pub(crate) struct SearchResp {
    pub(crate) hits: Vec<SearchHit>,
}

pub(crate) const CONSUMPTION_MAX_PATHS: usize = 200;

#[derive(Deserialize)]
pub(crate) struct ConsumptionReq {
    pub(crate) session_id: String,
    pub(crate) observed_at: String,
    #[serde(default)]
    pub(crate) used: Vec<String>,
    #[serde(default)]
    pub(crate) contested: Vec<String>,
    #[serde(default)]
    pub(crate) supersedes: Vec<[String; 2]>,
}

#[derive(Serialize)]
pub(crate) struct ConsumptionResp {
    pub(crate) session: String,
    pub(crate) used: usize,
    pub(crate) contested: usize,
    pub(crate) supersedes: usize,
    pub(crate) unknown: usize,
}

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
    pub(crate) ingest_skipped: usize,
    pub(crate) ingest_failed: usize,
    pub(crate) ingest_repaired: usize,
    pub(crate) ingest_chunks: usize,
    pub(crate) graph_tools: usize,
    pub(crate) graph_concepts: usize,
    pub(crate) graph_claims: usize,
    pub(crate) graph_edges: usize,
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
    pub(crate) gc_claim_nodes: usize,
    pub(crate) gc_claim_edges: usize,
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
    pub(crate) limit_applied: i64,
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
    pub(crate) sync: SyncState,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) corpus_count: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) db_healthy: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) build_sha: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) compact_failure: Option<CompactFailure>,
}

impl CompactFailure {
    pub(crate) fn next(previous: Option<&Self>, at: String, error: String) -> Self {
        Self {
            at,
            consecutive: previous.map_or(1, |f| f.consecutive.saturating_add(1)),
            error,
        }
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub(crate) struct CompactFailure {
    pub(crate) at: String,
    pub(crate) consecutive: u32,
    pub(crate) error: String,
}

pub(crate) fn build_sha() -> Option<String> {
    std::env::var("BORING_BUILD_SHA")
        .ok()
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty())
}

pub(crate) fn count_wiki_notes(wiki_dir: &Path) -> Option<usize> {
    let entries = std::fs::read_dir(wiki_dir).ok()?;
    Some(
        entries
            .filter_map(Result::ok)
            .filter(|e| e.path().extension().is_some_and(|x| x == "md"))
            .count(),
    )
}

pub(crate) fn vector_disabled() -> AppError {
    anyhow::anyhow!(
        "BORING_VECTOR=off — this feature requires the vector backend (pgvector). Set BORING_VECTOR=on and start Postgres."
    )
    .into()
}

pub(crate) fn vec_off_rpc() -> (i32, String) {
    (-32603, format!("{:#}", vector_disabled().error))
}

pub(crate) const MCP_MAX_RESULTS: usize = 50;
pub(crate) const MCP_MAX_TOKENS: usize = 16_384;
/// Per-hit cap for the opt-in claims handover — a note declares a handful of solving claims,
/// not dozens, so a runaway `claims=N` cannot turn a search hit into a register dump.
pub(crate) const SEARCH_MAX_CLAIMS: u32 = 10;

pub async fn run(store: Option<Store>, llm: Llm, cfg: config::BoringConfig) -> Result<()> {
    let vault_dir: Option<PathBuf> = config::env_set("BORING_VAULT_DIR").map(PathBuf::from);

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
        .route("/mcp", get(mcp::handle_mcp_get).post(mcp::handle_mcp))
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .map_err(|e| anyhow::anyhow!("bind {addr}: {e}"))?;
    eprintln!("[serve] listening on {addr}");

    axum::serve(listener, router)
        .await
        .map_err(|e| anyhow::anyhow!("axum serve: {e}"))
}
