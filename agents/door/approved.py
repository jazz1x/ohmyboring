#!/usr/bin/env python3
"""Approved notes — the pure read side of the morning card.

A Slack card session is named ``slack:<channel>:<message_ts>`` (agents/slack/card.py)
and the engine records what that session judged as graph edges
``session:slack:… → doc:…`` with kind ``used`` (approved with 「해」) or ``contested``.
The edge table has no time column (measured 2026-09-22), so the timestamp is read
from the session name itself: the float after the last colon is a Slack epoch.

Everything here is pure: rows in, dataclasses out. The door route owns the SQL
and the wire format.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")

APPROVED_KIND = "used"
CONTESTED_KIND = "contested"

SINCE_HOURS_MIN = 1
# 24 × 365: the window a card ever asks about. Above this, timedelta(hours=…) overflows
# on absurd input and the door died 500 (cycle-7 defect); outside [1, 8760] is a 400.
SINCE_HOURS_MAX = 24 * 365

_SINCE_HOURS_ERROR = f"since_hours must be an integer in [{SINCE_HOURS_MIN}, {SINCE_HOURS_MAX}]"


def check_since_hours(raw: str) -> int:
    """Query string in, bounded int out — ValueError with the reason otherwise.

    `since_hours=0` is refused too: an empty window answers an empty list that
    reads like "nothing was approved", which is not a fact the store holds.
    """
    try:
        hours = int(raw)
    except ValueError:
        raise ValueError(_SINCE_HOURS_ERROR) from None
    if not SINCE_HOURS_MIN <= hours <= SINCE_HOURS_MAX:
        raise ValueError(_SINCE_HOURS_ERROR)
    return hours


@dataclass(frozen=True)
class Approved:
    session: str
    note: str
    at: datetime


@dataclass(frozen=True)
class Contested:
    session: str
    note: str
    at: datetime


@dataclass(frozen=True)
class Skipped:
    session: str
    reason: str


@dataclass(frozen=True)
class Selection:
    approved: list[Approved]
    contested: list[Contested]
    skipped: list[Skipped]


def parse_session_ts(name: str) -> datetime | None:
    """``session:slack:C1:1758470000.5`` (or the bare card name) → aware datetime.

    The token after the last colon must be a Slack epoch in seconds; anything
    else (a channel-only name, a uuid session) is None, never an exception.
    """
    tail = name.rsplit(":", 1)[-1]
    try:
        ts = float(tail)
    except ValueError:
        return None
    return datetime.fromtimestamp(ts, tz=SEOUL)


def select_approved(rows, since_hours: float, now: datetime) -> Selection:
    """Partition edge rows into approved / contested inside the ``since_hours`` window.

    Rows are ``(src, dst, kind)`` as they come from the edge table. Sessions that
    are not slack card sessions, or whose name carries no parseable ts, become
    ``Skipped`` values — the read reports what it dropped instead of failing.
    """
    cutoff = now - timedelta(hours=since_hours)
    approved: list[Approved] = []
    contested: list[Contested] = []
    skipped: list[Skipped] = []
    for src, dst, kind in rows:
        name = src.removeprefix("session:")
        if not name.startswith("slack:"):
            skipped.append(Skipped(src, "not a slack card session"))
            continue
        at = parse_session_ts(name)
        if at is None:
            skipped.append(Skipped(src, "no parseable ts in session name"))
            continue
        if not cutoff <= at <= now:
            continue
        if kind == APPROVED_KIND:
            approved.append(Approved(session=name, note=dst, at=at))
        elif kind == CONTESTED_KIND:
            contested.append(Contested(session=name, note=dst, at=at))
    approved.sort(key=lambda item: item.at, reverse=True)
    contested.sort(key=lambda item: item.at, reverse=True)
    return Selection(approved=approved, contested=contested, skipped=skipped)
