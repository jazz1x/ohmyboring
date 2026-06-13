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
import sys
import urllib.request

URL = "http://localhost:7700/search"


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    prompt = (data.get("prompt") or "").strip()
    if len(prompt) < 8:  # too short → recall is meaningless
        return
    try:
        body = json.dumps({"query": prompt}).encode()
        req = urllib.request.Request(URL, data=body, headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            hits = json.loads(r.read()).get("hits", [])
    except Exception:
        return  # engine down → no-op (graceful)
    if not hits:
        return
    lines = []
    for h in hits[:3]:
        src = (h.get("source_path") or "").rsplit("/", 1)[-1]
        snip = " ".join((h.get("snippet") or "").split())[:280]
        if snip:
            lines.append(f"- [{src}] {snip}")
    if not lines:
        return
    ctx = "📚 내 과거 작업 경험 (자가증강 RAG 회수 — 관련될 때만 참고):\n" + "\n".join(lines)
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}
    }))


if __name__ == "__main__":
    main()
