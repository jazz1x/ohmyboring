#!/usr/bin/env python3
"""UserPromptSubmit 훅 — 현재 프롬프트와 관련된 *내 과거 작업 경험*을 drudge 에서
회수(벡터+그래프)해 컨텍스트로 주입한다. 자가증강이 실제 작업을 자동 보강하는 고리.

설계:
- push(자동 주입) — 모델이 MCP 툴을 부를지 결정하는 pull 보다 ambient recall 에 적합.
- /search(벡터, ~100ms) 사용 — /ask(gemma4, 느림) 아님. 회수만, 합성 X.
- drudge(:7700) 미가동/에러면 *조용히 no-op* (프롬프트를 절대 막지 않음).
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
    if len(prompt) < 8:  # 너무 짧으면 회수 무의미
        return
    try:
        body = json.dumps({"query": prompt}).encode()
        req = urllib.request.Request(URL, data=body, headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            hits = json.loads(r.read()).get("hits", [])
    except Exception:
        return  # 엔진 다운 → no-op (graceful)
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
