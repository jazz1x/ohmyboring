#!/usr/bin/env python3
"""UserPromptSubmit hook — recalls *my past work experiences* relevant to the current
prompt from drudge (vector+graph) and injects them as context. The loop where
self-augmentation automatically enriches actual work.

Design:
- push (auto-injection) — better suited to ambient recall than pull, where the model decides whether to call an MCP tool.
- uses /search (vector, ~100ms) — not /ask (gemma4, slow). Recall only, no synthesis.
- if drudge (:7700) is down/errors, it's a *silent no-op* (never blocks the prompt).
"""
import json
import os
import sys
import urllib.request

URL = "http://localhost:7700/search"
# Hard ceiling for automatic prompt injection. Keeps Claude Code's context window from
# being flooded by recalled memory on every prompt.
MAX_RESULTS = int(os.environ.get("RECALL_MAX_RESULTS") or "3")
MAX_TOKENS = int(os.environ.get("RECALL_MAX_TOKENS") or "1500")
TIMEOUT = float(os.environ.get("RECALL_TIMEOUT") or "5")
RETRIES = int(os.environ.get("RECALL_RETRIES") or "1")


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    prompt = (data.get("prompt") or "").strip()
    if len(prompt) < 8:  # too short → recall is meaningless
        return
    def _search():
        body = json.dumps({
            "query": prompt,
            "max_results": MAX_RESULTS,
            "max_tokens": MAX_TOKENS,
        }).encode()
        req = urllib.request.Request(URL, data=body, headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read()).get("hits", [])

    hits = []
    for attempt in range(RETRIES + 1):
        try:
            hits = _search()
            break
        except Exception:
            if attempt == RETRIES:
                return  # engine down → no-op (graceful)
    if not hits:
        return
    lines = []
    for h in hits[:MAX_RESULTS]:
        src = (h.get("source_path") or "").rsplit("/", 1)[-1]
        snip = " ".join((h.get("snippet") or "").split())[:280]
        if snip:
            lines.append(f"- [{src}] {snip}")
    if not lines:
        return
    ctx = "📚 My past work experience (self-augmenting RAG recall — refer only when relevant):\n" + "\n".join(lines)
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}
    }))


if __name__ == "__main__":
    main()
