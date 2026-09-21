#!/usr/bin/env python3
"""Black-box service-contract eval for oh-my-boring retrieval.

This script does NOT test drudge internals (those live in Rust #[cfg(test)]).
It loads data/eval/golden.json and calls the live /search endpoint the same
way an external agent would, then reports Recall@k and MRR@k against the
golden fixture ids. It also reports a two-sided relevance error rate
(false_drop / false_pass) by exercising the same filter predicate that
`agents/shared/recall_core.py` ships.

Run via `make eval` (requires a live stack on :7700).
"""

import json
import os
import sys
import urllib.request

# The gate scores the predicate the agents actually run, imported rather than restated:
# a second copy would drift from the shipped behaviour while still reporting green.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "agents", "shared"))
from recall_core import RELEVANCE_MAX_DIST, exceeds_relevance_ceiling  # noqa: E402

BORING_URL = os.environ.get("BORING_URL") or "http://localhost:7700"
GOLDEN = os.path.join(os.path.dirname(__file__), "golden.json")
K = 3


def load_golden():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


def search(query, k=K):
    body = json.dumps({"query": query, "max_results": k, "max_tokens": 2000}).encode()
    req = urllib.request.Request(
        f"{BORING_URL}/search",
        data=body,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("hits", [])
    except Exception as e:
        print(f"search failed for {query!r}: {e}", file=sys.stderr)
        return []


def source_ids(hits):
    out = []
    for h in hits:
        src = h.get("source_path") or ""
        base = os.path.basename(src)
        name = base.replace(".md", "")
        # wiki-NNNN ids can't be mapped back to fixture ids without extra metadata,
        # but a synced fixture copy keeps its basename.
        out.append(name)
    return out


def over_ceiling(hit):
    """True if a filter on the shipped ceiling would have discarded this hit.

    This is `recall_core.exceeds_relevance_ceiling` itself, not a copy of it — the point of
    the gate is to score the predicate the agents actually run. Recall discards nothing on that
    ceiling, so this measures the cost of a filter that does not exist yet, which is the whole
    reason the numbers below are worth printing.
    """
    return exceeds_relevance_ceiling(hit)


def first_expected_rank(hits, expect):
    """Return (rank, hit) of the first hit whose source id is in expect, or (None, None)."""
    ids = source_ids(hits)
    for i, sid in enumerate(ids):
        if sid in expect:
            return i + 1, hits[i]
    return None, None


def main():
    golden = load_golden()
    queries = golden.get("queries", [])
    negatives = golden.get("negatives", [])
    if not queries:
        print("no queries in golden.json")
        sys.exit(0)

    recall_at_k = 0
    mrr_sum = 0.0
    false_drop = 0
    # Distances of the hits that actually matched, so the band the positives occupy is
    # visible on every run. Without this the gate can say "false_drop 0/21" while every
    # positive sits far below the ceiling — true, and worth nothing for judging a bound.
    positive_dists: list[float] = []

    for q in queries:
        query = q["query"]
        expect = q["expect"]
        hits = search(query)
        ids = source_ids(hits)
        rank, hit = first_expected_rank(hits, expect)
        if rank:
            recall_at_k += 1
            mrr_sum += 1.0 / rank
            if isinstance(hit.get("dist"), (int, float)):
                positive_dists.append(float(hit["dist"]))
            if over_ceiling(hit):
                false_drop += 1
                print(
                    f"{query!r} -> rank={rank} ids={ids} "
                    f"FALSE DROP dist={hit.get('dist')} over {RELEVANCE_MAX_DIST}"
                )
            else:
                print(f"{query!r} -> rank={rank} ids={ids}")
        else:
            print(f"{query!r} -> rank={rank} ids={ids}")

    n = len(queries)
    print(f"\nRecall@{K}: {recall_at_k}/{n} = {recall_at_k / n:.2f}")
    print(f"MRR@{K}: {mrr_sum / n:.3f}")

    # Two-sided relevance pass: negatives are queries that should be suppressed by
    # a working relevance filter. A false_pass is a negative where at least one hit
    # would survive the ceiling. Recall enforces nothing, so this gates no build; it
    # reports the error rate a future two-sided mechanism has to beat.
    false_pass = 0
    negative_dists: list[float] = []
    for neg in negatives:
        query = neg["query"]
        hits = search(query)
        near = [float(h["dist"]) for h in hits if isinstance(h.get("dist"), (int, float))]
        if near:
            negative_dists.append(min(near))
        surviving = [h for h in hits if not over_ceiling(h)]
        if surviving:
            false_pass += 1
            ids = source_ids(surviving)
            print(
                f"[negative] {query!r} -> FALSE PASS ids={ids} (survived {len(surviving)}/{len(hits)} hits)"
            )
        else:
            print(f"[negative] {query!r} -> suppressed ({len(hits)} hits dropped)")

    # The bands themselves. false_drop/false_pass are rates; these are the distances the
    # rates are computed over, and a rate cannot be read without them. If every positive
    # sits far below the ceiling, false_drop is 0 by construction rather than by merit —
    # that is exactly the state this corpus was in when the numbers below were first read.
    if positive_dists or negative_dists:
        print("\nDistance bands (what the rates above are computed over):")
        if positive_dists:
            band = sorted(positive_dists)
            mid = band[len(band) // 2]
            margin = RELEVANCE_MAX_DIST - band[-1]
            print(
                f"  positive dist: min {band[0]:.4f} / median {mid:.4f} / max {band[-1]:.4f}  (n={len(band)})"
            )
            print(
                f"  margin to ceiling {RELEVANCE_MAX_DIST}: {margin:+.4f}"
                f"{'  <-- no positive is near the ceiling; false_drop cannot discriminate' if margin > 0.05 else ''}"
            )
        if negative_dists:
            nb = sorted(negative_dists)
            print(f"  negative dist (nearest hit): min {nb[0]:.4f} / max {nb[-1]:.4f}  (n={len(nb)})")
        if positive_dists and negative_dists:
            overlap = min(negative_dists) < max(positive_dists)
            print(
                f"  bands overlap: {overlap}"
                f"{'  <-- distance alone cannot separate these two sets' if overlap else ''}"
            )

    if negatives:
        print(f"\nRelevance filter (forced ON, max_dist={RELEVANCE_MAX_DIST}):")
        print(f"  false_drop: {false_drop}/{n} positives = {false_drop / n:.3f}")
        print(f"  false_pass: {false_pass}/{len(negatives)} negatives = {false_pass / len(negatives):.3f}")

    # The gate's original exit condition is preserved and no new failing condition is added.
    # false_drop/false_pass here are real forced-ON measurements of a filter recall does not
    # run: `over_ceiling` scores the predicate directly, so the numbers are not zero by
    # construction. They gate nothing because a single distance scalar has already been ruled
    # out as the mechanism — the bands above overlap, so no value of it can score both rates
    # at once (see agents/shared/recall_core.py). This gate is the bar for the replacement:
    # a two-sided predicate has to reach false_drop 0/22 and false_pass <= 2/6 in a commit
    # other than the one that tuned it. A bound picked in the same commit that first measures
    # the quantity encodes today's accident as tomorrow's contract — that is exactly how the
    # 0.514 ceiling came to be.
    if recall_at_k < n:
        print("eval gate: FAIL")
        sys.exit(1)
    print("eval gate: PASS")


if __name__ == "__main__":
    main()
