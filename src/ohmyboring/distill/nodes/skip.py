"""LLM 이 건너뛰기로 한 세션 — 사건만 남기고 끝낸다."""

from __future__ import annotations

import sys

from ohmyboring.distill import resolution_event, settings


def skip(state):
    print("[distill-session] LLM decided SKIP", file=sys.stderr)
    resolution_event.log_skip_event(
        state["session_id"], state["origin"], state["repo"], state["resolution"], "llm_skip"
    )
    return {"ok": True}  # intentional skip → mark as done so we don't retry forever


def no_evidence(state):
    """근거 낱말 0 전사 — 게이트를 못 넘으니 모델을 부르지 않고 끝낸다."""
    resolution = settings.distill_resolution()
    print("[distill-session] no evidence tokens in transcript; SKIP before model", file=sys.stderr)
    resolution_event.log_skip_event(
        state["session_id"], state["origin"], state["repo"], resolution, "no_evidence_tokens"
    )
    return {"resolution": resolution, "ok": True}
