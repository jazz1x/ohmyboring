"""English display strings for the morning card — this file holds en only."""

from __future__ import annotations

STRINGS: dict[str, str] = {
    "card_title": "☀️ Today's picks",
    "button_do": "Adopt",
    "button_defer": "Hold",
    "button_drop": "Reject",
    "verdict_do": "✓ Adopt",
    "verdict_defer": "… Hold",
    "verdict_drop": "✕ Reject",
    "confirmation_line": "Past approvals {total} · Done {done} · Pending {pending}",
    "confirmation_unknown": " · Unknown {unknown}",
    "overflow_line": "+{n} more not shown (50-block limit)",
    "todo_header": "To-do today",
    "advice_header": "Flagged for you",
    "repairs_headline": "Remaining groups {remaining}",
    "repairs_headline_with_merged": "Remaining groups {remaining} · merged yesterday {merged} rows",
    "repair_tag_label": "Align names",
    "repair_body": "Rows spelled `{variant}` — {rows} rows ({notes} notes) — become {subject}",
    "repair_button_do": "Merge",
    "repair_button_defer": "Hold",
    "repair_button_drop": "Reject",
    "repair_verdict_done": "✓ Merged — {deleted} rows deleted · rereading {reread} notes",
    "repair_verdict_failed": "✕ Merge failed — {deleted} rows deleted · {reread} notes reread · {reason}",
    "repair_verdict_unanswered": "✕ Merge unanswered — rows may already be deleted, count unknown · {reason}",
    "repair_owner_held": " · owner notes left as they are {n}: {notes}",
    "review_header": "Agent's calls",
    "review_kind_used": "Used note",
    "review_kind_contested": "Contested note",
    "review_button_do": "Agree",
    "review_button_drop": "Flip",
    "review_verdict_agree": "✓ Agreed",
    "review_verdict_flip": "↺ Flipped — owner judged {kind}",
    "superseded_label": "superseded",
}

REGISTER_LABELS: dict[str, str] = {
    "recurrences": "Recurring",
    "risks": "Risk",
    "stalled": "Stalled",
    "next_actions": "Next",
}
