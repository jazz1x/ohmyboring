#!/usr/bin/env python3
"""Tests for label_core.py — sampling, verdict parsing, and the refusal to report a tiny sample."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import label_core


def _entry(qid, query="how did I fix the pool", paths=None, dists=None, kinds=None, endpoint="search"):
    paths = ["vault/wiki/wiki-0001.md", "vault/wiki/wiki-0002.md"] if paths is None else paths
    return {
        "id": qid,
        "endpoint": endpoint,
        "query": query,
        "hit_paths": paths,
        "hit_dists": [0.30, 0.52] if dists is None else dists,
        "hit_dist_kinds": ["vector_cosine", "vector_cosine"] if kinds is None else kinds,
    }


def test_samples_carry_hit_position_and_distance():
    samples = label_core.select_samples([_entry(7)], [])
    assert [(s["query_log_id"], s["hit_index"]) for s in samples] == [(7, 0), (7, 1)]
    assert samples[0]["dist"] == 0.30 and samples[1]["dist"] == 0.52


def test_already_labeled_pairs_are_skipped_per_judge():
    labels = [{"query_log_id": 7, "hit_index": 0, "judge": "llm", "verdict": "relevant"}]
    llm = label_core.select_samples([_entry(7)], labels, judge=label_core.JUDGE_LLM)
    assert [s["hit_index"] for s in llm] == [1], "the llm must not relabel its own verdict"
    human = label_core.select_samples([_entry(7)], labels, judge=label_core.JUDGE_HUMAN)
    assert [s["hit_index"] for s in human] == [0, 1], "an llm label must not block a human audit"


def test_non_search_endpoints_are_not_sampled():
    entries = [_entry(1, endpoint="brief"), _entry(2)]
    assert {s["query_log_id"] for s in label_core.select_samples(entries, [])} == {2}


def test_caps_bound_queries_and_hits():
    entries = [
        _entry(i, paths=[f"n{j}.md" for j in range(5)], dists=[0.3] * 5, kinds=["vector_cosine"] * 5)
        for i in range(9)
    ]
    samples = label_core.select_samples(entries, [], max_queries=2, max_hits=3)
    assert len(samples) == 6
    assert len({s["query_log_id"] for s in samples}) == 2


def test_missing_distance_stays_none_not_zero():
    samples = label_core.select_samples([_entry(3, dists=[], kinds=[])], [])
    assert samples[0]["dist"] is None
    assert label_core.in_audit_band(samples[0]["dist"]) is False


def test_audit_targets_unsure_and_the_decision_band_only():
    samples = label_core.select_samples([_entry(4, dists=[0.20, 0.50])], [])
    verdicts = {(4, 0): "relevant", (4, 1): "relevant"}
    picked = label_core.audit_candidates(samples, verdicts)
    assert [s["hit_index"] for s in picked] == [1], "0.20 is not a borderline call; 0.50 is"
    verdicts[(4, 0)] = "unsure"
    assert len(label_core.audit_candidates(samples, verdicts)) == 2


def test_verdict_parsing_rejects_anything_not_asked_for():
    assert label_core.parse_verdict({"verdict": "Relevant"}) == "relevant"
    assert label_core.parse_verdict({"verdict": "maybe"}) is None
    assert label_core.parse_verdict({"why": "no verdict key"}) is None
    assert label_core.parse_verdict("relevant") is None
    assert label_core.parse_verdict(None) is None


def test_precision_refuses_a_sample_under_the_floor():
    assert label_core.precision(10, 5) is None
    assert label_core.precision(0, 0) is None
    assert label_core.precision(label_core.MIN_DECIDED, 0) == 1.0
    assert label_core.precision(15, 15) == 0.5


def test_agreement_and_llm_usability_default_to_unusable():
    assert label_core.agreement(5, 5) is None
    assert label_core.llm_is_usable(5, 5) is False, "too few audits is not permission"
    assert label_core.agreement(20, label_core.MIN_COMPARED) == 1.0
    assert label_core.llm_is_usable(19, 20) is True
    assert label_core.llm_is_usable(15, 20) is False, "0.75 is below the floor"


def test_report_prints_counts_not_zero_percent_when_short():
    stats = {
        "judges": [{"judge": "llm", "relevant": 3, "irrelevant": 1, "unsure": 2}],
        "agreed": 2,
        "compared": 3,
    }
    lines = label_core.format_report(stats)
    joined = "\n".join(lines)
    assert "판단 보류" in joined
    assert "0.0" not in joined and "0.000" not in joined, "a short sample must not render a rate"
    assert "decided 4" in joined and "unsure 2" in joined
    assert "라벨 0건" in joined, "a judge with no labels at all must still be named"


def test_report_shows_rates_and_the_usability_verdict_when_the_sample_is_enough():
    stats = {
        "judges": [
            {"judge": "llm", "relevant": 20, "irrelevant": 20, "unsure": 1},
            {"judge": "human", "relevant": 30, "irrelevant": 10, "unsure": 0},
        ],
        "agreed": 30,
        "compared": 40,
    }
    joined = "\n".join(label_core.format_report(stats))
    assert "llm: precision 0.500 (n=40" in joined
    assert "human: precision 0.750 (n=40" in joined
    assert "일치율 0.750" in joined and "LLM 라벨 지표 제외" in joined


def test_prompt_asks_whether_it_helped_not_whether_it_is_related():
    prompt = label_core.judge_prompt("why did the pool die", "the pool died because ...")
    assert "would reading this note have helped" in prompt
    assert "why did the pool die" in prompt and "the pool died because" in prompt
    for verdict in label_core.VERDICTS:
        assert verdict in prompt


def test_the_audit_pick_reaches_past_queries_the_model_has_not_judged():
    """Requiring the model's verdict must happen during the pick, not after it.

    The model labels the newest rows and search runs far more often than the judge does, so the
    newest queries are exactly the ones with no verdict yet. Taking the newest `max_queries` and
    then keeping only the judged ones therefore yields nothing — measured on the live ledger,
    0 candidates while 24 hits sat waiting for a person.
    """
    # Newest first, as /query-log returns them. Only the oldest has been judged.
    entries = [_entry(qid) for qid in (100, 99, 98, 97, 96, 95)]
    labels = [
        {"query_log_id": 95, "hit_index": 0, "judge": label_core.JUDGE_LLM, "verdict": "relevant"},
        {"query_log_id": 95, "hit_index": 1, "judge": label_core.JUDGE_LLM, "verdict": "irrelevant"},
    ]

    filtered_after = [
        s
        for s in label_core.select_samples(entries, labels, judge=label_core.JUDGE_HUMAN, max_queries=2)
        if (s["query_log_id"], s["hit_index"]) in {(95, 0), (95, 1)}
    ]
    assert filtered_after == [], "the old sample-then-filter order must still come up empty"

    picked = label_core.select_samples(
        entries,
        labels,
        judge=label_core.JUDGE_HUMAN,
        max_queries=2,
        require_judged_by=label_core.JUDGE_LLM,
    )
    assert [(s["query_log_id"], s["hit_index"]) for s in picked] == [(95, 0), (95, 1)], picked


def test_a_hit_the_person_already_ruled_on_is_not_offered_again():
    entries = [_entry(95)]
    labels = [
        {"query_log_id": 95, "hit_index": 0, "judge": label_core.JUDGE_LLM, "verdict": "relevant"},
        {"query_log_id": 95, "hit_index": 1, "judge": label_core.JUDGE_LLM, "verdict": "relevant"},
        {"query_log_id": 95, "hit_index": 0, "judge": label_core.JUDGE_HUMAN, "verdict": "relevant"},
    ]
    picked = label_core.select_samples(
        entries,
        labels,
        judge=label_core.JUDGE_HUMAN,
        max_queries=5,
        require_judged_by=label_core.JUDGE_LLM,
    )
    assert [(s["query_log_id"], s["hit_index"]) for s in picked] == [(95, 1)], picked


def test_audit_backlog_counts_down_to_the_floor_and_stops():
    assert label_core.audit_backlog({"compared": 0}) == label_core.MIN_COMPARED
    assert label_core.audit_backlog({"compared": 4}) == label_core.MIN_COMPARED - 4
    assert label_core.audit_backlog({"compared": label_core.MIN_COMPARED}) == 0
    # Past the floor is done, not negative work.
    assert label_core.audit_backlog({"compared": label_core.MIN_COMPARED + 9}) == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - label_core sampling, verdicts, and report floors")
