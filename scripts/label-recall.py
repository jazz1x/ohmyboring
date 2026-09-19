#!/usr/bin/env python3
"""Label real injected recall hits so precision stops being unmeasurable.

Three modes, all against the live engine:

    label-recall.py --judge          # sample unlabelled hits, ask the local model, store verdicts
    label-recall.py --audit          # show the person only the borderline ones, store their verdicts
    label-recall.py --report         # print per-judge precision + llm/human agreement

`query_log` already records what was injected and how far it was; it records nothing about whether
it helped. That gap is why the relevance ceiling could never be judged (see recall_core.py). Every
decision here lives in `agents/shared/label_core.py` — this file is I/O only.
"""

import argparse
import json
import os
import sys
import urllib.error
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1] / "agents" / "shared"
sys.path.insert(0, str(SHARED))

import label_core  # noqa: E402
import omb_env  # noqa: E402
from drudge_client import DrudgeClient  # noqa: E402
from vault_note import split_frontmatter  # noqa: E402

#: How much of a note the judge sees. Long enough to decide, short enough that a local model
#: answers in seconds; the note's own opening carries its subject.
EXCERPT_CHARS = 1200


def vault_dir():
    return Path(
        os.environ.get("BORING_VAULT_DIR") or f"{omb_env.omb_home().rstrip('/')}/vault"
    )


def read_excerpt(path):
    """Note body for a logged hit path, or None when it is gone (pruned/renamed).

    Logged paths are engine-side (`/vault/wiki/wiki-0001.md`) or host-side; both end in the same
    basename, which is what the local vault is keyed by.
    """
    name = str(path).rsplit("/", 1)[-1]
    candidate = vault_dir() / "wiki" / name
    if not candidate.is_file():
        return None
    text = candidate.read_text(encoding="utf-8", errors="replace")
    split = split_frontmatter(text)
    return (split[1] if split else text).strip()[:EXCERPT_CHARS]


def call_judge(prompt, base_url, model, api_key, timeout=90):
    """One chat completion, JSON forced. Returns the parsed object or None.

    Same shape as the distiller's call (`agents/shared/distill_core.py::_call_llm`), including
    `reasoning_effort: none` — the local model is a thinking variant and without it a batch of
    judgements blows past the timeout and silently records nothing.
    """
    import urllib.request

    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "You emit only compact, valid JSON. No prose."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
            "reasoning_effort": "none",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions", data=payload, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return json.loads(data["choices"][0]["message"]["content"])
    except Exception as e:  # noqa: BLE001 — any failure is one missing label, never a crash
        print(f"[label-recall] judge call failed: {e}", file=sys.stderr)
        return None


def fetch(client, path, limit):
    return client._retry("GET", f"{path}?limit={limit}")  # noqa: SLF001 — same package's client


def record(client, sample, judge, verdict, model, note):
    client._retry(  # noqa: SLF001
        "POST",
        "/recall-labels",
        {
            "query_log_id": sample["query_log_id"],
            "hit_index": sample["hit_index"],
            "judge": judge,
            "verdict": verdict,
            "model": model,
            "note": note[:200],
        },
    )


def load(client, scan):
    entries = (fetch(client, "/query-log", scan) or {}).get("entries") or []
    labels = (fetch(client, "/recall-labels", 5000) or {}).get("entries") or []
    return entries, labels


def audited_keys(labels):
    """(query_log_id, hit_index) a person has already ruled on."""
    return label_core.labeled_keys(labels, label_core.JUDGE_HUMAN)


def audit_scan_depth(client, requested):
    """How deep the query_log scan has to reach to still see the rows awaiting a human label.

    Recency is the wrong selector for an audit. `--judge` labels the newest rows and search runs
    far more often than the judge does -- measured 2026-08-26, 502 searches in a day against 24
    labels -- so the newest-N window slides past the labelled rows about twenty times faster than
    they accrue. At the default depth of 200 none of the 16 judged rows were still in view and
    `--audit` reported "nothing to audit", which reads exactly like "you are done".

    The rows that need a person are already known: they carry an LLM verdict and no human one.
    Their ids say how far back the window must go, so the depth is derived rather than guessed.
    Returns `requested` unchanged when nothing is outstanding or the ids cannot be read -- a
    depth this function invented would be a number with no evidence behind it.
    """
    labels = (fetch(client, "/recall-labels", 5000) or {}).get("entries") or []
    judged = {
        (row.get("query_log_id"), row.get("hit_index"))
        for row in labels
        if row.get("judge") == label_core.JUDGE_LLM
    }
    audited = {
        (row.get("query_log_id"), row.get("hit_index"))
        for row in labels
        if row.get("judge") == label_core.JUDGE_HUMAN
    }
    outstanding = [qid for qid, _ in (judged - audited) if isinstance(qid, int)]
    if not outstanding:
        return requested
    newest = (fetch(client, "/query-log", 1) or {}).get("entries") or []
    newest_id = newest[0].get("id") if newest else None
    if not isinstance(newest_id, int):
        return requested
    return max(requested, newest_id - min(outstanding) + 1)


def run_judge(client, args):
    entries, labels = load(client, args.scan)
    samples = label_core.select_samples(
        entries, labels, judge=label_core.JUDGE_LLM, max_queries=args.queries, max_hits=args.hits
    )
    if not samples:
        print("[label-recall] nothing unlabelled in the scanned window")
        return 0
    base_url, model, api_key = omb_env.llm_base_url(), omb_env.llm_model(), omb_env.llm_api_key()
    stored = skipped = 0
    for sample in samples:
        excerpt = read_excerpt(sample["path"])
        if excerpt is None:
            # The note is gone; a label pointing at nothing would be unauditable later.
            print(f"[label-recall] skip (note missing): {sample['path']}")
            skipped += 1
            continue
        verdict = label_core.parse_verdict(
            call_judge(label_core.judge_prompt(sample["query"], excerpt), base_url, model, api_key)
        )
        if verdict is None:
            print(f"[label-recall] skip (unparseable verdict): q{sample['query_log_id']}")
            skipped += 1
            continue
        why = ""
        if args.dry_run:
            print(f"  would label q{sample['query_log_id']}#{sample['hit_index']} -> {verdict}")
            continue
        record(client, sample, label_core.JUDGE_LLM, verdict, model, why)
        stored += 1
    # Sampling deliberately bounds work per run; say what was left rather than implying coverage.
    print(
        f"[label-recall] judged {stored}, skipped {skipped}, sampled {len(samples)} "
        f"(cap {args.queries} queries x {args.hits} hits of {len(entries)} scanned)"
    )
    return 0


def run_audit(client, args):
    depth = audit_scan_depth(client, args.scan)
    if depth > args.scan:
        print(f"[label-recall] scanning {depth} rows to reach the oldest hit awaiting a person")
    entries, labels = load(client, depth)
    llm_verdicts = {
        (row["query_log_id"], row["hit_index"]): row["verdict"]
        for row in labels
        if row.get("judge") == label_core.JUDGE_LLM
    }
    # How far to walk comes from the contract, not from a habit: the audit exists to clear the
    # comparison floor, so it walks at least as many queries as the floor still owes. A query
    # yields at most a few band hits, so this is a lower bound on the work, never a promise --
    # the count printed below says what was actually found.
    owed = label_core.audit_backlog({"compared": len(audited_keys(labels))})
    walk = max(args.queries, owed)
    # Only hits the model already judged can be audited — the point is the disagreement rate —
    # and that requirement belongs in the pick, not after it: filtering afterwards left the
    # sampler counting newest-first queries the model had never reached.
    pending = label_core.select_samples(
        entries,
        labels,
        judge=label_core.JUDGE_HUMAN,
        max_queries=walk,
        max_hits=args.hits,
        require_judged_by=label_core.JUDGE_LLM,
    )
    candidates = label_core.audit_candidates(pending, llm_verdicts)
    if not candidates:
        print("[label-recall] nothing to audit (run --judge first, or no borderline hits)")
        return 0
    # Say what this sitting can and cannot finish. "3 candidates" reads as done when the floor
    # still wants 17, and a silent shortfall is how a window closes with no verdict in it.
    print(f"[label-recall] 후보 {len(candidates)}건 · 하한까지 {owed}건 남음")
    done = 0
    for index, sample in enumerate(candidates, 1):
        excerpt = read_excerpt(sample["path"])
        if excerpt is None:
            continue
        key = (sample["query_log_id"], sample["hit_index"])
        dist = sample.get("dist")
        print("\n" + "=" * 72)
        print(f"[{index}/{len(candidates)}]  기록됨 {done} · 하한까지 {max(0, owed - done)}")
        print(f"PROMPT: {sample['query'][:300]}")
        # The model's verdict and the retrieval distance are deliberately NOT shown yet. This
        # sitting exists to measure how often the person disagrees with the model, and a person
        # who has just read `llm=irrelevant` is no longer an independent reference — the number
        # the floor is protecting would be measuring anchoring instead of agreement. Both are
        # printed straight after the answer, so a disagreement is still visible while it is being
        # made rather than discovered in a report weeks later.
        print(f"NOTE  : {sample['path']}")
        print("-" * 72)
        print(excerpt[:600])
        print("-" * 72)
        answer = input("relevant? [y]es / [n]o / [u]nsure / [s]kip: ").strip().lower()
        verdict = {
            "y": label_core.VERDICT_RELEVANT,
            "n": label_core.VERDICT_IRRELEVANT,
            "u": label_core.VERDICT_UNSURE,
        }.get(answer)
        if verdict is None:
            continue
        record(client, sample, label_core.JUDGE_HUMAN, verdict, "human", "")
        done += 1
        machine = llm_verdicts.get(key)
        mark = "일치" if machine == verdict else "불일치"
        print(f"  recorded {verdict}  ·  llm={machine} ({mark})  ·  dist={dist}")
    return 0


def run_report(client, _args):
    stats = client._retry("GET", "/recall-label-stats")  # noqa: SLF001
    for line in label_core.format_report(stats or {}):
        print(line)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--judge", action="store_true", help="LLM labels unlabelled sampled hits")
    mode.add_argument("--audit", action="store_true", help="person audits the borderline ones")
    mode.add_argument("--report", action="store_true", help="print precision + agreement")
    ap.add_argument("--queries", type=int, default=5, help="queries to sample (default 5)")
    ap.add_argument("--hits", type=int, default=3, help="hits per query (default 3)")
    ap.add_argument("--scan", type=int, default=200, help="query_log rows to scan (default 200)")
    ap.add_argument("--dry-run", action="store_true", help="judge but store nothing")
    args = ap.parse_args(argv)

    client = DrudgeClient(timeout=10.0, retries=1)
    try:
        if args.judge:
            return run_judge(client, args)
        if args.audit:
            return run_audit(client, args)
        return run_report(client, args)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"[label-recall] engine unreachable: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
