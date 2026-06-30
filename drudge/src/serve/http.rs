//! HTTP handlers for the ohmyboring axum API.
//!
//! Cross-reference: design decision D3 (write door gated / read door open).
use std::time::Instant;
use std::time::SystemTime;

use axum::Json;
use axum::extract::{Query, State};
use serde_json::{Value, json};

use crate::ask;
use crate::audit;
use crate::graph;
use crate::retrieve;
use crate::serve::{
    AppError, AppState, AskReq, AskResp, CompactResp, EventIngestResp, EventLogEntry, EventLogReq,
    EventLogResp, GraphReq, GraphResp, HealthResp, MCP_MAX_RESULTS, MCP_MAX_TOKENS, QueryLogEntry,
    QueryLogReq, QueryLogResp, SearchHit, SearchResp, StalledReq, SyncResp, SyncState,
    count_wiki_notes, spawn_query_log, vector_disabled,
};
use crate::store::EventLogFilter;

const EVENT_LOG_MAX_LIMIT: i64 = 1000;
const EVENT_INGEST_MAX_BATCH: usize = 100;

pub(crate) async fn health(State(state): State<AppState>) -> Json<HealthResp> {
    // Non-blocking: try_lock reveals whether a sync is mid-flight without ever waiting on it. The
    // momentary guard is dropped at the end of the expression, so this never blocks a real sync.
    let sync = if state.sync_lock.try_lock().is_ok() {
        SyncState::Idle
    } else {
        SyncState::Running
    };
    Json(HealthResp {
        status: "ok",
        vector: state.store.is_some(),
        sync,
        corpus_count: state.wiki_dir().as_deref().and_then(count_wiki_notes),
    })
}

pub(crate) async fn handle_ask(
    State(s): State<AppState>,
    Json(req): Json<AskReq>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    // vector on → synthesize from vector+graph retrieval. off → synthesize from direct vault/wiki reads.
    let project = req.project.as_deref();
    let since_hours = req.since_hours;
    let out = if let Some(store) = s.store.as_ref() {
        ask::answer(store, &s.llm, &req.question, &[], project, since_hours).await?
    } else {
        ask::answer_wiki(
            &s.llm,
            s.wiki_dir().as_deref(),
            &req.question,
            project,
            since_hours,
        )
        .await?
    };
    spawn_query_log(
        s.store.clone(),
        "ask",
        req.question,
        out.sources.clone(),
        out.sources.clone(),
        out.answer.chars().take(280).collect(),
        started.elapsed(),
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Recency-first briefing — no question (recency retrieval). Called by the cron morning briefing.
/// Recency (updated_at) ordering depends on pgvector → rejected if `BORING_VECTOR=off`.
pub(crate) async fn handle_brief(State(s): State<AppState>) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let out = ask::brief(store, &s.llm, &[], s.cfg.note_lang.as_str()).await?;
    spawn_query_log(
        s.store.clone(),
        "brief",
        String::new(),
        out.sources.clone(),
        out.sources.clone(),
        out.answer.chars().take(280).collect(),
        started.elapsed(),
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Weekly briefing — last 7 days, grouped by project.
pub(crate) async fn handle_weekly(
    State(s): State<AppState>,
    Json(_req): Json<crate::serve::WeeklyReq>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let out = ask::weekly_brief(store, &s.llm, &[], s.cfg.note_lang.as_str()).await?;
    spawn_query_log(
        s.store.clone(),
        "weekly",
        String::new(),
        out.sources.clone(),
        out.sources.clone(),
        out.answer.chars().take(280).collect(),
        started.elapsed(),
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Project status — last 30 days for a single project.
pub(crate) async fn handle_project_status(
    State(s): State<AppState>,
    Json(req): Json<crate::serve::StatusReq>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let out =
        ask::project_status(store, &s.llm, &req.project, &[], s.cfg.note_lang.as_str()).await?;
    spawn_query_log(
        s.store.clone(),
        "status",
        req.project.clone(),
        out.sources.clone(),
        out.sources.clone(),
        out.answer.chars().take(280).collect(),
        started.elapsed(),
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Decision register — recent decision claims.
pub(crate) async fn handle_decisions(
    State(s): State<AppState>,
    Json(req): Json<crate::serve::DecisionsReq>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let out = ask::decision_register(
        store,
        &s.llm,
        req.project.as_deref(),
        &[],
        s.cfg.note_lang.as_str(),
    )
    .await?;
    spawn_query_log(
        s.store.clone(),
        "decisions",
        req.project.clone().unwrap_or_default(),
        out.sources.clone(),
        out.sources.clone(),
        out.answer.chars().take(280).collect(),
        started.elapsed(),
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Risk register — recent risk/assumption/blocked claims.
pub(crate) async fn handle_risks(
    State(s): State<AppState>,
    Json(req): Json<crate::serve::RisksReq>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let out = ask::risk_register(
        store,
        &s.llm,
        req.project.as_deref(),
        &[],
        s.cfg.note_lang.as_str(),
    )
    .await?;
    spawn_query_log(
        s.store.clone(),
        "risks",
        req.project.clone().unwrap_or_default(),
        out.sources.clone(),
        out.sources.clone(),
        out.answer.chars().take(280).collect(),
        started.elapsed(),
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Next-action register — recent `next` claims plus active `blocked` claims.
pub(crate) async fn handle_next_actions(
    State(s): State<AppState>,
    Json(req): Json<crate::serve::NextActionsReq>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let out = ask::next_action_register(
        store,
        &s.llm,
        req.project.as_deref(),
        &[],
        s.cfg.note_lang.as_str(),
    )
    .await?;
    spawn_query_log(
        s.store.clone(),
        "next_actions",
        req.project.clone().unwrap_or_default(),
        out.sources.clone(),
        out.sources.clone(),
        out.answer.chars().take(280).collect(),
        started.elapsed(),
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Stalled register — `next`/`blocked` claims older than N days (default 7).
pub(crate) async fn handle_stalled(
    State(s): State<AppState>,
    Json(req): Json<StalledReq>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let out = ask::stalled_register(
        store,
        &s.llm,
        req.project.as_deref(),
        &[],
        s.cfg.note_lang.as_str(),
        req.older_than_days.unwrap_or(7),
    )
    .await?;
    spawn_query_log(
        s.store.clone(),
        "stalled",
        req.project.clone().unwrap_or_default(),
        out.sources.clone(),
        out.sources.clone(),
        out.answer.chars().take(280).collect(),
        started.elapsed(),
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Structured context card for agent session start — decisions/risks/facts/glossary/next_actions as claim lists.
/// Uses recency ordering (no vector search), so it works when BORING_VECTOR=off.
pub(crate) async fn handle_context(
    State(s): State<AppState>,
    Json(req): Json<crate::serve::ContextReq>,
) -> Result<Json<ask::ContextCard>, AppError> {
    // Context can be served from the vault even when the vector backend is off, because it only
    // needs current claims by recency. Fall back to an empty card if no store is available.
    let card = if let Some(store) = s.store.as_ref() {
        ask::context_card(
            store,
            req.project.as_deref(),
            &req.exclude_origins,
            req.max_items.clamp(1, 20),
            s.cfg.note_lang.as_str(),
        )
        .await?
    } else {
        ask::ContextCard {
            decisions: vec![],
            risks: vec![],
            facts: vec![],
            glossary: vec![],
            next_actions: vec![],
            language: s.cfg.note_lang.as_str().to_owned(),
        }
    };
    Ok(Json(card))
}

pub(crate) async fn handle_search(
    State(s): State<AppState>,
    Json(req): Json<crate::serve::SearchReq>,
) -> Result<Json<SearchResp>, AppError> {
    let started = Instant::now();
    let max_results = req.max_results.clamp(1, MCP_MAX_RESULTS);
    let max_tokens = req.max_tokens.clamp(1, MCP_MAX_TOKENS);
    let max_chars = max_tokens.saturating_mul(4);
    let project = req.project.as_deref();
    let since_hours = req.since_hours;
    // vector-first: /search is the external accuracy contract (eval gate). Use the strongest
    // retriever when available; fall back to direct wiki reads only when vector is off.
    let mapped: Vec<SearchHit> = if let Some(store) = s.store.as_ref() {
        retrieve::retrieve_budget(
            store,
            &s.llm,
            &req.query,
            max_results,
            max_chars,
            &[],
            project,
            since_hours,
        )
        .await?
        .into_iter()
        .map(|h| SearchHit {
            id: h.id,
            origin: h.origin,
            project: h.project,
            source_path: h.source_path,
            snippet: h.content,
        })
        .collect()
    } else {
        s.wiki_recall(&req.query, max_results, project, since_hours)?
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
    let hit_paths: Vec<String> = mapped.iter().map(|h| h.source_path.clone()).collect();
    spawn_query_log(
        s.store.clone(),
        "search",
        req.query.clone(),
        hit_paths,
        vec![],
        mapped
            .first()
            .map(|h| h.snippet.chars().take(200).collect())
            .unwrap_or_default(),
        started.elapsed(),
    );
    Ok(Json(SearchResp { hits: mapped }))
}

pub(crate) async fn handle_graph(
    State(s): State<AppState>,
    Json(req): Json<GraphReq>,
) -> Result<Json<GraphResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?; // graph is pgvector-only
    let out = graph::query(store, &s.llm, &req.query).await?;
    let hit = if out.hit.is_empty() {
        vec![]
    } else {
        vec![out.hit.clone()]
    };
    spawn_query_log(
        s.store.clone(),
        "graph",
        req.query.clone(),
        hit,
        vec![],
        out.hit.chars().take(200).collect(),
        started.elapsed(),
    );
    Ok(Json(GraphResp {
        hit: out.hit,
        graph_neighbors: out.graph_neighbors,
        semantic_neighbors: out.semantic_neighbors,
    }))
}

pub(crate) async fn handle_audit(
    State(s): State<AppState>,
) -> Result<Json<audit::AuditStats>, AppError> {
    let store = s.store.as_ref().ok_or_else(vector_disabled)?; // ingest stats are pgvector-only
    let stats = audit::stats(store, s.cfg.allow_company_origin).await?;
    Ok(Json(stats))
}

/// Recent query/retrieval log — for memory-utility analytics.
pub(crate) async fn handle_query_log(
    State(s): State<AppState>,
    Query(params): Query<QueryLogReq>,
) -> Result<Json<QueryLogResp>, AppError> {
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let limit = params.limit.clamp(1, 1000);
    let rows = store.recent_queries(limit).await?;
    let entries = rows
        .into_iter()
        .map(|r| QueryLogEntry {
            id: r.id,
            created_at: format!("{:?}", r.created_at),
            endpoint: r.endpoint,
            query: r.query,
            hit_paths: r.hit_paths,
            sources: r.sources,
            answer_snippet: r.answer_snippet,
            latency_ms: r.latency_ms,
        })
        .collect();
    Ok(Json(QueryLogResp { entries }))
}

/// Recent adapter/workflow events mirrored into Postgres. The payload is OpenTelemetry-shaped
/// (`otel`) while keeping legacy top-level keys for filtering and readability.
pub(crate) async fn handle_events(
    State(s): State<AppState>,
    Query(params): Query<EventLogReq>,
) -> Result<Json<EventLogResp>, AppError> {
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let limit = params.limit.clamp(1, EVENT_LOG_MAX_LIMIT);
    let rows = store
        .recent_events(EventLogFilter {
            limit,
            component: params.component.as_deref(),
            event_name: params.event_name.as_deref(),
            status: params.status.as_deref(),
            run_id: params.run_id.as_deref(),
            workflow: params.workflow.as_deref(),
            since_hours: params.since_hours,
        })
        .await?;
    let entries = rows
        .into_iter()
        .map(|r| {
            let observed_at = system_time_rfc3339(r.observed_at);
            let severity_text = r.severity_text;
            let event_name = r.event_name;
            let trace_id = r.trace_id;
            let span_id = r.span_id;
            let body = r.body;
            let attributes = r.attributes;
            let resource = r.resource;
            let otel = json!({
                "observed_timestamp": observed_at.clone(),
                "time_unix_nano": r.time_unix_nano,
                "severity_text": severity_text.clone(),
                "severity_number": r.severity_number,
                "body": body.clone(),
                "attributes": attributes.clone(),
                "resource": resource.clone(),
                "trace_id": trace_id.clone(),
                "span_id": span_id.clone(),
                "event_name": event_name.clone(),
            });
            EventLogEntry {
                id: r.id,
                observed_at,
                time_unix_nano: r.time_unix_nano,
                severity_text,
                severity_number: r.severity_number,
                service_name: r.service_name,
                component: r.component,
                event_name,
                status: r.status,
                trace_id,
                span_id,
                run_id: r.run_id,
                session_id: r.session_id,
                workflow: r.workflow,
                workflow_node: r.workflow_node,
                workflow_outcome: r.workflow_outcome,
                body,
                attributes,
                resource,
                otel,
            }
        })
        .collect();
    Ok(Json(EventLogResp { entries }))
}

/// Store one event or an `{events: [...]}` batch. The original file journal remains owned by
/// `agents/shared/event_log.py`; this endpoint is the queryable DB projection.
pub(crate) async fn handle_event_ingest(
    State(s): State<AppState>,
    Json(req): Json<Value>,
) -> Result<Json<EventIngestResp>, AppError> {
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let events = if let Some(items) = req.get("events").and_then(Value::as_array) {
        items
            .iter()
            .take(EVENT_INGEST_MAX_BATCH)
            .cloned()
            .collect::<Vec<_>>()
    } else {
        vec![req]
    };
    let accepted = events.len();
    for event in events {
        store.log_event(&event).await?;
    }
    Ok(Json(EventIngestResp { accepted }))
}

pub(crate) async fn handle_sync(State(s): State<AppState>) -> Result<Json<SyncResp>, AppError> {
    let _guard = s.sync_lock.lock().await;
    let o = super::scheduler::do_sync(s.store.as_deref(), &s.llm, (*s.vault_dir).as_ref(), &s.cfg)
        .await?;
    Ok(Json(SyncResp {
        ingest_new: o.ingest.new,
        ingest_updated: o.ingest.updated,
        ingest_deleted: o.ingest.deleted,
        ingest_chunks: o.ingest.chunks,
        graph_tools: o.ingest.tools,
        graph_concepts: o.ingest.concepts,
        graph_claims: o.ingest.claims,
        graph_edges: o.ingest.edges,
        total_chunks: o.total_chunks,
        total_edges: o.total_edges,
    }))
}

fn system_time_rfc3339(value: SystemTime) -> String {
    let datetime: chrono::DateTime<chrono::Utc> = value.into();
    datetime.to_rfc3339()
}

/// Maintenance compact: VACUUM/ANALYZE + REINDEX + query_log pruning + orphan GC.
pub(crate) async fn handle_compact(
    State(s): State<AppState>,
) -> Result<Json<CompactResp>, AppError> {
    let _guard = s.sync_lock.lock().await;
    let summary = super::scheduler::do_compact(s.store.as_deref()).await?;
    *s.last_compact.lock().await = Some(Instant::now());
    Ok(Json(CompactResp {
        vacuum_ms: summary.report.vacuum_ms,
        reindex_ms: summary.report.reindex_ms,
        prune_query_log: summary.report.prune_query_log,
        gc_tool: summary.report.gc_tool,
        gc_concept: summary.report.gc_concept,
        total_ms: summary.total_ms,
    }))
}
