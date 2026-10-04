"""remember 직전 퇴고 — 모델이 다시 쓴 본문을 judge 가 판정하고, 사실 누락·모양 미달이면 한 번만 재시도."""

from __future__ import annotations

import sys

from ohmyboring.adapters import llm
from ohmyboring.distill import polish as polish_engine
from ohmyboring.distill import settings


def polish(state):
    """첫 퇴고 시도. 결과를 polish_outcome 에 ADT 값 그대로 남긴다(Polished|Kept, 문자열 플래그 아님)."""
    return _run(state)


def polish_retry(state):
    """첫 Kept 가 LostFacts|MissedShape 일 때만 타는 간선 — 사유를 프롬프트에 그대로 붙여 한 번 더."""
    first = state["polish_outcome"]
    return _run(state, retry_reason=first.reason)


def _run(state, retry_reason=None):
    note = state["note"]
    result = polish_engine.polish(note["body"], settings.NOTE_LANG, llm.call_llm, retry_reason=retry_reason)
    label = "polish retry" if retry_reason else "polish"
    if isinstance(result, polish_engine.Polished):
        print(f"[distill-session] {label}: polished", file=sys.stderr)
        note = {**note, "body": result.body}
    else:
        print(f"[distill-session] {label}: kept — {result.reason}", file=sys.stderr)
    return {"note": note, "polish_outcome": result}
