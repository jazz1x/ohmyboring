//! HTTP handlers for the ohmyboring axum API.
//!
//! Cross-reference: design decision D3 (write door gated / read door open).
use std::time::Instant;

use axum::Json;
use axum::extract::{Query, State};

use crate::ask;
use crate::audit;
use crate::graph;
use crate::retrieve;
use crate::serve::{
    AppError, AppState, AskReq, AskResp, CompactResp, GraphReq, GraphResp, HealthResp,
    MCP_MAX_RESULTS, MCP_MAX_TOKENS, QueryLogEntry, QueryLogReq, QueryLogResp, SearchHit,
    SearchResp, SyncResp, SyncState, count_wiki_notes, spawn_query_log, vector_disabled,
};

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
    // wiki-first: try direct markdown search before vector retrieval.
    let wiki_hits = s.wiki_recall(&req.query, max_results, project, since_hours)?;
    let mapped: Vec<SearchHit> = if !wiki_hits.is_empty() {
        wiki_hits
            .into_iter()
            .map(|h| SearchHit {
                id: h.id,
                origin: String::new(),
                project: String::new(),
                source_path: h.source_path,
                snippet: h.snippet,
            })
            .collect()
    } else if let Some(store) = s.store.as_ref() {
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
        Vec::new()
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
