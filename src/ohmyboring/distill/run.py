"""증류 입구 — hermes ingest-worker 가 큐에서 꺼낸 세션 하나를 그래프에 태운다."""

from __future__ import annotations

from ohmyboring.distill.graph import graph


def distill_and_remember(text, origin, repo, session_id=""):
    """Distill the transcript text via local LLM and write it through ohmyboring's remember tool."""
    final = graph.invoke(
        {
            "text": text,
            "origin": origin,
            "repo": repo,
            "session_id": session_id,
            "verifier_status": "pass",
            "ok": False,
        }
    )
    return final["ok"]
