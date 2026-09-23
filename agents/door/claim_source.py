#!/usr/bin/env python3
"""Claim source — resolve a register's claim subject to the note path a card cites.

The morning card's register sources are claim subjects (e.g. 「3차 창 샘플링」), not
note paths, but the card's verdict edge attaches to doc:<note path>. Before a
proposal can carry a button, its subject must resolve to the note path of that
subject's current claim — superseded_at is null, latest valid_from. One subject
can own several current claims (measured 2026-09-22: 1,470), so the newest wins
and the rest ride along as candidates for the next look.

Everything here is pure: rows in, dict out. The door route owns the SQL and the
wire format.
"""

from __future__ import annotations

from typing import Any

SQL = (
    "select source_path, valid_from, value from claim "
    "where subject = %s and superseded_at is null "
    "order by valid_from desc"
)


def pick_current(subject: str, rows: list[tuple[str, Any, Any]]) -> dict[str, Any] | None:
    """rows: (source_path, valid_from, value) as fetched — any order. Newest claim wins;
    all current claims are the candidates, and the winner's value rides along for
    callers (the LangGraph store's get) that need the claim payload, not just the path.
    No rows → None, the door's 404."""
    if not rows:
        return None
    ordered = sorted(rows, key=lambda row: row[1], reverse=True)
    note, valid_from, value = ordered[0]
    return {
        "subject": subject,
        "note": note,
        "valid_from": valid_from.isoformat(timespec="seconds"),
        "value": value,
        "candidates": [
            {"note": path, "valid_from": at.isoformat(timespec="seconds")} for path, at, _ in ordered
        ],
    }
