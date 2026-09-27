//! Retrieval pipeline — vector + BM25 full-text → RRF merge → consumption feedback → top-k / budget-aware. origin filter.
//!
//! Cross-reference: design decision D3 (read door open) · ENFORCEMENT.md §B (one-way flow).
//!   - No rewriting/reranker (personal scale = simplest thing that works).
//!   - Verdicts feed back into ranking: a document's net `used − contested` nudges every one
//!     of its chunks' RRF scores, so what the owner already judged 👍/👎 moves the next answer.
use std::cmp::Reverse;
use std::collections::HashMap;
use std::time::SystemTime;

use anyhow::{Context, Result};

use crate::llm::Llm;
use crate::store::{Authorship, Hit, RankFacts, Store};
use crate::wiki_recall::WikiHit;

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
    let counts = match store.ranking_feedback_counts(&paths).await {
        Ok(counts) => counts,
        Err(e) => {
            eprintln!(
                "apply_feedback: ranking_feedback_counts failed ({e:#}); ranking without verdict feedback"
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
) -> Result<Vec<Scored>> {
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

    let mut merged: Vec<Scored> = byid
        .into_values()
        .filter(|h| !exclude_origins.iter().any(|o| o == &h.origin))
        .map(|hit| Scored {
            score: fused[&hit.id],
            hit,
        })
        .collect();
    merged.sort_by(|a, b| b.score.total_cmp(&a.score));
    Ok(merged)
}

/// A hit with its fused score, carried past the cut so the in-set order can still read it.
struct Scored {
    hit: Hit,
    score: f64,
}

/// The one order inside a returned set: superseded last, owner notes first and newest first
/// among themselves, then score.
fn rank_key(facts: Option<&RankFacts>) -> (bool, bool, Reverse<Option<SystemTime>>) {
    match facts.copied() {
        Some(RankFacts {
            superseded,
            authorship: Authorship::Owner { updated_at },
        }) => (superseded, false, Reverse(Some(updated_at))),
        Some(RankFacts {
            superseded,
            authorship: Authorship::Other,
        }) => (superseded, true, Reverse(None)),
        None => (false, true, Reverse(None)),
    }
}

/// What `order_within_set` reads from a member of an already-cut set.
trait InSet {
    fn path(&self) -> &str;
    fn score(&self) -> f64;
}

impl InSet for Scored {
    fn path(&self) -> &str {
        &self.hit.source_path
    }
    fn score(&self) -> f64 {
        self.score
    }
}

impl InSet for WikiHit {
    fn path(&self) -> &str {
        &self.source_path
    }
    fn score(&self) -> f64 {
        f64::from(self.score)
    }
}

/// Membership is decided by score first; only then is the returned set ordered by `rank_key`.
/// A note outside the cut is never pulled in, and a superseded note is demoted, never cut.
async fn order_within_set<T, F, Fut>(mut set: Vec<T>, lookup: F) -> Result<Vec<T>>
where
    T: InSet,
    F: FnOnce(Vec<String>) -> Fut,
    Fut: Future<Output = Result<HashMap<String, RankFacts>>>,
{
    let mut paths: Vec<String> = set.iter().map(|s| s.path().to_owned()).collect();
    paths.sort_unstable();
    paths.dedup();
    let facts = lookup(paths).await.context("rank: rank facts lookup")?;
    set.sort_by(|a, b| {
        rank_key(facts.get(a.path()))
            .cmp(&rank_key(facts.get(b.path())))
            .then_with(|| b.score().total_cmp(&a.score()))
    });
    Ok(set)
}

/// The wiki recall set, already cut by word overlap, in the same in-set order as `retrieve`.
pub async fn order_wiki_hits(store: &Store, hits: Vec<WikiHit>) -> Result<Vec<WikiHit>> {
    order_within_set(hits, |paths| async move { store.rank_facts(&paths).await }).await
}

async fn top_k_ranked<F, Fut>(mut merged: Vec<Scored>, top_k: usize, lookup: F) -> Result<Vec<Hit>>
where
    F: FnOnce(Vec<String>) -> Fut,
    Fut: Future<Output = Result<HashMap<String, RankFacts>>>,
{
    merged.truncate(top_k);
    Ok(hits_of(order_within_set(merged, lookup).await?))
}

fn hits_of(set: Vec<Scored>) -> Vec<Hit> {
    set.into_iter().map(|s| s.hit).collect()
}

async fn budget_ranked<F, Fut>(
    merged: Vec<Scored>,
    max_results: usize,
    max_chars: usize,
    lookup: F,
) -> Result<Vec<Hit>>
where
    F: FnOnce(Vec<String>) -> Fut,
    Fut: Future<Output = Result<HashMap<String, RankFacts>>>,
{
    Ok(hits_of(
        order_within_set(within_budget(merged, max_results, max_chars), lookup).await?,
    ))
}

fn within_budget(merged: Vec<Scored>, max_results: usize, max_chars: usize) -> Vec<Scored> {
    let per_hit_cap = max_chars / max_results;
    let mut budget = max_chars;
    let mut out = Vec::new();
    for mut s in merged {
        if out.len() >= max_results {
            break;
        }
        let take = per_hit_cap.min(budget);
        if take == 0 {
            break;
        }
        let cut = s.hit.content.chars().take(take).collect::<String>();
        if cut.is_empty() {
            continue;
        }
        budget = budget.saturating_sub(cut.chars().count());
        s.hit.content = cut;
        out.push(s);
    }
    out
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
    let merged = merge_hits(store, vec_hits, txt_hits, exclude_origins).await?;
    top_k_ranked(merged, top_k, |paths| async move {
        store.rank_facts(&paths).await
    })
    .await
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
    budget_ranked(merged, max_results, max_chars, |paths| async move {
        store.rank_facts(&paths).await
    })
    .await
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

    fn score_ordered_pool() -> Vec<Scored> {
        ["old", "new", "b", "c", "d"]
            .into_iter()
            .enumerate()
            .map(|(rank, n)| Scored {
                hit: Hit {
                    content: "x".to_owned(),
                    ..hit(&format!("{n}#0"), &format!("/{n}.md"))
                },
                score: rrf_term(rank + 1).unwrap(),
            })
            .collect()
    }

    fn ids(hits: Vec<Hit>) -> Vec<String> {
        hits.into_iter().map(|h| h.id).collect()
    }

    fn other(superseded: bool) -> RankFacts {
        RankFacts {
            superseded,
            authorship: Authorship::Other,
        }
    }

    fn owner(superseded: bool, secs: u64) -> RankFacts {
        RankFacts {
            superseded,
            authorship: Authorship::Owner {
                updated_at: SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(secs),
            },
        }
    }

    fn facts<const N: usize>(rows: [(&str, RankFacts); N]) -> HashMap<String, RankFacts> {
        rows.into_iter().map(|(p, f)| (p.to_owned(), f)).collect()
    }

    #[tokio::test]
    async fn superseded_top_scorer_is_returned_after_the_live_notes() {
        let superseded = facts([("/old.md", other(true)), ("/new.md", other(false))]);
        let found = |_| async { Ok(superseded.clone()) };
        let none = |_| async { Ok(HashMap::new()) };

        let top = top_k_ranked(score_ordered_pool(), 3, none).await.unwrap();
        assert_eq!(ids(top), ["old#0", "new#0", "b#0"], "control: score order");

        let top = top_k_ranked(score_ordered_pool(), 3, found).await.unwrap();
        assert_eq!(ids(top), ["new#0", "b#0", "old#0"], "demoted, not cut");

        let budget = budget_ranked(score_ordered_pool(), 3, 300, found)
            .await
            .unwrap();
        assert_eq!(ids(budget), ["new#0", "b#0", "old#0"], "budget path too");
    }

    #[tokio::test]
    async fn owner_notes_lead_the_returned_set_newest_first_and_nothing_is_pulled_in() {
        let rank = |rows: HashMap<String, RankFacts>| async move {
            let top = top_k_ranked(score_ordered_pool(), 3, |_| async { Ok(rows.clone()) })
                .await
                .unwrap();
            let budget =
                budget_ranked(score_ordered_pool(), 3, 300, |_| async { Ok(rows.clone()) })
                    .await
                    .unwrap();
            let (top, budget) = (ids(top), ids(budget));
            assert_eq!(top, budget, "top-k and budget paths share one order");
            top
        };

        assert_eq!(
            rank(facts([("/b.md", owner(false, 10))])).await,
            ["b#0", "old#0", "new#0"],
            "the owner note sits above higher-scoring notes of the set"
        );
        assert_eq!(
            rank(facts([
                ("/new.md", owner(false, 10)),
                ("/b.md", owner(false, 20))
            ]))
            .await,
            ["b#0", "new#0", "old#0"],
            "owner notes newest first, whatever their score"
        );
        assert_eq!(
            rank(facts([
                ("/old.md", owner(true, 30)),
                ("/b.md", owner(false, 10))
            ]))
            .await,
            ["b#0", "new#0", "old#0"],
            "a superseded owner note is still last"
        );
        assert_eq!(
            rank(facts([("/d.md", owner(false, 10))])).await,
            ["old#0", "new#0", "b#0"],
            "an owner note outside the score cut is not pulled in"
        );
    }

    #[tokio::test]
    async fn failed_rank_facts_lookup_fails_the_retrieval() {
        let failing = |_| async { Err(anyhow::anyhow!("db down")) };
        let err = top_k_ranked(score_ordered_pool(), 3, failing)
            .await
            .unwrap_err();
        assert!(format!("{err:#}").contains("rank facts lookup"));
    }
}
