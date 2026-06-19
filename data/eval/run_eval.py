#!/usr/bin/env python3
"""Retrieval-quality eval gate for ohmyboring (drudge).

WHAT IT DOES
  1. Reads the self-contained fixture wiki notes in data/eval/fixtures/.
  2. Ingests each one into the RUNNING drudge via the product's own ingest path
     — the MCP `remember` tool over /mcp (JSON-RPC 2.0 tools/call). drudge
     allocates the wiki-NNNN id; we capture it from the tool's text reply and
     map fixture-local-id -> allocated wiki id.
  3. Runs every golden query through the product's recall path — POST /search —
     and also exercises POST /ask for an answer-presence signal.
  4. Scores recall@1 and MRR by matching the expected fixtures' allocated wiki
     ids against the wiki ids of the returned hits, prints a per-query table +
     totals, and with --check exits non-zero below the recall@1 floor.

HOW TO RUN
  Needs the full stack up (drudge on :7700) — usually via `make up`. Then:
      python3 data/eval/run_eval.py            # report only
      python3 data/eval/run_eval.py --check    # gate: exit 1 if recall@1 < floor
  Or just `make eval` (scripts/eval-gate.sh runs --check when the stack is up).
  Endpoint base from $DRUDGE_URL (default http://127.0.0.1:7700).

GROUNDING (every request shape is grounded in drudge/src/serve.rs + scripts/smoke.sh)
  - POST /search  {query, max_results}        -> {hits:[{id, source_path, ...}]}
        serve.rs:1007 route; SearchReq serve.rs:91-98; SearchResp/SearchHit 108-120.
  - POST /ask     {question}                  -> {answer, sources}
        serve.rs:1005 route; AskReq 80-83; AskResp 85-89; smoke.sh step 3.
  - POST /mcp     JSON-RPC tools/call name=remember args={title,body,tags,
        tools,concepts,origin,repo}           -> result.content[0].text
        serve.rs:1011 route; handle_mcp 264-287; remember tool 474, 656-696;
        arg names from the tools/list inputSchema 320-352; reply text 680/692.
  - GET  /health                              -> 200 (readiness probe)
        serve.rs:1004 route; smoke.sh step 2.

  Wiki-id matching is mode-agnostic. In wiki mode SearchHit.id is the wiki stem
  (wiki_recall.rs:155). In vector mode SearchHit.id is "{source_path}#{idx}"
  (store.rs:24), but source_path still ends in the wiki file. So we always derive
  the wiki id from id-or-source_path via the wiki-NNNN stem.

NOTE ON SIDE EFFECTS
  `remember` writes real wiki notes into the running vault — that is the only
  ingest entry drudge exposes over the wire, and it is what the product uses.
  The notes are clearly tagged `eval` and id-prefixed `eval-` in their bodies so
  they are easy to spot; this harness is meant for an eval/CI stack, not a vault
  you care about. No external deps beyond the Python standard library.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
GOLDEN_PATH = Path(__file__).resolve().parent / "golden.json"
DRUDGE_URL = os.environ.get("DRUDGE_URL", "http://127.0.0.1:7700").rstrip("/")

# Conservative first-cut floor. Below this the recall path has regressed.
RECALL_AT_1_FLOOR = 0.6

WIKI_ID_RE = re.compile(r"wiki-\d{4,5}")


def http_json(path, payload=None, method="POST", timeout=120):
    """POST/GET JSON to drudge and return the parsed JSON body."""
    url = f"{DRUDGE_URL}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else {}


def health_ok(timeout=5):
    """Readiness probe. /health returns the bare string 'ok' (serve.rs:148-150),
    not JSON — so check the HTTP status only, mirroring smoke.sh step 2."""
    req = urllib.request.Request(f"{DRUDGE_URL}/health", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status == 200


def parse_frontmatter(text):
    """Tiny stdlib YAML-subset parser for the fixture frontmatter we control.

    Handles exactly the shapes our fixtures use: `key: value`, quoted scalars,
    and `- item` block lists. Not a general YAML parser — it only needs to read
    files in this repo's data/eval/fixtures/.
    """
    if not text.startswith("---"):
        raise ValueError("fixture missing frontmatter fence")
    end = text.find("\n---", 3)
    if end == -1:
        raise ValueError("fixture frontmatter not closed")
    block = text[3:end].strip("\n")
    body = text[end + 4 :].lstrip("\n")

    front = {}
    cur_key = None
    for raw in block.splitlines():
        if not raw.strip():
            continue
        if raw.lstrip().startswith("- ") and cur_key is not None:
            front[cur_key].append(_scalar(raw.lstrip()[2:]))
            continue
        if ":" not in raw:
            continue
        key, _, val = raw.partition(":")
        key = key.strip()
        val = val.strip()
        if val == "":
            front[key] = []  # start of a block list
            cur_key = key
        else:
            front[key] = _scalar(val)
            cur_key = None
    return front, body


def _scalar(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def load_fixtures():
    """Return {local_id: {title, body, tags, tools, concepts, origin}}."""
    fixtures = {}
    for path in sorted(FIXTURES_DIR.glob("*.md")):
        front, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        local_id = front.get("id")
        if not local_id:
            raise ValueError(f"{path.name}: missing frontmatter id")
        fixtures[local_id] = {
            "title": front.get("title", local_id),
            "body": body.strip(),
            "tags": _as_list(front.get("tags")),
            "tools": _as_list(front.get("tools")),
            "concepts": _as_list(front.get("concepts")),
            "origin": front.get("origin", "personal"),
        }
    if not fixtures:
        raise SystemExit("no fixtures found under data/eval/fixtures/")
    return fixtures


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def wiki_id_of(hit):
    """Derive the wiki-NNNN id from a /search hit (mode-agnostic).

    Prefer the structured id (wiki stem in wiki mode); fall back to scanning the
    source_path (vector mode, where id is '{source_path}#{idx}').
    """
    for field in ("id", "source_path"):
        m = WIKI_ID_RE.search(str(hit.get(field, "")))
        if m:
            return m.group(0)
    return None


def ingest_fixtures(fixtures):
    """Ingest each fixture via MCP `remember`; return {local_id: wiki_id}."""
    mapping = {}
    for i, (local_id, f) in enumerate(fixtures.items(), start=1):
        # tag the note so the eval origin is obvious in any vault it lands in.
        tags = (f["tags"] + ["eval"])[:6]
        body = f"{f['body']}\n\n_(eval fixture: {local_id})_"
        rpc = {
            "jsonrpc": "2.0",
            "id": i,
            "method": "tools/call",
            "params": {
                "name": "remember",
                "arguments": {
                    "title": f["title"],
                    "body": body,
                    "tags": tags,
                    "tools": f["tools"],
                    "concepts": f["concepts"],
                    "origin": f["origin"],
                },
            },
        }
        resp = http_json("/mcp", rpc)
        if "error" in resp:
            raise SystemExit(f"remember failed for {local_id}: {resp['error']}")
        text = ""
        for block in resp.get("result", {}).get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                break
        m = WIKI_ID_RE.search(text)
        if not m:
            raise SystemExit(
                f"could not parse allocated wiki id from remember reply "
                f"for {local_id}: {text!r}"
            )
        mapping[local_id] = m.group(0)
        print(f"  ingested {local_id} -> {mapping[local_id]}")
    return mapping


def search(query, max_results=5):
    """Run a query through the product recall path (POST /search)."""
    resp = http_json("/search", {"query": query, "max_results": max_results})
    return resp.get("hits", [])


def ask(question):
    """Exercise the synthesis path (POST /ask); return the answer string."""
    try:
        resp = http_json("/ask", {"question": question})
        return (resp.get("answer") or "").strip()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return ""


def evaluate(golden, mapping):
    """Score each query; return rows + aggregate recall@1 and MRR."""
    rows = []
    sum_r1 = 0.0
    sum_mrr = 0.0
    for q in golden["queries"]:
        query = q["query"]
        expected = {mapping[e] for e in q["expect"] if e in mapping}
        missing = [e for e in q["expect"] if e not in mapping]
        if missing:
            raise SystemExit(
                f"golden references fixtures not ingested: {missing} "
                f"(query: {query!r})"
            )

        hits = search(query)
        ranked = [wiki_id_of(h) for h in hits]

        r1 = 1.0 if ranked[:1] and ranked[0] in expected else 0.0
        rr = 0.0
        for rank, wid in enumerate(ranked, start=1):
            if wid in expected:
                rr = 1.0 / rank
                break

        answer = ask(query)
        sum_r1 += r1
        sum_mrr += rr
        rows.append(
            {
                "query": query,
                "expect": sorted(expected),
                "top": ranked[0] if ranked else "-",
                "r1": r1,
                "rr": rr,
                "answer_ok": bool(answer),
            }
        )
    n = len(golden["queries"])
    return rows, (sum_r1 / n if n else 0.0), (sum_mrr / n if n else 0.0)


def print_table(rows, recall1, mrr):
    print()
    print(f"{'recall@1':>8}  {'MRR':>5}  {'top hit':<14}  {'ans':<3}  query")
    print("-" * 78)
    for r in rows:
        print(
            f"{r['r1']:>8.0f}  {r['rr']:>5.2f}  {r['top']:<14}  "
            f"{'yes' if r['answer_ok'] else 'NO ':<3}  {r['query'][:40]}"
        )
    print("-" * 78)
    print(f"TOTAL  recall@1={recall1:.3f}  MRR={mrr:.3f}  (floor recall@1>={RECALL_AT_1_FLOOR})")


def main():
    ap = argparse.ArgumentParser(description="ohmyboring retrieval eval gate")
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if recall@1 is below the floor (gate mode)",
    )
    args = ap.parse_args()

    print(f"eval: drudge={DRUDGE_URL}")
    try:
        health_ok()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"drudge not reachable at {DRUDGE_URL}/health: {e}", file=sys.stderr)
        return 2

    fixtures = load_fixtures()
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    print(f"eval: {len(fixtures)} fixtures, {len(golden['queries'])} golden queries")

    print("ingesting fixtures via MCP remember…")
    mapping = ingest_fixtures(fixtures)

    rows, recall1, mrr = evaluate(golden, mapping)
    print_table(rows, recall1, mrr)

    if args.check and recall1 < RECALL_AT_1_FLOOR:
        print(
            f"\nFAIL: recall@1 {recall1:.3f} < floor {RECALL_AT_1_FLOOR}",
            file=sys.stderr,
        )
        return 1
    print("\nOK: eval passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
