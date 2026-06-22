#!/usr/bin/env python3
"""Black-box service-contract eval for oh-my-boring retrieval.

This script does NOT test drudge internals (those live in Rust #[cfg(test)]).
It loads data/eval/golden.json and calls the live /search endpoint the same
way an external agent would, then reports Recall@k and MRR@k against the
golden fixture ids.

Run via `make eval` (requires a live stack on :7700).
"""
import json
import os
import sys
import urllib.request

DRUDGE_URL = os.environ.get("DRUDGE_URL") or "http://localhost:7700"
GOLDEN = os.path.join(os.path.dirname(__file__), "golden.json")
K = 3


def load_golden():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


def search(query, k=K):
    body = json.dumps({"query": query, "max_results": k, "max_tokens": 2000}).encode()
    req = urllib.request.Request(
        f"{DRUDGE_URL}/search",
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


def main():
    golden = load_golden()
    queries = golden.get("queries", [])
    if not queries:
        print("no queries in golden.json")
        sys.exit(0)

    recall_at_k = 0
    mrr_sum = 0.0

    for q in queries:
        query = q["query"]
        expect = q["expect"]
        hits = search(query)
        ids = source_ids(hits)
        rank = next((i + 1 for i, sid in enumerate(ids) if sid in expect), None)
        if rank:
            recall_at_k += 1
            mrr_sum += 1.0 / rank
        print(f"{query!r} -> rank={rank} ids={ids}")

    n = len(queries)
    print(f"\nRecall@{K}: {recall_at_k}/{n} = {recall_at_k / n:.2f}")
    print(f"MRR@{K}: {mrr_sum / n:.3f}")
    if recall_at_k < n:
        print("eval gate: FAIL")
        sys.exit(1)
    print("eval gate: PASS")


if __name__ == "__main__":
    main()
