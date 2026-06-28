//! Retrieval pipeline — vector + BM25 full-text → RRF merge → top-k / budget-aware. origin filter.
//!
//! Cross-reference: design decision D3 (read door open) · ENFORCEMENT.md §B (one-way flow).
//!   - No rewriting/reranker (personal scale = simplest thing that works).
use std::collections::HashMap;

use anyhow::{Context, Result};

use crate::llm::Llm;
use crate::store::{Hit, Store};

const RRF_K: f64 = 60.0; // RRF denominator constant (de facto standard)

/// Compute an RRF term. rank is 1-based (0 not allowed). Err if usize → f64 conversion fails.
fn rrf_term(rank: usize) -> Result<f64> {
    // pool is at most a few hundred — exceeding u32 range is practically impossible, but the type must be the evidence.
    let r = f64::from(u32::try_from(rank).context("rrf rank to u32")?);
    Ok(1.0 / (RRF_K + r))
}

/// Shared RRF merge. Returns merged + sorted hits, origin-filtered, but not yet truncated.
fn merge_hits(
    vec_hits: Vec<Hit>,
    txt_hits: Vec<Hit>,
    exclude_origins: &[String],
) -> Result<Vec<Hit>> {
    let mut fused: HashMap<String, f64> = HashMap::new();
    let mut byid: HashMap<String, Hit> = HashMap::new();
    for (rank, h) in vec_hits.into_iter().enumerate() {
        *fused.entry(h.id.clone()).or_insert(0.0) += rrf_term(rank + 1)?;
        byid.entry(h.id.clone()).or_insert(h);
    }
    for (rank, h) in txt_hits.into_iter().enumerate() {
        *fused.entry(h.id.clone()).or_insert(0.0) += rrf_term(rank + 1)?;
        byid.entry(h.id.clone()).or_insert(h);
    }

    let mut merged: Vec<Hit> = byid
        .into_values()
        .filter(|h| !exclude_origins.iter().any(|o| o == &h.origin))
        .collect();
    merged.sort_by(|a, b| {
        fused[&b.id]
            .partial_cmp(&fused[&a.id])
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    Ok(merged)
}

/// Vector top-N + BM25 top-N → RRF position-based merge → exclude origin → top-k.
/// Optional `project`/`since_hours` narrow the pool before ranking.
pub async fn retrieve(
    store: &Store,
    llm: &Llm,
    query: &str,
    top_k: usize,
    exclude_origins: &[String],
    project: Option<&str>,
    since_hours: Option<i32>,
) -> Result<Vec<Hit>> {
    let pool = (top_k * 4).max(20);
    let qe = llm.embed(query).await?;
    let vec_hits = store
        .vector_search_filtered(&qe, pool, project, since_hours)
        .await?;
    let txt_hits = store
        .text_search_filtered(query, pool, project, since_hours)
        .await?;
    let mut merged = merge_hits(vec_hits, txt_hits, exclude_origins)?;
    merged.truncate(top_k);
    Ok(merged)
}

/// Token-/character-budget aware retrieval.
///
/// Returns up to `max_results` hits whose total `content` length does not exceed `max_chars`.
/// Each hit is individually capped to `max_chars / max_results` so a single huge chunk cannot
/// consume the whole budget. This lets agents call `recall` with a safe token ceiling.
#[allow(clippy::too_many_arguments)] // filtering flags grow the surface; a struct is overkill at 2 flags.
pub async fn retrieve_budget(
    store: &Store,
    llm: &Llm,
    query: &str,
    max_results: usize,
    max_chars: usize,
    exclude_origins: &[String],
    project: Option<&str>,
    since_hours: Option<i32>,
) -> Result<Vec<Hit>> {
    if max_results == 0 || max_chars == 0 {
        return Ok(Vec::new());
    }
    let pool = (max_results * 4).max(20);
    let qe = llm.embed(query).await?;
    let vec_hits = store
        .vector_search_filtered(&qe, pool, project, since_hours)
        .await?;
    let txt_hits = store
        .text_search_filtered(query, pool, project, since_hours)
        .await?;
    let merged = merge_hits(vec_hits, txt_hits, exclude_origins)?;

    let per_hit_cap = max_chars / max_results;
    let mut budget = max_chars;
    let mut out = Vec::new();
    for mut h in merged {
        if out.len() >= max_results {
            break;
        }
        let take = per_hit_cap.min(budget);
        if take == 0 {
            break;
        }
        let cut = h.content.chars().take(take).collect::<String>();
        if cut.is_empty() {
            continue;
        }
        budget = budget.saturating_sub(cut.chars().count());
        h.content = cut;
        out.push(h);
    }
    Ok(out)
}
