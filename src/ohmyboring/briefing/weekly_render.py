"""주간 렌더 — 일자별 관측치를 지속·추세로 보여 준다. 마감률은 말하지 않는다."""

from __future__ import annotations

from typing import Any

from ohmyboring.briefing.text import _context, _mrkdwn_text, _plain_text, _section
from ohmyboring.briefing.zones import SECTION_EMOJI, SECTION_TITLE

#: The weekly's whole message when a week produced nothing worth saying. Lived in
#: weekly-briefing.py until the weekly card needed to recognise it on stdout; the renderer is
#: the SSOT so the script and the poster never drift apart.
EMPTY_MESSAGE = "이번 주는 새로 짚을 진행/막힘 항목이 회수되지 않았어요."


def render_weekly_blocks(title, stamp, projects, intervention, board, trend, sources):  # noqa: PLR0913
    """Block Kit for the weekly: persistence and trend, never closure.

    Item bullets are deliberately absent except one per persistent project, quoted from the most
    recent daily rather than re-summarised. A weekly that re-lists items is the same information a
    seventh time; what it uniquely knows is which projects held a state all week — something the
    reader could only see by deduping seven messages in their head.
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": _plain_text(title, 150), "emoji": True},
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _mrkdwn_text(stamp, 2000)}]},
    ]

    if intervention:
        lines = []
        for week, label, count, span in intervention:
            head = f"• {week.name} — {count}/{span}일 {SECTION_EMOJI.get(label, '•')}"
            lines.append(f"{head}\n  {week.latest_line}" if week.latest_line else head)
        blocks.append(_section("*개입 필요 — 상태가 주 내내 지속*\n" + "\n".join(lines)))
        blocks.append({"type": "divider"})

    if board:
        # Two columns are right here and wrong on the daily: these values are short. Slack caps
        # a section at 10 fields, which is why the caller bounds the list before it arrives.
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": _mrkdwn_text(f"*{w.name}*\n{len(w.days)}/{span}일", 2000)}
                    for w, span in board
                ][:10],
            }
        )

    if trend:
        parts = [
            f"{SECTION_EMOJI.get(label, '•')} {SECTION_TITLE.get(label, label)} {first}→{last}"
            for label, (first, last) in trend.items()
        ]
        # The caveat is part of the contract, not decoration: a reader who adds ✅ 10→10 into
        # "twenty done" has been lied to, and every day here is an independent re-synthesis.
        blocks.append(_context(" · ".join(parts) + " — 일자별 관측치이며 마감률이 아니다"))

    return blocks[:50]


def render_weekly_mrkdwn(title, stamp, intervention, board, trend, sources) -> str:  # noqa: PLR0913
    """The weekly as text, carrying what the blocks carry.

    Without this the persistence weekly could only be emitted as a Block Kit payload, so it sat
    behind `BORING_BRIEFING_FORMAT=blocks` — unset in production, because cron delivery is
    text-only (`_standalone_send` posts `text`). The feature was merged, tested, and structurally
    unreachable: every Monday sent a re-synthesised `/weekly` instead, which is the one thing the
    persistence weekly exists to replace.

    Same content as `render_weekly_blocks`, including the caveat. A text rendering that quietly
    drops "일자별 관측치이며 마감률이 아니다" would let a reader add ✅ 10→10 into "twenty done".
    """
    out = [f"*{title}*", f"`{stamp}`"]
    if intervention:
        out.append("")
        out.append("*개입 필요 — 상태가 주 내내 지속*")
        for week, label, count, span in intervention:
            head = f"• {week.name} — {count}/{span}일 {SECTION_EMOJI.get(label, '•')}"
            out.append(f"{head}\n   {week.latest_line}" if week.latest_line else head)
    if board:
        out.append("")
        out.append("*주간 점유*")
        out.append(" · ".join(f"{w.name} {len(w.days)}/{span}일" for w, span in board))
    if trend:
        parts = [
            f"{SECTION_EMOJI.get(label, '•')} {SECTION_TITLE.get(label, label)} {first}→{last}"
            for label, (first, last) in trend.items()
        ]
        out.append("")
        out.append("_" + " · ".join(parts) + " — 일자별 관측치이며 마감률이 아니다_")
    return "\n".join(out)
