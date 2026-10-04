"""일간 산출물 읽기 — 주간은 엔진에 다시 묻지 않고 이미 나온 일간을 읽는다."""

from __future__ import annotations

import os
from datetime import timedelta

from ohmyboring.briefing.parse import parse_brief
from ohmyboring.weekly.stamp import WINDOW_DAYS
from ohmyboring.weekly.trend import collect_week


def read_days(today, split_frontmatter, wiki_dir, span=WINDOW_DAYS):
    """Parsed daily briefs for the window, oldest first. Missing days are simply absent.

    A missing day is not an error — the machine may have been off — but the count of days found
    is reported, because "5/7일" means something different when only five briefs exist.
    """
    root = os.path.join(wiki_dir, "wiki")
    out = []
    for back in range(span - 1, -1, -1):
        date = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        path = os.path.join(root, f"daily-brief-{date}.md")
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read()
        except OSError:
            continue
        split = split_frontmatter(raw)
        out.append((date, parse_brief(split[1] if split else raw)))
    return out


def read_week(state):
    days = read_days(state["now"], state["split_frontmatter"], state["vault_dir"])
    projects = collect_week(days) if len(days) >= 2 else {}
    return {"days": days, "projects": projects}
