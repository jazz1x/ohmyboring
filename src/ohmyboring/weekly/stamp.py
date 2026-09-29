"""주간 브리핑의 이름표 — 제목·주 번호·날짜. 시각은 부르는 쪽이 넘긴다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
TITLE = "📅 주간 브리핑"
WINDOW_DAYS = 7


def week_label(now: datetime) -> str:
    return now.strftime("%G-W%V")


def stamp(now: datetime) -> str:
    return f"{week_label(now)} · {now.strftime('%Y-%m-%d %a')}"


def header(now: datetime, body: str) -> str:
    return f"*{TITLE}*\n`{stamp(now)}`\n\n{body}"
