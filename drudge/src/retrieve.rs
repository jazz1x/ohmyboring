//! Retrieval pipeline — vector + BM25 full-text → RRF merge → consumption feedback → top-k / budget-aware. origin filter.
//!
//! Cross-reference: design decision D3 (read door open) · ENFORCEMENT.md §B (one-way flow).
//!   - No rewriting/reranker (personal scale = simplest thing that works).
//!   - Verdicts feed back into ranking: a document's net `used − contested` nudges every one
//!     of its chunks' RRF scores, so what the owner already judged 👍/👎 moves the next answer.
use std::collections::HashMap;

use anyhow::{Context, Result};

use crate::llm::Llm;
use crate::store::{Hit, Store};

const RRF_K: f64 = 60.0; // RRF denominator constant (de facto standard)

/// One 👍 = one rank step up: the exact score gap between rank 1 and rank 2 in a single list.
/// Written against RRF_K so the step and the RRF terms can never drift apart.
const FEEDBACK_STEP: f64 = 1.0 / (RRF_K + 1.0) - 1.0 / (RRF_K + 2.0);

/// Cap on a document's net verdict: three 👍 at most. A pile of reactions must not flip ranking
/// wholesale — ±FEEDBACK_NET_MAX steps against ≈ 2·rrf_term(1) for a both-lists topper.
const FEEDBACK_NET_MAX: i64 = 3;

/// Compute an RRF term. rank is 1-based (0 not allowed). Err if usize → f64 conversion fails.
fn rrf_term(rank: usize) -> Result<f64> {
    // pool is at most a few hundred — exceeding u32 range is practically impossible, but the type must be the evidence.
    let r = f64::from(u32::try_from(rank).context("rrf rank to u32")?);
    Ok(1.0 / (RRF_K + r))
}

/// Net verdict for one document: `used − contested`, clamped to ±FEEDBACK_NET_MAX.
fn net_feedback(used: i64, contested: i64) -> i64 {
    (used - contested).clamp(-FEEDBACK_NET_MAX, FEEDBACK_NET_MAX)
}

/// Add one document's net verdict to every chunk score of that document. The verdict is
/// document-level (consumption edges point at the note), the score is chunk-level — all chunks
/// of the same note move together.
fn apply_net(fused: &mut HashMap<String, f64>, byid: &HashMap<String, Hit>, path: &str, net: i64) {
    if net == 0 {
        return;
    }
    for (id, hit) in byid {
        if hit.source_path == path
            && let Some(score) = fused.get_mut(id)
        {
            // |net| ≤ FEEDBACK_NET_MAX = 3 — the i64→f64 cast loses nothing at this magnitude.
            #[allow(clippy::cast_precision_loss)]
            let delta = net as f64 * FEEDBACK_STEP;
            *score += delta;
        }
    }
}

/// Fold document-level consumption verdicts (`used`/`contested` edges) into the fused RRF
/// scores: `score += clamp(used − contested, ±FEEDBACK_NET_MAX) × FEEDBACK_STEP`. Runs once per
/// retrieval, before the single sort, so every caller — `/search`, MCP `recall`, `/ask` + brief,
/// CLI — ranks with the feedback in. A failed count read is a one-line log and a plain ranking,
/// never a dead search.
async fn apply_feedback(
    store: &Store,
    fused: &mut HashMap<String, f64>,
    byid: &HashMap<String, Hit>,
) -> Result<()> {
    let mut paths: Vec<String> = byid.values().map(|h| h.source_path.clone()).collect();
    paths.sort_unstable();
    paths.dedup();
    let counts = match store.consumption_counts(&paths).await {
        Ok(counts) => counts,
        Err(e) => {
            eprintln!(
                "apply_feedback: consumption_counts failed ({e:#}); ranking without verdict feedback"
            );
            return Ok(());
        }
    };
    for (path, c) in &counts {
        apply_net(fused, byid, path, net_feedback(c.used, c.contested));
    }
    Ok(())
}

/// Shared RRF merge. Fuses both hit lists, folds in consumption feedback, then sorts once —
/// origin-filtered, but not yet truncated.
async fn merge_hits(
    store: &Store,
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
    apply_feedback(store, &mut fused, &byid).await?;

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
    let mut merged = merge_hits(store, vec_hits, txt_hits, exclude_origins).await?;
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
    let merged = merge_hits(store, vec_hits, txt_hits, exclude_origins).await?;

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

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        clippy::panic,
        clippy::float_cmp // the assertions are exactness checks — same computation path, same bits
    )]

    use super::*;
    use crate::store::DistKind;

    fn hit(id: &str, path: &str) -> Hit {
        Hit {
            id: id.to_owned(),
            content: String::new(),
            origin: "personal".to_owned(),
            project: "test".to_owned(),
            source_path: path.to_owned(),
            dist: 0.0,
            dist_kind: DistKind::VectorCosine,
        }
    }

    #[test]
    fn net_feedback_clamps_used_minus_contested() {
        assert_eq!(net_feedback(0, 0), 0);
        assert_eq!(net_feedback(1, 0), 1);
        assert_eq!(net_feedback(0, 1), -1);
        assert_eq!(net_feedback(10, 0), 3, "the cap is FEEDBACK_NET_MAX");
        assert_eq!(net_feedback(0, 10), -3);
        assert_eq!(net_feedback(5, 5), 0, "a wash reads as no verdict");
    }

    #[test]
    fn feedback_step_is_exactly_one_rrf_rank() {
        let step = rrf_term(1).unwrap() - rrf_term(2).unwrap();
        assert_eq!(
            FEEDBACK_STEP, step,
            "one 👍 = one rank step in a single list"
        );
    }

    #[test]
    fn net_plus_one_ranks_above_net_minus_one() {
        let mut fused: HashMap<String, f64> = HashMap::from([
            ("a#0".to_owned(), 0.5),
            ("a#1".to_owned(), 0.5), // two chunks of the same document
            ("b#0".to_owned(), 0.5),
        ]);
        let byid: HashMap<String, Hit> = HashMap::from([
            ("a#0".to_owned(), hit("a#0", "/a.md")),
            ("a#1".to_owned(), hit("a#1", "/a.md")),
            ("b#0".to_owned(), hit("b#0", "/b.md")),
        ]);

        apply_net(&mut fused, &byid, "/a.md", 1);
        apply_net(&mut fused, &byid, "/b.md", -1);

        assert!(fused["a#0"] > fused["b#0"], "net +1 ranks above net −1");
        assert_eq!(
            fused["a#0"], fused["a#1"],
            "a document's net moves every chunk of that document by the same amount"
        );

        // A net of 0 is a no-op — golden corpora with no consumption edges rank byte-identical.
        let before = fused["b#0"];
        apply_net(&mut fused, &byid, "/b.md", 0);
        assert_eq!(fused["b#0"], before);
    }
}
