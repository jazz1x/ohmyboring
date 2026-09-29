"""브리핑 Block Kit 렌더 — mrkdwn 렌더와 같은 존·고른 항목을 블록으로."""

from __future__ import annotations

import json
import os
from typing import Any

from ohmyboring.briefing.mrkdwn import render_message_mrkdwn
from ohmyboring.briefing.notices import audit_notice, window_notice
from ohmyboring.briefing.parse import BriefItem, parse_brief
from ohmyboring.briefing.text import _context, _dedup_key, _mrkdwn_text, _plain_text, _section
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


def render_blocks_payload(  # noqa: C901, PLR0912, PLR0913
    title: str,
    stamp: str,
    answer: str,
    sources: list[object],
    empty_message: str,
    label_stats=None,
    uptake_stats=None,
    known_projects=None,
) -> dict[str, Any]:
    """Block Kit version of the priority-first briefing.

    Uses single-column sections instead of two-column fields: each status
    group is a clear visual chunk on mobile.
    """
    doc = parse_brief(answer, known_projects)
    fallback = render_message_mrkdwn(
        title, stamp, answer, sources, empty_message, label_stats, uptake_stats, known_projects
    )
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": _plain_text(title, 150), "emoji": True},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": _mrkdwn_text(f"`{stamp}`", 2000)}],
        },
    ]

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
                if project.name not in prev_project.split(" / "):
                    seen[key] = (f"{prev_project} / {project.name}", prev_item)
                continue
            seen[key] = (project.name, item)
            items_by_label[label].append((project.name, item))

    if not any(items_by_label.values()):
        blocks.append(_section(empty_message))
    else:
        # The count line says the words, not just the emoji: a reader should not have to have
        # memorised a legend to know that 🚨 2 means two things are blocking them.
        counts = [
            f"{SECTION_EMOJI[label]} {SECTION_TITLE[label]} {len(items_by_label[label])}"
            for label in SECTION_ORDER
            if items_by_label[label]
        ]

        # The shortlist goes above everything, because the question at 9am is "what first" and a
        # status ledger makes the reader answer it themselves.
        picks = top_picks(items_by_label)
        if picks and _shortlist_earns_its_place(items_by_label):
            pick_lines = [
                pick_line(n, label, project_name, item)
                for n, (label, project_name, item) in enumerate(picks, 1)
            ]
            blocks.append(_section("*오늘의 1순위*\n" + "\n".join(pick_lines)))
            blocks.append({"type": "divider"})

        action_limit, reference_yields = zone_plan(items_by_label)
        for zone_title, labels in ZONES:
            entries = zone_entries(items_by_label, labels)
            if not entries:
                continue
            if labels is not ACTIONABLE and reference_yields:
                blocks.append(_context(f"*{zone_title}* ({len(entries)}) — {reference_counts(entries)}"))
                continue
            # Done stays out of the zones entirely: it is confirmation, not work, and on a phone
            # sixteen finished items push the blockers off the first screen.
            zone_limit = (
                action_limit
                if labels is ACTIONABLE
                else sum(group_limit(label, len(items_by_label.get(label, ()))) for label in labels)
            )
            item_lines = render_zone_lines(entries, zone_limit)
            blocks.append(_section(f"*{zone_title}* ({len(entries)})\n" + "\n".join(item_lines)))
            followup = zone_followup(zone_title, entries)
            if followup:
                # A context block: present when wanted, visually quiet when not.
                blocks.append(_context(f"→ {followup}"))

        blocks.insert(2, _context(" · ".join(counts)))

    for notice in (window_notice(uptake_stats), audit_notice(label_stats)):
        if notice:
            blocks.append({"type": "divider"})
            blocks.append(_context(notice))
    return {
        "text": fallback,
        "blocks": blocks[:50],
        "unfurl_links": False,
        "unfurl_media": False,
    }


def maybe_print_blocks_json(  # noqa: PLR0913
    title: str,
    stamp: str,
    answer: str,
    sources: list[object],
    empty_message: str,
    label_stats=None,
    uptake_stats=None,
    known_projects=None,
) -> bool:
    if os.environ.get("BORING_BRIEFING_FORMAT", "").strip().lower() != "blocks":
        return False
    payload = render_blocks_payload(
        title, stamp, answer, sources, empty_message, label_stats, uptake_stats, known_projects
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return True
