#!/usr/bin/env python3
"""Claim source — resolve a register's claim subject to the note path a card cites.

The morning card's register sources are claim subjects (e.g. 「3차 창 샘플링」), not
note paths, but the card's verdict edge attaches to doc:<note path>. Before a
proposal can carry a button, its subject must resolve to the note path of that
subject's current claim — superseded_at is null, latest valid_from. One subject
can own several current claims (measured 2026-09-22: 1,470), so one wins and the
rest ride along as candidates for the next look. An owner-written claim wins over
a newer claim from anyone else: the engine keeps the owner's row current beside it.

Everything here is pure: rows in, dict out. The door route owns the SQL and the
wire format.
"""

from __future__ import annotations

from typing import Any

SQL = (
    "select c.source_path, c.valid_from, c.value, coalesce(d.author, 'unknown') from claim c "
    "left join document d on d.source_path = c.source_path "
    "where c.subject = %s and c.superseded_at is null "
    "order by c.valid_from desc"
)

LIST_SQL = (
    "select c.subject, c.source_path, c.valid_from, c.value, coalesce(d.author, 'unknown') from claim c "
    "left join document d on d.source_path = c.source_path "
    "where c.predicate = %s and c.superseded_at is null "
    "order by c.valid_from desc"
)


def pick_current(subject: str, rows: list[tuple[str, Any, Any, str]]) -> dict[str, Any] | None:
    """rows: (source_path, valid_from, value, author) as fetched — any order. Owner-written
    claims first, newest first within each; all current claims are the candidates, and the
    winner's value rides along for callers (the LangGraph store's get) that need the claim
    payload, not just the path. No rows → None, the door's 404."""
    if not rows:
        return None
    ordered = sorted(rows, key=lambda row: (row[3] == "owner", row[1]), reverse=True)
    note, valid_from, value, _ = ordered[0]
    return {
        "subject": subject,
        "note": note,
        "valid_from": valid_from.isoformat(timespec="seconds"),
        "value": value,
        "candidates": [
            {"note": path, "valid_from": at.isoformat(timespec="seconds")} for path, at, _, _ in ordered
        ],
    }


def group_current(rows: list[tuple[str, str, Any, Any, str]]) -> list[dict[str, Any]]:
    """rows: (subject, source_path, valid_from, value, author) for one predicate — any order.
    One pick_current answer per subject, subjects in sorted order."""
    by_subject: dict[str, list[tuple[str, Any, Any, str]]] = {}
    for subject, *rest in rows:
        by_subject.setdefault(subject, []).append(tuple(rest))
    return [pick_current(subject, by_subject[subject]) for subject in sorted(by_subject)]
