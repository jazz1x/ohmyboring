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
LABEL_DISPLAY = {
    "Done": "Done",
    "Next": "Next",
    "Blocked": "Blocked",
    "Decisions": "Decision",
    "Risks": "Risk",
    "Stalled": "Stalled",
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
    doc = parse_brief(answer)
    if not doc.projects:
        return _compact_text(answer)

    out: list[str] = []
    omitted_projects = max(0, len(doc.projects) - PROJECT_LIMIT)
    for project in doc.projects[:PROJECT_LIMIT]:
        if not project.items:
            continue
        if out and out[-1] != "":
            out.append("")
        out.append(f"*{_slack_inline(project.name)}*")
        items = project.items[:ITEM_LIMIT]
        for item in items:
            out.append(f"• {format_item_mrkdwn(item)}")
        omitted_items = max(0, len(project.items) - len(items))
        if omitted_items:
            out.append(f"• _외 {omitted_items}개 항목_")
    if omitted_projects:
        if out and out[-1] != "":
            out.append("")
        out.append(f"_외 {omitted_projects}개 프로젝트 생략_")
    return "\n".join(out).strip()


def render_blocks_payload(
    title: str,
    stamp: str,
    answer: str,
    sources: list[object],
    empty_message: str,
) -> dict[str, Any]:
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

    projects = [project for project in doc.projects if project.items]
    if not projects:
        blocks.append(_section(empty_message))
    for project in projects[:BLOCK_PROJECT_LIMIT]:
        blocks.append({"type": "divider"})
        blocks.append(_section(f"*{_escape_mrkdwn(project.name)}*"))
        fields = [
            {
                "type": "mrkdwn",
                "text": _mrkdwn_text(f"*{display_label(item.label)}*\n{item.text}", 2000),
            }
            for item in project.items[:BLOCK_ITEM_LIMIT]
        ]
        if fields:
            blocks.append({"type": "section", "fields": fields})
        omitted_items = max(0, len(project.items) - BLOCK_ITEM_LIMIT)
        if omitted_items:
            blocks.append(_context(f"_외 {omitted_items}개 항목_"))
    omitted_projects = max(0, len(projects) - BLOCK_PROJECT_LIMIT)
    if omitted_projects:
        blocks.append(_context(f"_외 {omitted_projects}개 프로젝트 생략_"))

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


def format_item_mrkdwn(item: BriefItem) -> str:
    text = _slack_inline(item.text)
    if item.label:
        return f"*{display_label(item.label)}* — {text}"
    return text


def display_label(label: str) -> str:
    return LABEL_DISPLAY.get(label, label)


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
