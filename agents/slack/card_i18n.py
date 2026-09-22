#!/usr/bin/env python3
"""The morning card's one display-string table -- en/ko/ja, nothing else.

Every string the card puts in front of the owner (title, buttons, verdict marks, the
past-approval head line, the unassigned-project bucket name, the register tag's localized
name, the block-limit overflow line) lives here, keyed by the language
`card_advice.resolve_lang(boring_config.note_lang())` picks. card_view.py and card.py never
spell out a display string themselves -- a Korean literal in either file's display path is
the bug this file exists to remove. Register *icons* are not translatable text and stay out
of this table (card_view.REGISTER_ICONS). The model's own advice prompt is not a display
string (nobody reads it in Slack) and stays out of this table; card_advice attaches its own
language instruction to that prompt separately, the way distill_core does.
"""

from __future__ import annotations

from typing import Literal

Lang = Literal["en", "ko", "ja"]

STRINGS: dict[Lang, dict[str, str]] = {
    "en": {
        "card_title": "☀️ Today's picks",
        "button_do": "Adopt",
        "button_defer": "Hold",
        "button_drop": "Reject",
        "verdict_do": "✓ Adopt",
        "verdict_defer": "… Hold",
        "verdict_drop": "✕ Reject",
        "confirmation_line": "Past approvals {total} · Done {done} · Pending {pending}",
        "confirmation_unknown": " · Unknown {unknown}",
        "unassigned_project": "No project",
        "overflow_line": "+{n} more not shown (50-block limit)",
    },
    "ko": {
        "card_title": "☀️ 오늘 제안",
        "button_do": "채택",
        "button_defer": "보류",
        "button_drop": "거절",
        "verdict_do": "✓ 채택",
        "verdict_defer": "… 보류",
        "verdict_drop": "✕ 거절",
        "confirmation_line": "지난 승인 {total} · 했다 {done} · 아직 {pending}",
        "confirmation_unknown": " · 확인불가 {unknown}",
        "unassigned_project": "프로젝트 없음",
        "overflow_line": "+{n}건 더 있음 (블록 50개 상한)",
    },
    "ja": {
        "card_title": "☀️ 今日の提案",
        "button_do": "採用",
        "button_defer": "保留",
        "button_drop": "却下",
        "verdict_do": "✓ 採用",
        "verdict_defer": "… 保留",
        "verdict_drop": "✕ 却下",
        "confirmation_line": "過去の承認 {total} · 完了 {done} · 保留 {pending}",
        "confirmation_unknown": " · 不明 {unknown}",
        "unassigned_project": "プロジェクトなし",
        "overflow_line": "他{n}件（50ブロック上限）",
    },
}

#: The register tag's localized name — recurrences/risks/stalled/next_actions, per language.
#: Icons are language-independent and live in card_view.REGISTER_ICONS instead.
REGISTER_LABELS: dict[Lang, dict[str, str]] = {
    "en": {
        "recurrences": "Recurring",
        "risks": "Risk",
        "stalled": "Stalled",
        "next_actions": "Next",
    },
    "ko": {
        "recurrences": "재발",
        "risks": "위험",
        "stalled": "정체",
        "next_actions": "다음",
    },
    "ja": {
        "recurrences": "再発",
        "risks": "リスク",
        "stalled": "滞留",
        "next_actions": "次",
    },
}
