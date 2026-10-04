"""추세 렌더 — 일간이 충분하면 지속·추세를 그린다."""

from __future__ import annotations

import json

from ohmyboring.briefing.weekly_render import render_weekly_blocks, render_weekly_mrkdwn
from ohmyboring.weekly.stamp import TITLE, WINDOW_DAYS, stamp
from ohmyboring.weekly.trend import label_trend, needs_intervention, scoreboard


def render_trend(state):
    days, projects = state["days"], state["projects"]
    span = len(days)
    intervention = [(w, label, count, span) for w, label, count in needs_intervention(projects)]
    board = [(w, span) for w in scoreboard(projects)]
    trend = label_trend(days)
    heading = f"{stamp(state['now'])} · 스냅샷 {span}/{WINDOW_DAYS}일"
    blocks = render_weekly_blocks(TITLE, heading, projects, intervention, board, trend, [])
    text = render_weekly_mrkdwn(TITLE, heading, intervention, board, trend, [])
    if state["fmt"] != "blocks":
        return {"stdout": text}
    payload = {"text": text, "blocks": blocks, "unfurl_links": False, "unfurl_media": False}
    return {"stdout": json.dumps(payload, ensure_ascii=False, sort_keys=True)}
