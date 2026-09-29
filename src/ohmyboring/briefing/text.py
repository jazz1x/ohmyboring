"""브리핑 글자 다듬기 — 한국어 끝맺음 깎기와 Slack 블록 조각."""

from __future__ import annotations

from typing import Any

#: Endings the distiller pads every line with. Korean puts the verb last, so truncating from the
#: front deletes the action and leaves the object — which is why these are shaved rather than the
#: line being cut. Each pattern keeps the verb stem and drops only the politeness tail, and a line
#: that matches nothing is left exactly as written.
_ENDING_TRIMS = (
    ("해야 합니다.", ""),
    ("해야 한다.", ""),
    ("이 필요합니다.", " 필요"),
    ("가 필요합니다.", " 필요"),
    ("이 필요함.", " 필요"),
    ("가 필요함.", " 필요"),
    ("하였습니다.", "함"),
    ("했습니다.", "함"),
    ("합니다.", "함"),
    ("됩니다.", "됨"),
    ("입니다.", ""),
    ("되었습니다.", "됨"),
    ("있습니다.", "있음"),
)


def shave_ending(text: str) -> str:
    """Drop the politeness tail, keep the verb.

    "…근거를 보강해야 합니다" -> "…근거를 보강". Purely a rendering concern: the wording comes from
    the distillation prompt, and changing that would change the notes the injection channel is
    being measured on, which is frozen until the window closes (docs/PRD.md §5-R3).
    """
    stripped = text.rstrip()
    for tail, replacement in _ENDING_TRIMS:
        if stripped.endswith(tail):
            return (stripped[: -len(tail)] + replacement).rstrip()
    return text


def _slack_inline(text: str) -> str:
    return shave_ending(text.replace("**", "*").strip())


def _compact_text(text: str) -> str:
    lines = [_slack_inline(line.strip()) for line in text.splitlines() if line.strip()]
    return "\n".join(lines).strip()


def _dedup_key(text: str) -> str:
    """Normalize item text so near-duplicate bullets collapse to one entry."""
    return " ".join(text.lower().split())


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _mrkdwn_text(text, 3000)}}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _mrkdwn_text(text, 2000)}]}


def _plain_text(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact[:limit] or "Briefing"


def _mrkdwn_text(text: str, limit: int) -> str:
    """Escape and fit into Slack's per-field limit, saying so when something was dropped.

    A bare slice cuts mid-sentence and mid-word with no sign it happened, which is how a reader
    ends up trusting a sentence that was never finished. Cut at a line boundary when there is one
    nearby, and always leave the ellipsis behind.
    """
    escaped = _escape_mrkdwn(text)
    if len(escaped) <= limit:
        return escaped or " "
    head = escaped[: limit - 2]
    cut = head.rfind("\n")
    if cut > limit // 2:
        head = head[:cut]
    return head.rstrip() + "…"


def _escape_mrkdwn(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
