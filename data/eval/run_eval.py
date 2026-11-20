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
from dataclasses import dataclass

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


PAIRS = os.path.join(os.path.dirname(__file__), "graph_pairs.json")
PG_DSN = os.environ.get("BORING_EVAL_PG_DSN") or "postgresql://boring:boring@127.0.0.1:5432/boring"
WIDE = 20  # how far down the off ranking a target's rank is read


def search_full(query, k, related, related_heads, max_tokens=2000):
    """Like `search` but keeps each hit's `related` and returns None on failure (못 잼, not 0 hits)."""
    body = json.dumps(
        {
            "query": query,
            "max_results": k,
            "max_tokens": max_tokens,
            "related": related,
            "related_heads": related_heads,
        }
    ).encode()
    req = urllib.request.Request(
        f"{BORING_URL}/search", data=body, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("hits", [])
    except Exception as e:
        print(f"search failed for {query!r}: {e}", file=sys.stderr)
        return None


@dataclass(frozen=True)
class Ground:
    """What the store says about a pair: the edge score H·T and both updated_at values."""

    shared: int
    head_at: object
    target_at: object


@dataclass(frozen=True)
class Invalid:
    n: int
    reason: str


@dataclass(frozen=True)
class Valid:
    n: int
    target_off_rank: int | None  # rank in the top WIDE of the off ranking, None = not in it
    target_on: str  # "hit" | "related" | "missing"


_SHARED_SQL = (
    "SELECT coalesce(sum(CASE WHEN a.kind = 'claims' THEN 2 ELSE 1 END), 0)"
    " FROM edge a JOIN edge b ON a.dst = b.dst AND a.kind = b.kind"
    " WHERE a.src = %s AND b.src = %s"
    " AND (a.kind = 'claims' OR (a.kind = 'about' AND a.dst LIKE 'concept:%%'))"
)
_UPDATED_SQL = "SELECT source_path, updated_at FROM document WHERE source_path LIKE %s"


def ground_from_pg(head, target):
    """Ground | str. A str is the reason the store could not be read (no psycopg, no DB, missing row)."""
    try:
        import psycopg  # CI installs it from requirements.txt; absent -> unmeasurable, not skipped
    except ImportError as e:
        return f"cannot measure: psycopg missing ({e})"
    try:
        with psycopg.connect(PG_DSN, connect_timeout=5) as conn:
            rows = {}
            for name in (head, target):
                got = conn.execute(_UPDATED_SQL, (f"%/{name}.md",)).fetchall()
                if len(got) != 1:
                    return f"cannot measure: {len(got)} document rows for {name}.md (want 1)"
                rows[name] = got[0]
            nodes = (f"doc:{rows[head][0]}", f"doc:{rows[target][0]}")
            (shared,) = conn.execute(_SHARED_SQL, nodes).fetchone()
            return Ground(int(shared), rows[head][1], rows[target][1])
    except Exception as e:
        return f"cannot measure: {type(e).__name__}: {e}"


def judge_pair(pair, ground, search):
    """Invalid(reason) | Valid. Pre-check on the store first, then the off assertion, then the on read."""
    n, query = pair["n"], pair["query"]
    head, target = f"eval-mh-{n}-head", f"eval-mh-{n}-target"
    g = ground(head, target)
    if isinstance(g, str):
        return Invalid(n, g)
    if g.shared < 2:
        return Invalid(n, f"edge score H·T = {g.shared} (< 2)")
    if not g.target_at < g.head_at:
        return Invalid(n, f"T.updated_at {g.target_at} not earlier than H.updated_at {g.head_at}")
    off = search(query, WIDE, 0, 0, 16384)
    if off is None:
        return Invalid(n, "off search failed")
    ids = source_ids(off)
    if head not in ids[:K]:
        return Invalid(n, f"off: H not in top {K} (ids={ids[:K]})")
    if target in ids[:K]:
        return Invalid(n, f"off: T already in top {K} at rank {ids.index(target) + 1}")
    target_off_rank = ids.index(target) + 1 if target in ids else None
    on = search(query, K, 3, 3)
    if on is None:
        return Invalid(n, "on search failed")
    if target in source_ids(on):
        where = "hit"
    elif target in related_names(on):
        where = "related"
    else:
        where = "missing"
    return Valid(n, target_off_rank, where)


def related_names(hits):
    return [
        os.path.basename(r.get("source_path") or "").replace(".md", "")
        for h in hits
        for r in (h.get("related") or [])
    ]


def simple_lookups_on(queries, search):
    """(answers in top K, extra docs per query, extra chars per query, failures) with the graph path on."""
    kept = extra_docs = extra_chars = 0
    for q in queries:
        hits = search(q["query"], K, 3, 3)
        if hits is None:
            continue
        ids = source_ids(hits)
        if any(sid in q["expect"] for sid in ids[:K]):
            kept += 1
        seen = set(ids)
        extra_docs += len({r for r in related_names(hits) if r not in seen})
        extra_chars += sum(len(r.get("snippet") or "") for h in hits for r in (h.get("related") or []))
    n = len(queries)
    return kept, extra_docs / n, extra_chars / n


def run_graph(search=search_full, ground=ground_from_pg):
    """Exit code. Side by side: how many answers the graph path hands over off vs on."""
    with open(PAIRS, encoding="utf-8") as f:
        pairs = json.load(f)["pairs"]
    results = [judge_pair(p, ground, search) for p in pairs]
    for r in results:
        match r:
            case Invalid(n, reason):
                print(f"pair {n}: INVALID — {reason}")
            case Valid(n, rank, where):
                print(f"pair {n}: valid — T off rank {rank if rank else f'>{WIDE}'} · on: {where}")
    valid = [r for r in results if isinstance(r, Valid)]
    v = len(valid)
    a = 0  # off: a valid pair has T outside the top K by construction of the assertion
    b = sum(1 for r in valid if r.target_on != "missing")
    print(f"multi-hop off {a}/{v} · on {b}/{v} (valid {v} of {len(pairs)})")

    queries = load_golden().get("queries", [])
    kept, docs, chars = simple_lookups_on(queries, search)
    n = len(queries)
    print(
        f"simple lookups, graph on: answer in top {K} {kept}/{n} · "
        f"extra docs/query {docs:.2f} · extra chars/query {chars:.0f}"
    )
    print(
        "scope: synthetic pairs measure plumbing reach (does the graph path hand over the answer) "
        "and noise on simple lookups; value on real questions is NOT measured here."
    )
    if v == 0:
        print("graph eval: FAIL (no valid pair — denominator 0)")
        return 1
    if kept < n:
        print(f"graph eval: FAIL (regression: {n - kept} of {n} simple lookups lost their answer)")
        return 1
    print("graph eval: PASS")
    return 0


def main():
    if "--graph" in sys.argv[1:]:
        sys.exit(run_graph())
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
