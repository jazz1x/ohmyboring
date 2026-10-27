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


class TsOutOfRange(ValueError):
    """The tail is a float (inf, nan, 1e400, 1e20 …) that cannot become a datetime."""


def parse_session_ts(name: str) -> datetime | None:
    """``session:slack:C1:1758470000.5`` (or the bare card name) → aware datetime.

    The token after the last colon must be a Slack epoch in seconds; anything
    else (a channel-only name, a uuid session) is None. A float no datetime can hold
    raises TsOutOfRange, which select_approved turns into a Skipped.
    """
    tail = name.rsplit(":", 1)[-1]
    try:
        ts = float(tail)
    except ValueError:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=SEOUL)
    except (OverflowError, ValueError, OSError):
        raise TsOutOfRange(name) from None


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
        try:
            at = parse_session_ts(name)
        except TsOutOfRange:
            skipped.append(Skipped(src, "session ts out of range"))
            continue
        if at is None:
            skipped.append(Skipped(src, "no parseable ts in session name"))
            continue
        if not cutoff <= at <= now:
            continue
        if kind == APPROVED_KIND:
            approved.append(Approved(session=name, note=dst, at=at))
        elif kind == CONTESTED_KIND:
            contested.append(Contested(session=name, note=dst, at=at))
    # at descending, then session ascending, then note ascending (code point). Two stable
    # passes: the DB hands rows back in no fixed order, and a tie must not depend on it.
    for items in (approved, contested):
        items.sort(key=lambda item: (item.session, item.note))
        items.sort(key=lambda item: item.at, reverse=True)
    return Selection(approved=approved, contested=contested, skipped=skipped)


def payload(selection: Selection, since_hours: int) -> dict:
    """The /approved JSON body, key order = wire order. Only the count of skipped edge rows
    is on the wire (duplicates counted, one per row); which rows went to the server log."""
    return {
        "since_hours": since_hours,
        "approved": [
            {"session": item.session, "note": item.note, "at": item.at.isoformat(timespec="seconds")}
            for item in selection.approved
        ],
        "contested": [
            {"session": item.session, "note": item.note, "at": item.at.isoformat(timespec="seconds")}
            for item in selection.contested
        ],
        "skipped_rows": len(selection.skipped),
    }
