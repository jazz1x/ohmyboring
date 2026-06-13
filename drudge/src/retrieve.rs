//! 회수 파이프 — 벡터 + BM25 full-text → RRF 병합 → top-k. origin 필터.
//!   - 재작성/리랭커는 없음(개인 스케일 = simplest thing that works).
use std::collections::HashMap;

use anyhow::{Context, Result};

use crate::llm::Llm;
use crate::store::{Hit, Store};

const RRF_K: f64 = 60.0; // RRF 분모 상수(사실상 표준)

/// RRF 항 계산. rank 는 1-based(0 불가). usize → f64 변환 실패 시 Err.
fn rrf_term(rank: usize) -> Result<f64> {
    // pool 은 최대 수백 수준 — u32 범위 초과는 실질적으로 없지만 타입이 증거여야 한다.
    let r = f64::from(u32::try_from(rank).context("rrf rank to u32")?);
    Ok(1.0 / (RRF_K + r))
}

/// 벡터 top-N + BM25 top-N → RRF 위치기반 병합 → origin 제외 → top-k.
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
