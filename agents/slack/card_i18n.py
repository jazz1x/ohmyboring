#!/usr/bin/env python3
"""The morning card's one display-string table -- en/ko/ja, nothing else.

Every string the card puts in front of the owner (title, buttons, verdict marks, the
past-approval head line, the unassigned-project bucket name) lives here, keyed by the
language `card_core.resolve_lang(boring_config.note_lang())` picks. card_core.py and
card.py never spell out a display string themselves -- a Korean literal in either file's
display path is the bug this file exists to remove. The model's own advice prompt is not
a display string (nobody reads it in Slack) and stays out of this table; card_core attaches
its own language instruction to that prompt separately, the way distill_core does.
"""

from __future__ import annotations

from typing import Literal

Lang = Literal["en", "ko", "ja"]

STRINGS: dict[Lang, dict[str, str]] = {
    "en": {
        "card_title": "☀️ Today's picks",
        "button_do": "Do",
        "button_defer": "Defer",
        "button_drop": "Drop",
        "verdict_do": "✓ Do",
        "verdict_defer": "… Defer",
        "verdict_drop": "✕ Drop",
        "confirmation_line": "Past approvals {total} · Done {done} · Pending {pending}",
        "confirmation_unknown": " · Unknown {unknown}",
        "unassigned_project": "Unassigned",
        "bottleneck_label": "Bottleneck",
    },
    "ko": {
        "card_title": "☀️ 오늘 제안",
        "button_do": "해",
        "button_defer": "미뤄",
        "button_drop": "빼",
        "verdict_do": "✓ 해",
        "verdict_defer": "… 미뤄",
        "verdict_drop": "✕ 빼",
        "confirmation_line": "지난 승인 {total} · 했다 {done} · 아직 {pending}",
        "confirmation_unknown": " · 확인불가 {unknown}",
        "unassigned_project": "무소속",
        "bottleneck_label": "병목",
    },
    "ja": {
        "card_title": "☀️ 今日の提案",
        "button_do": "やる",
        "button_defer": "後で",
        "button_drop": "却下",
        "verdict_do": "✓ やる",
        "verdict_defer": "… 後で",
        "verdict_drop": "✕ 却下",
        "confirmation_line": "過去の承認 {total} · 完了 {done} · 保留 {pending}",
        "confirmation_unknown": " · 不明 {unknown}",
        "unassigned_project": "未所属",
        "bottleneck_label": "ボトルネック",
    },
}
