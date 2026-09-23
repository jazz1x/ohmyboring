#!/usr/bin/env python3
"""The morning card's Block Kit layout (v2) — no Slack client, no LLM, no engine.

build_blocks is the only entry point: proposals in (grouped by project), a Block Kit list
out, judged rows showing their mark instead of buttons. Every display string comes from
card_i18n.STRINGS/REGISTER_LABELS, keyed by the language the caller passes — no literal
display text lives in this file's own code. One proposal is five blocks: divider, a
register-tag context line, a section (advice + bottleneck), a top-level rich_text quote
(Slack's context blocks cannot carry rich text), and an actions row or — once judged — a
context line with the verdict mark. No `/vault/wiki/...` path appears anywhere in the
output; only the short `wiki-NNNN` form does. Slack caps one message at 50 blocks: past
that, this stops adding proposal rows and reports how many were left out in a final context
line instead of silently dropping them.

When the caller passes repair data (`repairs`/`repairs_total_groups`/`merged_yesterday_rows`
— all optional, and off by default so every existing single-lane caller renders exactly as
before), the card grows a second lane above the advice one: a head-line context block
("remaining groups n · merged yesterday m"), then, if `repairs` itself is non-empty, a
「오늘 할 일」 section label and one four-block row per repair group (divider, tag, section,
actions-or-mark), then a 「짚어 둔 것」 section label before the existing advice rows. A
repair row's button idx shares one space with the advice rows — repairs first, 0..k-1 — so
`card.py`'s record_verdict can tell the two lanes apart by idx alone."""

from __future__ import annotations

from collections.abc import Iterable

import card_i18n
from card_types import (
    CHOICES,
    ButtonVerdict,
    Confirmation,
    Evidence,
    Proposal,
    ProposedVerdict,
    Repair,
    RepairDone,
    RepairFailed,
)

#: Language-independent register glyphs — the words come from card_i18n.REGISTER_LABELS.
REGISTER_ICONS: dict[str, str] = {
    "recurrences": "🔁",
    "risks": "⚠️",
    "stalled": "🧊",
    "next_actions": "➡️",
}

#: Slack's own hard cap on blocks in one message.
BLOCK_LIMIT = 50

#: How much of a cited quote shows on the card — the note+line is the coordinate, the quote
#: is just enough for the owner to recognize it without opening the note.
EVIDENCE_QUOTE_CHARS = 60
#: Evidence lines shown per proposal — a candidate may ground on more, the card shows the head.
EVIDENCE_LINES_SHOWN = 2


def _note_label(note: str) -> str:
    """`/vault/wiki/wiki-0576.md` → `wiki-0576` — the short form the owner already reads,
    and the only form of the note path this module ever puts in a block (AC7)."""
    base = note.rsplit("/", 1)[-1]
    return base[:-3] if base.endswith(".md") else base


def _actions_block(idx: int, proposal: Proposal, strings: dict[str, str]) -> dict:
    elements = []
    for choice in CHOICES:
        button = {
            "type": "button",
            "text": {"type": "plain_text", "text": strings[f"button_{choice}"], "emoji": True},
            "action_id": f"card:{idx}:{choice}",
            "value": _note_label(proposal.note),
        }
        if choice == "do":
            button["style"] = "primary"
        elif choice == "drop":
            button["style"] = "danger"
        elements.append(button)
    return {"type": "actions", "elements": elements}


def _verdict_block(verdict: ButtonVerdict, strings: dict[str, str]) -> dict:
    mark = strings[f"verdict_{verdict.choice}"]
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{mark} — <@{verdict.user}>"}]}


def _tag_block(idx: int, total: int, proposal: Proposal, register_labels: dict[str, str]) -> dict:
    icon = REGISTER_ICONS[proposal.register_]
    label = register_labels[proposal.register_]
    note = _note_label(proposal.note)
    text = f"{icon}  *{label}*  ·  `{note}`  ·  {idx + 1}/{total}"
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _section_block(proposal: Proposal) -> dict:
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*{proposal.advice}*\n{proposal.bottleneck}"},
    }


def _quote_block(evidence: list[Evidence]) -> dict:
    """A top-level `rich_text` block holding one `rich_text_quote` — Slack's `context` blocks
    only take mrkdwn/plain_text, never rich text, so the evidence quote cannot live there
    (AC4). Two or more pieces of evidence share one quote block, a blank line between them."""
    elements: list[dict] = []
    for i, e in enumerate(evidence[:EVIDENCE_LINES_SHOWN]):
        if i:
            elements.append({"type": "text", "text": "\n\n"})
        elements.append({"type": "text", "text": e.quote[:EVIDENCE_QUOTE_CHARS]})
        elements.append(
            {"type": "text", "text": f"\n{_note_label(e.note)} L{e.line}", "style": {"code": True}}
        )
    return {"type": "rich_text", "elements": [{"type": "rich_text_quote", "elements": elements}]}


def _proposal_row(
    idx: int,
    total: int,
    proposal: Proposal,
    verdict: ButtonVerdict | None,
    strings: dict[str, str],
    register_labels: dict[str, str],
    *,
    action_idx: int,
) -> list[dict]:
    """`idx`/`total` are the display position within the advice lane (unchanged by the repair
    lane's presence); `action_idx` is the shared button-idx space record_verdict reads —
    n_repairs + idx once a repair lane exists, idx alone otherwise."""
    row = [
        {"type": "divider"},
        _tag_block(idx, total, proposal, register_labels),
        _section_block(proposal),
        _quote_block(proposal.evidence),
    ]
    row.append(
        _verdict_block(verdict, strings)
        if verdict is not None
        else _actions_block(action_idx, proposal, strings)
    )
    return row


def _confirmation_block(confirmation: Confirmation, strings: dict[str, str]) -> dict:
    text = strings["confirmation_line"].format(
        total=confirmation.total, done=len(confirmation.done), pending=len(confirmation.pending)
    )
    if confirmation.unknown:
        text += strings["confirmation_unknown"].format(unknown=len(confirmation.unknown))
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


#: The repair tag's icon — language-independent, like REGISTER_ICONS.
REPAIR_ICON = "🧩"


def _section_header_block(label: str) -> dict:
    """A lane label — the same "section with a leading full-width space" shape _project_groups
    already uses for a project's own label, reused here for the two lane headers."""
    return {"type": "section", "text": {"type": "mrkdwn", "text": f"　\n*{label}*"}}


def _repairs_headline_block(
    total_groups: int, merged_yesterday_rows: int | None, strings: dict[str, str]
) -> dict:
    if merged_yesterday_rows is not None:
        text = strings["repairs_headline_with_merged"].format(
            remaining=total_groups, merged=merged_yesterday_rows
        )
    else:
        text = strings["repairs_headline"].format(remaining=total_groups)
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _repair_actions_block(idx: int, strings: dict[str, str]) -> dict:
    elements = []
    for choice in CHOICES:
        button = {
            "type": "button",
            "text": {"type": "plain_text", "text": strings[f"repair_button_{choice}"], "emoji": True},
            "action_id": f"card:{idx}:{choice}",
            "value": "",
        }
        if choice == "do":
            button["style"] = "primary"
        elif choice == "drop":
            button["style"] = "danger"
        elements.append(button)
    return {"type": "actions", "elements": elements}


def _repair_verdict_block(
    verdict: ButtonVerdict, result: RepairDone | RepairFailed | None, strings: dict[str, str]
) -> dict:
    """An adopted repair shows the door's own numbers once execute_repair answered — done or
    failed get their own marks (F2: a failed merge still names the rows it already
    committed, never a plain "✓ 채택" and never silence). Hold/reject, or adopt before the
    door has answered, fall back to the same verdict words the advice lane uses — 보류/거절
    mean the same thing in either lane."""
    if verdict.choice == "do" and isinstance(result, RepairDone):
        text = strings["repair_verdict_done"].format(deleted=result.deleted_rows, reread=result.reread_notes)
    elif verdict.choice == "do" and isinstance(result, RepairFailed):
        text = strings["repair_verdict_failed"].format(
            deleted=result.deleted_rows, reread=result.reread_notes, reason=result.reason
        )
    else:
        text = strings[f"verdict_{verdict.choice}"]
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _repair_tag_block(idx: int, total: int, repair: Repair, strings: dict[str, str]) -> dict:
    text = f"{REPAIR_ICON}  *{strings['repair_tag_label']}*  ·  `{repair.subject}`  ·  {idx + 1}/{total}"
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _repair_section_block(repair: Repair, strings: dict[str, str]) -> dict:
    body = strings["repair_body"].format(
        variant=repair.variants[0], rows=f"{repair.rows:,}", notes=repair.notes
    )
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*{strings['repair_section_title']}*\n{body}"},
    }


def _repair_row(
    idx: int,
    total: int,
    repair: Repair,
    verdict: ButtonVerdict | None,
    result: dict | None,
    strings: dict[str, str],
) -> list[dict]:
    row = [
        {"type": "divider"},
        _repair_tag_block(idx, total, repair, strings),
        _repair_section_block(repair, strings),
    ]
    row.append(
        _repair_verdict_block(verdict, result, strings)
        if verdict is not None
        else _repair_actions_block(idx, strings)
    )
    return row


def _review_tag_block(review: ProposedVerdict, strings: dict[str, str]) -> dict:
    text = f"{strings[f'review_kind_{review.kind}']} · {_note_label(review.note)}"
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _review_actions_block(idx: int, strings: dict[str, str]) -> dict:
    # Agree (do) or flip (drop) only — no hold: not pressing is the hold, and it records
    # nothing, so a hold button would promise a trace the run never leaves.
    elements = []
    for choice in ("do", "drop"):
        button = {
            "type": "button",
            "text": {"type": "plain_text", "text": strings[f"review_button_{choice}"], "emoji": True},
            "action_id": f"card:{idx}:{choice}",
            "value": "",
        }
        if choice == "do":
            button["style"] = "primary"
        elif choice == "drop":
            button["style"] = "danger"
        elements.append(button)
    return {"type": "actions", "elements": elements}


def _review_verdict_block(review: ProposedVerdict, verdict: ButtonVerdict, strings: dict[str, str]) -> dict:
    if verdict.choice == "do":
        text = strings["review_verdict_agree"]
    else:
        flipped = strings[f"review_kind_{'contested' if review.kind == 'used' else 'used'}"]
        text = strings["review_verdict_flip"].format(kind=flipped)
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _review_row(
    idx: int,
    review: ProposedVerdict,
    verdict: ButtonVerdict | None,
    strings: dict[str, str],
) -> list[dict]:
    row = [{"type": "divider"}, _review_tag_block(review, strings)]
    row.append(
        _review_verdict_block(review, verdict, strings)
        if verdict is not None
        else _review_actions_block(idx, strings)
    )
    return row


def _project_groups(proposals: list[Proposal]) -> list[tuple[str, list[int]]]:
    """proposals grouped by `.project`, each project's rows kept together under one label —
    in first-seen order, so a priority (아직) pick from project B ahead of project A's own
    picks still puts B's label first. Indices are into the original `proposals` list; the
    button `action_id`s (and so `record_verdict`'s `state["proposals"][verdict.idx]` lookup)
    must reference that list, never a position inside the display grouping."""
    order: list[str] = []
    groups: dict[str, list[int]] = {}
    for idx, proposal in enumerate(proposals):
        if proposal.project not in groups:
            groups[proposal.project] = []
            order.append(proposal.project)
        groups[proposal.project].append(idx)
    return [(project, groups[project]) for project in order]


def build_blocks(
    proposals: list[Proposal],
    verdicts: Iterable[ButtonVerdict] = (),
    confirmation: Confirmation | None = None,
    repairs: list[Repair] = (),
    repairs_total_groups: int = 0,
    merged_yesterday_rows: int | None = None,
    repair_results: dict[int, RepairDone | RepairFailed] | None = None,
    reviews: Iterable[ProposedVerdict] = (),
    *,
    lang: str,
) -> list[dict]:
    """Block Kit for the card: the head line (past approvals cross-checked against today's
    registers — present only when there were any), then a project label and one five-block
    row per proposal, proposals grouped under their project's own label (the unassigned
    bucket's name comes from card_i18n too). A judged row shows its mark instead of its
    buttons — the card is edited in place as verdicts arrive. Slack's 50-block cap on one
    message means a large enough proposal list cannot all be shown; rather than drop rows
    silently, this stops adding rows once the next one would not fit and says how many were
    left out in a final context line (AC8).

    A repair lane sits above the advice one when the caller has repair data to show
    (`repairs_total_groups > 0`, or `repairs` itself non-empty, or a merge happened
    yesterday) — every existing single-lane caller leaves these at their defaults and
    renders exactly as before. When shown: a head-line context block, then, only if
    `repairs` itself is non-empty, a 「오늘 할 일」 label and one four-block row per repair,
    then a 「짚어 둔 것」 label before the advice rows. Repair rows occupy button idx
    0..len(repairs)-1; advice rows continue from there — one shared space, repairs first.
    A review lane sits below the advice lane when `reviews` is non-empty — the agent's own
    session-end classifications, one three-block row each, agree/flip buttons occupying the
    idx slots after the advice rows. It shares the 50-block cap: the advice lane reserves
    the review lane's tail, and review rows that still do not fit are reported in an
    overflow line, not dropped. Empty `reviews` leaves the card exactly as it was —
    a zero-proposal morning must not change the card's shape."""
    strings = card_i18n.STRINGS[lang]
    register_labels = card_i18n.REGISTER_LABELS[lang]
    repair_results = repair_results or {}
    by_idx = {v.idx: v for v in verdicts}
    total = len(proposals)
    n_repairs = len(repairs)
    show_lanes = repairs_total_groups > 0 or n_repairs > 0 or merged_yesterday_rows is not None
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{strings['card_title']} {total}", "emoji": True},
        }
    ]
    if show_lanes:
        blocks.append(_repairs_headline_block(repairs_total_groups, merged_yesterday_rows, strings))
    if confirmation is not None and confirmation.total > 0:
        blocks.append(_confirmation_block(confirmation, strings))

    if show_lanes:
        if repairs:
            blocks.append(_section_header_block(strings["todo_header"]))
            for ridx, repair in enumerate(repairs):
                blocks.extend(
                    _repair_row(ridx, n_repairs, repair, by_idx.get(ridx), repair_results.get(ridx), strings)
                )
        blocks.append(_section_header_block(strings["advice_header"]))

    shown = 0
    overflowed = False
    reviews = list(reviews)
    # the review lane below must fit inside the same 50-block cap: the advice loop
    # reserves its whole tail (header + one 3-block row per review), and the review loop
    # reserves its own overflow line — an unshown row is counted there, never dropped
    # silently. A card that filled all 50 blocks with advice rows used to push the
    # review header past the cap.
    review_tail = (1 + 3 * len(reviews)) if reviews else 0
    for project, indices in _project_groups(proposals):
        label = project if project else strings["unassigned_project"]
        label_block = {"type": "section", "text": {"type": "mrkdwn", "text": f"　\n*{label}*"}}
        label_pending = True
        for idx in indices:
            proposal = proposals[idx]
            row = _proposal_row(
                idx,
                total,
                proposal,
                by_idx.get(n_repairs + idx),
                strings,
                register_labels,
                action_idx=n_repairs + idx,
            )
            addition = ([label_block] if label_pending else []) + row
            remaining_after = total - shown - 1
            reserve = (1 if remaining_after > 0 else 0) + review_tail
            if len(blocks) + len(addition) + reserve > BLOCK_LIMIT:
                overflowed = True
                break
            blocks.extend(addition)
            label_pending = False
            shown += 1
        if overflowed:
            break

    if overflowed:
        remaining = total - shown
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": strings["overflow_line"].format(n=remaining)}],
            }
        )

    if reviews:
        # the review lane sits below the advice one — the agent's own session-end calls,
        # which the owner may agree with or flip. No reviews, no header: a lane the agent
        # never filled must not render as an empty promise.
        blocks.append(_section_header_block(strings["review_header"]))
        n_slots = n_repairs + total
        r_shown = 0
        r_overflowed = False
        for ridx, review in enumerate(reviews):
            addition = _review_row(n_slots + ridx, review, by_idx.get(n_slots + ridx), strings)
            remaining_after = len(reviews) - ridx - 1
            reserve = 1 if remaining_after > 0 else 0
            if len(blocks) + len(addition) + reserve > BLOCK_LIMIT:
                r_overflowed = True
                break
            blocks.extend(addition)
            r_shown += 1
        if r_overflowed:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": strings["overflow_line"].format(n=len(reviews) - r_shown),
                        }
                    ],
                }
            )
    return blocks
