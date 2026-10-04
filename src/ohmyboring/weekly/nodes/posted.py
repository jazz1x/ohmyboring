"""이번 주에 이미 올렸나 — 이 카드가 남기는 weekly_card 사건이 장부다."""

from __future__ import annotations

from ohmyboring.adapters import events as event_log
from ohmyboring.weekly.stamp import week_label
from ohmyboring.weekly.state import AlreadyPosted

#: The poster's own ledger — one per posted weekly, the guard's signal (no new state file).
WEEKLY_EVENT = "weekly_card"


def posted_this_week_ts(week: str) -> str | None:
    """The ts of the weekly already posted this ISO week, from the weekly_card events this
    poster records — or None when the week is still unposted. An unreadable event log is
    not a reason to skip a Monday: it just leaves the guard blind and the normal run will
    surface a real outage on its own."""
    newest: str | None = None
    for event in event_log.recent_events(20, event_name=WEEKLY_EVENT):
        if str(event.get("week") or "") == week and event.get("ts"):
            newest = str(event["ts"])  # oldest-first order: the last match is the newest
    return newest


def check_posted(state):
    ts = posted_this_week_ts(week_label(state["now"]))
    return {} if ts is None else {"outcome": AlreadyPosted(ts)}
