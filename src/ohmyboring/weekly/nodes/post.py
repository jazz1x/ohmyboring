"""슬랙 게시 — 거절은 값이다. 왜인지 한 줄로 남기고 스택은 남기지 않는다."""

from __future__ import annotations

from ohmyboring.adapters import slack
from ohmyboring.weekly.report import one_line
from ohmyboring.weekly.state import PostFailed


def post(state):
    try:
        return {"ts": slack.post_payload(state["payload"])}
    except Exception as e:  # noqa: BLE001 — say why in one line, not with a stack trace
        return {"outcome": PostFailed(one_line(str(e)))}
