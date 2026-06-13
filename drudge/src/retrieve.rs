//! Retrieval pipeline — vector + BM25 full-text → RRF merge → top-k. origin filter.
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

/// Vector top-N + BM25 top-N → RRF position-based merge → exclude origin → top-k.
pub async fn retrieve(
    store: &Store,
    llm: &Llm,
    query: &str,
    top_k: usize,
    exclude_origins: &[String],
) -> Result<Vec<Hit>> {
    let pool = (top_k * 4).max(20);
    let qe = llm.embed(query).await?;
    let vec_hits = store.vector_search(&qe, pool).await?;
    let txt_hits = store.text_search(query, pool).await?;

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
    merged.truncate(top_k);
    Ok(merged)
}
