"""브리핑 mrkdwn 렌더 — 답 한 덩어리를 우선순위 순 본문 텍스트로."""

from __future__ import annotations

from ohmyboring.briefing.notices import audit_notice, window_notice
from ohmyboring.briefing.parse import BriefItem, parse_brief
from ohmyboring.briefing.text import _compact_text, _dedup_key
from ohmyboring.briefing.zones import (
    ACTIONABLE,
    SECTION_EMOJI,
    SECTION_ORDER,
    SECTION_TITLE,
    ZONES,
    _shortlist_earns_its_place,
    group_limit,
    pick_line,
    reference_counts,
    render_zone_lines,
    top_picks,
    zone_entries,
    zone_followup,
    zone_plan,
)


def render_message_mrkdwn(  # noqa: PLR0913
    title: str,
    stamp: str,
    answer: str,
    sources: list[object],
    empty_message: str,
    label_stats=None,
    uptake_stats=None,
    known_projects=None,
) -> str:
    body = render_body_mrkdwn(answer, known_projects)
    if not body:
        body = empty_message
    out = f"{title}\n`{stamp}`\n\n{body}"
    for notice in (window_notice(uptake_stats), audit_notice(label_stats)):
        if notice:
            out += f"\n\n{notice}"
    return out


def render_body_mrkdwn(answer: str, known_projects=None) -> str:  # noqa: C901, PLR0912
    """Render a priority-first briefing body.

    The reader should grasp the day in one glance:
    1) summary counts, 2) blockers, 3) next actions, 4) context/decisions,
    5) recently done. Project names stay attached to each item so context
    is never lost.
    """
    doc = parse_brief(answer, known_projects)
    if not doc.projects:
        return _compact_text(answer)

    items_by_label: dict[str, list[tuple[str, BriefItem]]] = {label: [] for label in SECTION_ORDER}
    seen: dict[str, tuple[str, BriefItem]] = {}
    for project in doc.projects:
        for item in project.items:
            label = item.label or ""
            if label not in items_by_label:
                label = ""
            key = _dedup_key(item.text)
            if key in seen:
                prev_project, prev_item = seen[key]
                # Merge project names if the text is identical.
                if project.name not in prev_project.split(" / "):
                    seen[key] = (f"{prev_project} / {project.name}", prev_item)
                continue
            seen[key] = (project.name, item)
            items_by_label[label].append((project.name, item))

    if not any(items_by_label.values()):
        return _compact_text(answer)

    counts: list[str] = []
    lines: list[str] = []

    # The same shortlist the blocks lead with. Slack picks between the two renderings, so a
    # reader who falls back must not lose the one thing the message exists to answer.
    # The shortlist earns its place by saving a scroll. When the message is short enough that
    # every pick is already visible in the groups below without scrolling, it only repeats
    # itself — a summary of a screenful is not a summary.
    picks = top_picks(items_by_label)
    if picks and _shortlist_earns_its_place(items_by_label):
        lines.append("*오늘의 1순위*")
        lines.extend(
            pick_line(n, label, project_name, item) for n, (label, project_name, item) in enumerate(picks, 1)
        )
        lines.append("")

    for label in SECTION_ORDER:
        entries = items_by_label[label]
        if entries:
            counts.append(f"{SECTION_EMOJI[label]} {SECTION_TITLE[label]} {len(entries)}")

    action_limit, reference_yields = zone_plan(items_by_label)
    for zone_title, labels in ZONES:
        entries = zone_entries(items_by_label, labels)
        if not entries:
            continue
        if labels is not ACTIONABLE and reference_yields:
            # There is more work this morning than fits. The reference zone is read and forgotten;
            # the action zone is the reason the message was sent, so it takes the room and this
            # says only how much is waiting below.
            lines.append(f"*{zone_title}* ({len(entries)}) — {reference_counts(entries)}")
            lines.append("")
            continue
        limit = (
            action_limit
            if labels is ACTIONABLE
            else sum(group_limit(label, len(items_by_label.get(label, ()))) for label in labels)
        )
        lines.append(f"*{zone_title}* ({len(entries)})")
        lines.extend(render_zone_lines(entries, limit))
        followup = zone_followup(zone_title, entries)
        if followup:
            lines.append(f"_→ {followup}_")
        lines.append("")

    # The count line at the top already says "✅ 완료 N". Repeating it at the bottom was the same
    # number twice in one message -- 7 of the 7 sent briefings that had any Done items, and on
    # 09-02 the repeat was the entire body. The count survives; the echo does not.
    return f"{' · '.join(counts)}\n\n" + "\n".join(lines).strip()
