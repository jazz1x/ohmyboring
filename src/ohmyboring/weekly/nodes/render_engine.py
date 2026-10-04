"""엔진 답 렌더 — blocks 면 Block Kit JSON, 아니면 mrkdwn 본문."""

from __future__ import annotations

import json

from ohmyboring.briefing.blocks import render_blocks_payload
from ohmyboring.briefing.mrkdwn import render_message_mrkdwn
from ohmyboring.briefing.weekly_render import EMPTY_MESSAGE
from ohmyboring.weekly.stamp import TITLE, stamp


def render_engine(state):
    heading = stamp(state["now"])
    answer, sources = state["answer"], state["sources"]
    if state["fmt"] == "blocks":
        payload = render_blocks_payload(TITLE, heading, answer, sources, EMPTY_MESSAGE)
        return {"stdout": json.dumps(payload, ensure_ascii=False, sort_keys=True)}
    return {"stdout": render_message_mrkdwn(f"*{TITLE}*", heading, answer, sources, EMPTY_MESSAGE)}
