"""기록 — 다음 실행(같은 주의 다른 스케줄러)이 읽는 장부. 카드는 이미 나갔으니 실패해도 성공이다."""

from __future__ import annotations

from ohmyboring.adapters import events as event_log
from ohmyboring.weekly.nodes.posted import WEEKLY_EVENT
from ohmyboring.weekly.report import one_line
from ohmyboring.weekly.stamp import week_label
from ohmyboring.weekly.state import Posted


def record(state):
    try:
        event_log.append_event(
            "slack-card", WEEKLY_EVENT, "ok", week=week_label(state["now"]), ts=state["ts"]
        )
    except OSError as e:
        return {
            "outcome": Posted(state["ts"], f"[weekly] {WEEKLY_EVENT} event not recorded: {one_line(str(e))}")
        }
    return {"outcome": Posted(state["ts"], None)}
