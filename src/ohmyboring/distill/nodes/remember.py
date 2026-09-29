"""검증을 통과한 노트를 엔진의 remember 로 쓰고 결과를 사건으로 적는다."""

from __future__ import annotations

from ohmyboring.adapters import engine
from ohmyboring.distill import resolution_event


def remember(state):
    note = state["note"]
    outcome = engine.call_remember(
        note["title"],
        note["body"],
        state["origin"],
        state["repo"],
        note["tags"],
        note["tools"],
        note["concepts"],
        note["claims"],
        state["session_id"],
    )
    resolution_event.log_resolution_event(
        state["session_id"],
        state["origin"],
        state["repo"],
        state["report"],
        state["verifier_status"],
        outcome.status,
    )
    return {"ok": outcome.ok}
