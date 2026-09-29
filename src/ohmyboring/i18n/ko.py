"""Korean display strings for the morning card — this file holds ko only."""

from __future__ import annotations

STRINGS: dict[str, str] = {
    "card_title": "☀️ 오늘 제안",
    "button_do": "채택",
    "button_defer": "보류",
    "button_drop": "거절",
    "verdict_do": "✓ 채택",
    "verdict_defer": "… 보류",
    "verdict_drop": "✕ 거절",
    "confirmation_line": "지난 승인 {total} · 했다 {done} · 아직 {pending}",
    "confirmation_unknown": " · 확인불가 {unknown}",
    "overflow_line": "+{n}건 더 있음 (블록 50개 상한)",
    "todo_header": "오늘 할 일",
    "advice_header": "짚어 둔 것",
    "repairs_headline": "남은 묶음 {remaining}",
    "repairs_headline_with_merged": "남은 묶음 {remaining} · 어제 합친 행 {merged}",
    "repair_tag_label": "이름 맞추기",
    "repair_body": "`{variant}` 로 적힌 {rows}행(노트 {notes})을\n{subject} 로 바꿉니다",
    "repair_button_do": "실행",
    "repair_button_defer": "보류",
    "repair_button_drop": "거절",
    "repair_verdict_done": "✓ 합침 — 지운 행 {deleted} · 다시 읽는 노트 {reread}",
    "repair_verdict_failed": "✕ 합침 실패 — 지운 행 {deleted} · 다시 읽은 노트 {reread} · {reason}",
    "repair_verdict_unanswered": "✕ 합침 응답 없음 — 행이 이미 지워졌을 수 있음, 개수 모름 · {reason}",
    "repair_owner_held": " · 그대로 둔 소유자 노트 {n}: {notes}",
    "review_header": "에이전트가 가른 것",
    "review_kind_used": "쓴 노트",
    "review_kind_contested": "틀린 노트",
    "review_button_do": "맞음",
    "review_button_drop": "뒤집기",
    "review_verdict_agree": "✓ 맞음",
    "review_verdict_flip": "↺ 뒤집음 — 소유자 판정으로 {kind}",
    "superseded_label": "대체됨",
}

REGISTER_LABELS: dict[str, str] = {
    "recurrences": "재발",
    "risks": "위험",
    "stalled": "정체",
    "next_actions": "다음",
}
