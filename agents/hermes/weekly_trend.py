"""What a week of daily briefings can honestly say, and what it cannot.

The obvious weekly is "what closed this week" — take seven daily briefs, diff the items, report
the ones that moved from Next to Done. Measured against the real artifacts, that does not work:
over 2026-08-20..26 there were 252 distinct items and the adjacent-day intersection was
0, 0, 0, 2, 0, 0. Each day's briefing is re-synthesised by the local model from the corpus, so the
same fact comes back in different words and no text key survives the night.

What does survive is the project name and the label. `oh-my-codereview` appeared 7/7 days,
`kb-rag-bot` 5/7. So this module reports **persistence and trend**, never closure:

- how many days of the window a project appeared, and which states it kept
- the label counts on each day, as observations rather than a rate

The distinction matters more than it sounds. "✅ 10 → 10" is two daily observations; it is not
twenty completed items, and the moment a reader adds them up the report has lied. Anything that
needs item identity — closed this week, newly opened, a Next that became a Done — waits for stable
item ids, which need a distillation change and therefore cannot happen before the measurement
window closes (docs/PRD.md §5-R3).

No I/O: the caller supplies parsed daily briefs.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

#: States worth surfacing when they persist. "Done every day" is not an intervention signal;
#: "blocked every day" is the whole reason to read a weekly.
PERSISTENT_LABELS = ("Blocked", "Stalled")

#: A project has to hold a state on at least this many days before it is called persistent. Two
#: is noise on a seven-day window — a thing can be blocked overnight and cleared by lunch.
MIN_PERSIST_DAYS = 3

#: Scoreboard width. Slack allows 10 fields per section; the measured window had 4–7 active
#: projects, so this caps the tail rather than the normal case.
MAX_SCOREBOARD = 8


@dataclass
class ProjectWeek:
    """One project's week. Days, not items — items do not survive re-synthesis."""

    name: str
    days: set[str] = field(default_factory=set)
    label_days: dict[str, set[str]] = field(default_factory=dict)
    latest_line: str = ""
    latest_label: str = ""

    def persistent_labels(self, min_days: int = MIN_PERSIST_DAYS) -> list[tuple[str, int]]:
        """(label, days) for states this project held on enough days, worst first."""
        held = [
            (label, len(self.label_days.get(label, ())))
            for label in PERSISTENT_LABELS
            if len(self.label_days.get(label, ())) >= min_days
        ]
        held.sort(key=lambda pair: (-pair[1], PERSISTENT_LABELS.index(pair[0])))
        return held


def collect_week(days: list[tuple[str, object]]) -> dict[str, ProjectWeek]:
    """Fold parsed daily briefs into per-project weeks.

    `days` is [(iso_date, BriefDocument)] in chronological order; the last one supplies the
    representative line, quoted rather than re-summarised — a weekly that rewrites the daily is
    the same information a seventh time, blurrier.
    """
    projects: dict[str, ProjectWeek] = {}
    for date, doc in days:
        for project in getattr(doc, "projects", []):
            name = (project.name or "").strip()
            if not name:
                continue
            week = projects.setdefault(name, ProjectWeek(name=name))
            week.days.add(date)
            for item in project.items:
                label = item.label or ""
                if label:
                    week.label_days.setdefault(label, set()).add(date)
    if days:
        last_date, last_doc = days[-1]
        for project in getattr(last_doc, "projects", []):
            week = projects.get((project.name or "").strip())
            if not week:
                continue
            for label in PERSISTENT_LABELS:
                pick = next((i for i in project.items if (i.label or "") == label), None)
                if pick:
                    week.latest_line, week.latest_label = pick.text, label
                    break
    return projects


def needs_intervention(
    projects: dict[str, ProjectWeek], min_days: int = MIN_PERSIST_DAYS
) -> list[tuple[ProjectWeek, str, int]]:
    """Projects that held a blocked/stalled state across the window, worst first.

    This is the one thing the weekly knows that seven dailies do not: the reader would have to
    dedup a week of messages in their head to see it.
    """
    out: list[tuple[ProjectWeek, str, int]] = []
    for week in projects.values():
        held = week.persistent_labels(min_days)
        if held:
            label, count = held[0]
            out.append((week, label, count))
    out.sort(key=lambda row: (-row[2], PERSISTENT_LABELS.index(row[1]), row[0].name))
    return out


def scoreboard(projects: dict[str, ProjectWeek], limit: int = MAX_SCOREBOARD) -> list[ProjectWeek]:
    """Projects by how much of the week they occupied — where the attention went."""
    ranked = sorted(projects.values(), key=lambda w: (-len(w.days), w.name))
    return ranked[:limit]


def label_trend(days: list[tuple[str, object]]) -> dict[str, tuple[int, int]]:
    """{label: (first_day_count, last_day_count)} — two observations, not a rate.

    Deliberately only the endpoints. A sum across the week would double-count: each day is an
    independent re-synthesis, so the same finished work can be observed on several days and there
    is no key to tell a re-observation from a new one.
    """
    if not days:
        return {}

    def counts(doc: object) -> Counter:
        return Counter(
            item.label for project in getattr(doc, "projects", []) for item in project.items if item.label
        )

    first, last = counts(days[0][1]), counts(days[-1][1])
    return {label: (first.get(label, 0), last.get(label, 0)) for label in first | last}
