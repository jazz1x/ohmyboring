"""LLM 이 건너뛰기로 한 세션 — 사건만 남기고 끝낸다."""

from __future__ import annotations

import sys

from ohmyboring.distill import resolution_event


def skip(state):
    print("[distill-session] LLM decided SKIP", file=sys.stderr)
    resolution_event.log_skip_event(
        state["session_id"], state["origin"], state["repo"], state["resolution"], "llm_skip"
    )
    return {"ok": True}  # intentional skip → mark as done so we don't retry forever
