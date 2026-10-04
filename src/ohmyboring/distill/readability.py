"""Is a note readable? The owner's 「가독성」 complaint (2026-09-30) made countable, in one place —
data-steward reports it over the vault, the polish step checks a rewrite against it."""

from __future__ import annotations

import re

WALL_LINE = 300
HEADING_MIN_BODY = 400
TITLE_MAX = 60
SIGNALS = ("wall-line", "no-heading", "long-title", "sha-in-title")
_SHA = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")


def prose_lines(body: str) -> list[str]:
    """Non-empty body lines outside ``` fences — a code block's long lines are code, not prose."""
    lines, fenced = [], False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced and line.strip():
            lines.append(line)
    return lines


def body_signals(body: str) -> list[str]:
    prose = prose_lines(body)
    tripped = {
        "wall-line": any(len(line) > WALL_LINE for line in prose),
        "no-heading": len(body) > HEADING_MIN_BODY and not any(line.startswith("#") for line in prose),
    }
    return [name for name in ("wall-line", "no-heading") if tripped[name]]


def title_signals(title: str) -> list[str]:
    tripped = {"long-title": len(title) > TITLE_MAX, "sha-in-title": bool(_SHA.search(title))}
    return [name for name in ("long-title", "sha-in-title") if tripped[name]]


def signals(title: str, body: str) -> list[str]:
    """Which of SIGNALS a note trips, in SIGNALS order."""
    tripped = set(body_signals(body)) | set(title_signals(title))
    return [name for name in SIGNALS if name in tripped]
