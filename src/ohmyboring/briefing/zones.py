"""브리핑 존·고른 항목·그룹 — 두 렌더가 같은 항목을 같은 개수로 보이게 하는 자리."""

from __future__ import annotations

from typing import Any

from ohmyboring.briefing.parse import UNATTRIBUTED
from ohmyboring.briefing.text import _slack_inline

PROJECT_LIMIT = 6
#: How many items of a group each renderer shows. One number, not two: the text fallback and the
#: Block Kit payload go into the same message, and Slack picks between them — so a reader whose
#: client falls back was being shown a different set of items (5 vs 4) with a different "+N".
#: A fallback that disagrees with the blocks is a second, quieter briefing.
ITEM_LIMIT = 5
DONE_ITEM_LIMIT = 3  # Done is historical context; keep it short.

#: Blocked is never truncated. It is the reason this briefing exists — a "+N more" hiding a
#: blocker is the one omission that can cost the reader their morning.
NEVER_TRUNCATED = frozenset({"Blocked"})
BLOCK_PROJECT_LIMIT = 5

# Briefing is read, not searched. Group by status priority so the reader
# sees "what blocks me" first, "what to do next" second, and "what finished"
# last. Keep sections short; mobile Slack rewards vertical scannability.
SECTION_ORDER = ["Blocked", "Next", "Stalled", "Risks", "Decisions", "Facts", "Done", ""]
SECTION_EMOJI = {
    "Blocked": "🚨",
    "Next": "▶️",
    "Stalled": "⏸️",
    "Risks": "⚠️",
    "Decisions": "💡",
    "Facts": "📌",
    "Done": "✅",
    "": "•",
}
SECTION_TITLE = {
    "Blocked": "막힘",
    "Next": "다음 행동",
    "Stalled": "정체 중",
    "Risks": "리스크",
    "Decisions": "결정",
    "Facts": "사실",
    "Done": "완료",
    "": "기타",
}


#: Groups that answer "what do I do now", in the order a reader should meet them. Everything else
#: is confirmation, and confirmation belongs below the fold.
#:
#: Stalled sits after Next, not before it. A stalled item is by definition one that has not moved
#: in over a week, so it is the least likely of the three to be what today is for -- yet leading
#: with it gave it 26% of the shortlist slots across the 62 briefings in the vault (34 of 133),
#: and the same few items kept taking them. Moving it last cuts that to 5% and hands 27 slots to
#: Next, changing the opening of 18 of those 62 briefings. Stalled still reaches the shortlist on
#: a day with little else, which is the day it is worth reading.
ACTIONABLE = ("Blocked", "Next", "Stalled")

#: Six status headings were more precision than the classifier behind them can deliver: the
#: distiller's own labels wander (a "Blocked" row that is really a task — see the 2026-08-26
#: artifact), and a six-way surface amplifies that instead of absorbing it. The reader's question
#: is not "which status is this" but "do I have to move" — so the headings collapse to that, and
#: the status survives as a per-item emoji rather than a box the item might be in wrongly.
#: The unlabelled bucket rides in 참고 rather than getting a zone of its own. It must ride
#: somewhere: an item the distiller failed to label is still an item, and dropping it would make
#: the briefing quietly lossy — which is worse than the ugly "기타" heading it replaces.
ZONES = (
    # The action zone is the actionable labels, not a second copy of them. It held its own tuple
    # in the old order, so moving Stalled last in `ACTIONABLE` fixed the shortlist and left the
    # zone below it still opening with items that had not moved in a week.
    ("행동", ACTIONABLE),
    ("참고", ("Risks", "Decisions", "Facts", "")),
)

#: What to ask next, per zone. The briefing is a summary of notes the reader cannot open from
#: Slack — there is no URL for a vault file — so naming the sources bought nothing actionable.
#: Naming the MCP call does: the reader is already sitting in front of an agent that can run it,
#: and these are the tools that actually answer each zone (verified against tools/list).
#: The MCP call that digs into a zone. Naming the sources bought nothing — Slack cannot open a
#: vault file — but naming the call does: the reader is already sitting in front of an agent that
#: can run it. `{p}` is filled with a project the zone actually contains rather than left as a
#: placeholder, because the briefing already knows the name.
#:
#: Deterministic tools lead. `recall` and `claims` embed and return; `next_actions`, `stalled`,
#: `decisions`, and `risks` answer straight from current claims — no LLM, sub-second. Only `ask`
#: still runs the local LLM, and it is not suggested here.
ZONE_FOLLOWUP = {
    "행동": '`recall("…", "{p}")` · `next_actions("{p}")`',
    "참고": '`claims("{p}")` · `decisions("{p}")`',
}


def pick_line(number: int, label: str, project_name: str, item) -> str:
    """One shortlist line, written the same way for both renderers.

    An item with no project prints without one. The alternative -- `1. ⏸️ Brief — …` -- opens the
    briefing by naming a project that does not exist, in the position the reader trusts most.
    """
    head = f"{number}. {SECTION_EMOJI[label]}"
    if project_name and project_name != UNATTRIBUTED:
        head = f"{head} {_slack_inline(project_name)} —"
    return f"{head} {_slack_inline(item.text)}"


def zone_followup(zone_title: str, entries) -> str:
    """The MCP call that digs into this zone, aimed at the project carrying the most of it."""
    template = ZONE_FOLLOWUP.get(zone_title)
    if not template or not entries:
        return ""
    counts: dict[str, int] = {}
    for _label, project_name, _item in entries:
        if not project_name or project_name == UNATTRIBUTED:
            # `recall("…", "Brief")` sends the reader after a project that was never real. No
            # follow-up is better than one that cannot return anything.
            continue
        counts[project_name] = counts.get(project_name, 0) + 1
    if not counts:
        return ""
    busiest = max(counts, key=lambda name: (counts[name], name))
    return template.format(p=_slack_inline(busiest))


#: How many items the top-of-message shortlist carries. Three is what fits above the fold on a
#: phone next to a header and a count line; a shortlist that needs scrolling is not a shortlist.
TOP_PICKS = 3


def top_picks(items_by_label, limit=TOP_PICKS):
    """The first thing the reader should look at, drawn from the actionable groups in order.

    A briefing that opens with a status ledger makes the reader do the triage the briefing was
    supposed to do. Blocked first because it is the reason the message exists, then Stalled
    (something has been sitting), then Next. Returns [] when nothing is actionable — a quiet day
    should not manufacture a priority.
    """
    picks: list[tuple[str, str, Any]] = []
    for label in ACTIONABLE:
        for project_name, item in items_by_label.get(label, []):
            picks.append((label, project_name, item))
            if len(picks) >= limit:
                return picks
    return picks


#: Below this many actionable items the whole message fits on one screen, so a shortlist would
#: only restate what is already visible. Measured against real briefings, which carry 20-30.
SHORTLIST_MIN_ITEMS = 6


def _shortlist_earns_its_place(items_by_label) -> bool:
    """True when there is enough to triage that naming the top three saves the reader a scroll."""
    actionable = sum(len(items_by_label.get(label, ())) for label in ACTIONABLE)
    return actionable >= SHORTLIST_MIN_ITEMS


def group_by_project(entries):
    """[(project, item)] -> [(project, [items])], first-seen order.

    Five consecutive lines that all begin with the same project name spend the first twenty
    characters of every line saying nothing new, and on a phone that is most of the line. The
    name is written once and its items nest under it.
    """
    grouped: list[tuple[str, list]] = []
    index: dict[str, int] = {}
    for project_name, item in entries:
        pos = index.get(project_name)
        if pos is None:
            index[project_name] = len(grouped)
            grouped.append((project_name, [item]))
        else:
            grouped[pos][1].append(item)
    return grouped


def zone_entries(items_by_label, labels):
    """[(label, project, item)] for one zone, in the labels' priority order."""
    out = []
    for label in labels:
        out.extend((label, project_name, item) for project_name, item in items_by_label.get(label, []))
    return out


def render_zone_lines(entries, limit, sub="   ◦"):
    """Lines for one zone: status rides on the item, the project name is written once.

    The status heading is gone, so each line carries its own emoji — a reader still sees that
    something is blocked rather than merely next, without the briefing having to be right about
    which of six boxes it belongs in.
    """
    lines: list[str] = []
    shown = 0
    grouped: list[tuple[str, list[tuple[str, object]]]] = []
    index: dict[str, int] = {}
    for label, project_name, item in entries:
        pos = index.get(project_name)
        if pos is None:
            index[project_name] = len(grouped)
            grouped.append((project_name, [(label, item)]))
        else:
            grouped[pos][1].append((label, item))

    for project_name, rows in grouped:
        if shown >= limit:
            break
        take = rows[: limit - shown]
        shown += len(take)
        if project_name == UNATTRIBUTED:
            # No project the reader could look up, so no name on the line. See `render_group_lines`.
            lines.extend(f"{SECTION_EMOJI[label]} {_slack_inline(item.text)}" for label, item in take)
            continue
        name = _slack_inline(project_name)
        if len(take) == 1:
            label, item = take[0]
            lines.append(f"{SECTION_EMOJI[label]} {name} — {_slack_inline(item.text)}")
        else:
            # Mixed statuses under one project keep their own emoji on each row.
            lines.append(f"• {name}")
            lines.extend(f"{sub} {SECTION_EMOJI[label]} {_slack_inline(item.text)}" for label, item in take)
    omitted = len(entries) - shown
    if omitted > 0:
        lines.append(f"• _외 {omitted}개 항목_")
    return lines


def render_group_lines(entries, limit, bullet="•", sub="   ◦"):
    """Lines for one status group, nested under project names and honouring the item limit.

    Shared by both renderers so a reader who falls back to text sees the same items in the same
    shape — the limits already agree, and the layout has to as well.
    """
    lines: list[str] = []
    shown = 0
    for project_name, items in group_by_project(entries):
        if shown >= limit:
            break
        room = limit - shown
        take = items[:room]
        shown += len(take)
        if project_name == UNATTRIBUTED:
            # These items had no project the reader could look up, which is why they are here.
            # Printing the group's internal name beside them puts a project back in front of the
            # reader -- one that is not a project, cannot be searched, and reads as a real name
            # sitting among real names. They go flat instead, and the line says nothing it cannot
            # support. Nearly half of all items land here (944 of 2016 across 62 briefings), so
            # this is not an edge case in the rendering.
            lines.extend(f"{bullet} {_slack_inline(i.text)}" for i in take)
            continue
        name = _slack_inline(project_name)
        if len(take) == 1:
            lines.append(f"{bullet} {name} — {_slack_inline(take[0].text)}")
        else:
            lines.append(f"{bullet} {name}")
            lines.extend(f"{sub} {_slack_inline(i.text)}" for i in take)
    omitted = sum(len(items) for _n, items in group_by_project(entries)) - shown
    if omitted > 0:
        lines.append(f"{bullet} _외 {omitted}개 항목_")
    return lines


def group_limit(label: str, total: int) -> int:
    """How many items of this group to show. The single source both renderers ask.

    Blocked returns everything: an unseen blocker is the failure mode the priority order exists
    to prevent, and a section can be split before a blocker is hidden behind "+N".
    """
    if label in NEVER_TRUNCATED:
        return total
    return DONE_ITEM_LIMIT if label == "Done" else ITEM_LIMIT


#: What Next and Stalled may show on a day the action zone would otherwise be cut short. Across
#: the 63 briefings in the vault the action zone overflowed on 27 of them, hiding 170 items behind
#: "외 N개 항목" while the reference zone below printed 351 -- twice as much read-and-forget
#: content, on exactly the mornings with the most work. At ten the overflow falls to 5 days and
#: 26 items, and the room comes from the reference zone rather than from the reader's screen.
BUSY_ITEM_LIMIT = 10


def zone_plan(items_by_label):
    """How many action items to show, and whether the reference zone yields to make room.

    One function because two renderers must not disagree about which items a reader gets; the
    fallback text and the Block Kit payload go into the same message and Slack picks between them.
    """
    action_labels = ACTIONABLE
    action = zone_entries(items_by_label, action_labels)
    plain = sum(group_limit(label, len(items_by_label.get(label, ()))) for label in action_labels)
    if len(action) <= plain:
        return plain, False
    busy = sum(
        len(items_by_label.get(label, ())) if label in NEVER_TRUNCATED else BUSY_ITEM_LIMIT
        for label in action_labels
    )
    return busy, True


def reference_counts(entries) -> str:
    """The reference zone as one line, for the mornings it has to stand aside."""
    tally: dict[str, int] = {}
    for label, _project, _item in entries:
        tally[label] = tally.get(label, 0) + 1
    parts = [
        f"{SECTION_EMOJI.get(label, '•')} {SECTION_TITLE.get(label, '기타')} {n}"
        for label, n in tally.items()
        if n
    ]
    return " · ".join(parts)
