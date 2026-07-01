"""Slack briefing renderer.

The current Hermes cron path sends stdout as chat.postMessage text, so the
default renderer returns mrkdwn text. The same parsed structure can also emit a
Block Kit payload for a future adapter that posts JSON with `blocks`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


EMPTY_VALUES = {"", "-", "없음", "없습니다", "none", "None", "N/A", "n/a"}
SOURCE_LIMIT = 5
PROJECT_LIMIT = 6
ITEM_LIMIT = 5
BLOCK_PROJECT_LIMIT = 5
BLOCK_ITEM_LIMIT = 4

LABEL_ALIASES = {
    "Done": "Done",
    "완료": "Done",
    "Next": "Next",
    "다음": "Next",
    "Blocked": "Blocked",
    "막힘": "Blocked",
    "Decisions": "Decisions",
    "결정": "Decisions",
    "Risks": "Risks",
    "리스크": "Risks",
    "Stalled": "Stalled",
    "정체": "Stalled",
}
LABELS = set(LABEL_ALIASES)

# Briefing is read, not searched. Group by status priority so the reader
# sees "what blocks me" first, "what to do next" second, and "what finished"
# last. Keep sections short; mobile Slack rewards vertical scannability.
SECTION_ORDER = ["Blocked", "Next", "Stalled", "Risks", "Decisions", "Done", ""]
SECTION_EMOJI = {
    "Blocked": "🚨",
    "Next": "▶️",
    "Stalled": "⏸️",
    "Risks": "⚠️",
    "Decisions": "💡",
    "Done": "✅",
    "": "•",
}
SECTION_TITLE = {
    "Blocked": "막힘",
    "Next": "다음 행동",
    "Stalled": "정체 중",
    "Risks": "리스크",
    "Decisions": "결정",
    "Done": "완료",
    "": "기타",
}


@dataclass
class BriefItem:
    label: str
    text: str


@dataclass
class BriefProject:
    name: str
    items: list[BriefItem] = field(default_factory=list)


@dataclass
class BriefDocument:
    projects: list[BriefProject] = field(default_factory=list)


def source_label(source: object) -> str:
    return os.path.basename(str(source)) or str(source)


def render_message_mrkdwn(
    title: str,
    stamp: str,
    answer: str,
    sources: list[object],
    empty_message: str,
) -> str:
    body = render_body_mrkdwn(answer)
    if not body:
        body = empty_message
    out = f"{title}\n`{stamp}`\n\n{body}"
    source_text = render_sources(sources)
    if source_text:
        out += f"\n\n_{source_text}_"
    return out


def render_body_mrkdwn(answer: str) -> str:
    """Render a priority-first briefing body.

    The reader should grasp the day in one glance:
    1) summary counts, 2) blockers, 3) next actions, 4) context/decisions,
    5) recently done. Project names stay attached to each item so context
    is never lost.
    """
    doc = parse_brief(answer)
    if not doc.projects:
        return _compact_text(answer)

    items_by_label: dict[str, list[tuple[str, BriefItem]]] = {
        label: [] for label in SECTION_ORDER
    }
    for project in doc.projects:
        for item in project.items:
            label = item.label or ""
            if label not in items_by_label:
                label = ""
            items_by_label[label].append((project.name, item))

    if not any(items_by_label.values()):
        return _compact_text(answer)

    counts: list[str] = []
    lines: list[str] = []
    for label in SECTION_ORDER:
        entries = items_by_label[label]
        if not entries:
            continue
        emoji = SECTION_EMOJI[label]
        title = SECTION_TITLE[label]
        counts.append(f"{emoji} {len(entries)}")
        lines.append(f"{emoji} *{title}*")
        for project_name, item in entries[:ITEM_LIMIT]:
            text = _slack_inline(item.text)
            lines.append(f"• {_slack_inline(project_name)} — {text}")
        omitted = max(0, len(entries) - ITEM_LIMIT)
        if omitted:
            lines.append(f"• _외 {omitted}개 항목_")
        lines.append("")

    return f"{' · '.join(counts)}\n\n" + "\n".join(lines).strip()


def render_blocks_payload(
    title: str,
    stamp: str,
    answer: str,
    sources: list[object],
    empty_message: str,
) -> dict[str, Any]:
    """Block Kit version of the priority-first briefing.

    Uses single-column sections instead of two-column fields: each status
    group is a clear visual chunk on mobile.
    """
    doc = parse_brief(answer)
    fallback = render_message_mrkdwn(title, stamp, answer, sources, empty_message)
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

    items_by_label: dict[str, list[tuple[str, BriefItem]]] = {
        label: [] for label in SECTION_ORDER
    }
    for project in doc.projects:
        for item in project.items:
            label = item.label or ""
            if label not in items_by_label:
                label = ""
            items_by_label[label].append((project.name, item))

    if not any(items_by_label.values()):
        blocks.append(_section(empty_message))
    else:
        counts: list[str] = []
        for label in SECTION_ORDER:
            entries = items_by_label[label]
            if not entries:
                continue
            emoji = SECTION_EMOJI[label]
            title_text = SECTION_TITLE[label]
            counts.append(f"{emoji} {len(entries)}")
            blocks.append({"type": "divider"})
            blocks.append(_section(f"{emoji} *{title_text}*"))
            item_lines: list[str] = []
            for project_name, item in entries[:BLOCK_ITEM_LIMIT]:
                item_lines.append(f"• {project_name} — {item.text}")
            omitted = max(0, len(entries) - BLOCK_ITEM_LIMIT)
            if omitted:
                item_lines.append(f"• _외 {omitted}개 항목_")
            if item_lines:
                blocks.append(_section("\n".join(item_lines)))
        blocks.insert(2, _context(" · ".join(counts)))

    source_text = render_sources(sources)
    if source_text:
        blocks.append({"type": "divider"})
        blocks.append(_context(source_text))
    return {
        "text": fallback,
        "blocks": blocks[:50],
        "unfurl_links": False,
        "unfurl_media": False,
    }


def render_sources(sources: list[object]) -> str:
    labels = [source_label(source) for source in sources[:SOURCE_LIMIT]]
    return "근거: " + " · ".join(labels) if labels else ""


def parse_brief(answer: str) -> BriefDocument:
    doc = BriefDocument()
    current: BriefProject | None = None
    previous_heading = ""
    pending_label = ""

    for raw in answer.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading and heading != previous_heading:
                current = BriefProject(heading)
                doc.projects.append(current)
                previous_heading = heading
            pending_label = ""
            continue

        plain = _plain_label(stripped)
        if plain in LABELS:
            pending_label = canonical_label(plain)
            continue

        bullet = _strip_bullet(stripped)
        item = parse_item(bullet if bullet is not None else stripped, pending_label)
        # A plain (non-bullet) line consumes the pending label; a bullet line keeps
        # it so multiple bullets under one label heading share the same label.
        if bullet is None:
            pending_label = ""
        if item is None:
            continue
        if current is None:
            current = BriefProject("Brief")
            doc.projects.append(current)
        current.items.append(item)

    doc.projects = [project for project in doc.projects if project.items]
    return doc


def parse_item(text: str, pending_label: str = "") -> BriefItem | None:
    normalized = _slack_inline(text)
    for label in LABELS:
        for sep in (":", "：", " - ", " — "):
            prefix = f"{label}{sep}"
            if normalized.startswith(prefix):
                rest = normalized[len(prefix) :].strip()
                if rest in EMPTY_VALUES:
                    return None
                return BriefItem(canonical_label(label), rest)
    if pending_label:
        if normalized in EMPTY_VALUES:
            return None
        return BriefItem(pending_label, normalized)
    if normalized in EMPTY_VALUES:
        return None
    return BriefItem("", normalized)


def canonical_label(label: str) -> str:
    return LABEL_ALIASES.get(label, label)


def maybe_print_blocks_json(
    title: str,
    stamp: str,
    answer: str,
    sources: list[object],
    empty_message: str,
) -> bool:
    if os.environ.get("BORING_BRIEFING_FORMAT", "").strip().lower() != "blocks":
        return False
    payload = render_blocks_payload(title, stamp, answer, sources, empty_message)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return True


def _strip_bullet(line: str) -> str | None:
    if line.startswith(("- ", "* ", "• ")):
        return line[2:].strip()
    head, sep, tail = line.partition(". ")
    if sep and head.isdigit():
        return tail.strip()
    return None


def _plain_label(line: str) -> str:
    return line.strip().strip("*").strip().rstrip(":：")


def _slack_inline(text: str) -> str:
    return text.replace("**", "*").strip()


def _compact_text(text: str) -> str:
    lines = [_slack_inline(line.strip()) for line in text.splitlines() if line.strip()]
    return "\n".join(lines).strip()


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _mrkdwn_text(text, 3000)}}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _mrkdwn_text(text, 2000)}]}


def _plain_text(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact[:limit] or "Briefing"


def _mrkdwn_text(text: str, limit: int) -> str:
    return _escape_mrkdwn(text)[:limit] or " "


def _escape_mrkdwn(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
