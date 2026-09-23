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
        "todo_header": "To-do today",
        "advice_header": "Flagged for you",
        "repairs_headline": "Remaining groups {remaining}",
        "repairs_headline_with_merged": "Remaining groups {remaining} · merged yesterday {merged} rows",
        "repair_tag_label": "Merge",
        "repair_section_title": "Two spellings, one subject",
        "repair_body": "`{variant}` {rows} rows · {notes} notes",
        "repair_button_do": "Merge",
        "repair_button_defer": "Hold",
        "repair_button_drop": "Reject",
        "repair_verdict_done": "✓ Merged — {deleted} rows deleted · {reread} notes reread",
        "repair_verdict_failed": "✕ Merge failed — {deleted} rows deleted · {reread} notes reread · {reason}",
        "review_header": "Agent's calls",
        "review_kind_used": "Used note",
        "review_kind_contested": "Contested note",
        "review_button_do": "Agree",
        "review_button_drop": "Flip",
        "review_verdict_agree": "✓ Agreed",
        "review_verdict_flip": "↺ Flipped — owner judged {kind}",
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
        "todo_header": "오늘 할 일",
        "advice_header": "짚어 둔 것",
        "repairs_headline": "남은 묶음 {remaining}",
        "repairs_headline_with_merged": "남은 묶음 {remaining} · 어제 합친 행 {merged}",
        "repair_tag_label": "합치기",
        "repair_section_title": "주어 두 표기를 하나로",
        "repair_body": "`{variant}` {rows}행 · 노트 {notes}",
        "repair_button_do": "실행",
        "repair_button_defer": "보류",
        "repair_button_drop": "거절",
        "repair_verdict_done": "✓ 합침 — 지운 행 {deleted} · 다시 읽은 노트 {reread}",
        "repair_verdict_failed": "✕ 합침 실패 — 지운 행 {deleted} · 다시 읽은 노트 {reread} · {reason}",
        "review_header": "에이전트가 가른 것",
        "review_kind_used": "쓴 노트",
        "review_kind_contested": "틀린 노트",
        "review_button_do": "맞음",
        "review_button_drop": "뒤집기",
        "review_verdict_agree": "✓ 맞음",
        "review_verdict_flip": "↺ 뒤집음 — 소유자 판정으로 {kind}",
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
        "todo_header": "今日やること",
        "advice_header": "気になった点",
        "repairs_headline": "残りグループ{remaining}",
        "repairs_headline_with_merged": "残りグループ{remaining} · 昨日統合{merged}行",
        "repair_tag_label": "統合",
        "repair_section_title": "二つの表記を一つに",
        "repair_body": "`{variant}` {rows}行 · ノート{notes}件",
        "repair_button_do": "統合",
        "repair_button_defer": "保留",
        "repair_button_drop": "却下",
        "repair_verdict_done": "✓ 統合 — 削除{deleted}行 · 再読込{reread}件",
        "repair_verdict_failed": "✕ 統合失敗 — 削除{deleted}行 · 再読込{reread}件 · {reason}",
        "review_header": "エージェントの判定",
        "review_kind_used": "使ったノート",
        "review_kind_contested": "間違ったノート",
        "review_button_do": "合っている",
        "review_button_drop": "ひっくり返す",
        "review_verdict_agree": "✓ 合っている",
        "review_verdict_flip": "↺ 反転 — 所有者の判定で{kind}",
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
