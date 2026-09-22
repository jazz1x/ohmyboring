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
line instead of silently dropping them."""

from __future__ import annotations

from collections.abc import Iterable

import card_i18n
from card_types import CHOICES, ButtonVerdict, Confirmation, Evidence, Proposal

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
    icon = REGISTER_ICONS.get(proposal.register_, "•")
    label = register_labels.get(proposal.register_, proposal.register_)
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
) -> list[dict]:
    row = [
        {"type": "divider"},
        _tag_block(idx, total, proposal, register_labels),
        _section_block(proposal),
        _quote_block(proposal.evidence),
    ]
    row.append(
        _verdict_block(verdict, strings) if verdict is not None else _actions_block(idx, proposal, strings)
    )
    return row


def _confirmation_block(confirmation: Confirmation, strings: dict[str, str]) -> dict:
    text = strings["confirmation_line"].format(
        total=confirmation.total, done=len(confirmation.done), pending=len(confirmation.pending)
    )
    if confirmation.unknown:
        text += strings["confirmation_unknown"].format(unknown=len(confirmation.unknown))
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


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
    *,
    lang: str,
) -> list[dict]:
    """Block Kit for the card: the head line (past approvals cross-checked against today's
    registers — present only when there were any), then a project label and one five-block
    row per proposal, proposals grouped under their project's own label (the unassigned
    bucket's name comes from card_i18n too). A judged row shows its mark instead of its
    buttons — the card is edited in place as 판정들 arrive. Slack's 50-block cap on one
    message means a large enough proposal list cannot all be shown; rather than drop rows
    silently, this stops adding rows once the next one would not fit and says how many were
    left out in a final context line (AC8)."""
    strings = card_i18n.STRINGS[lang]
    register_labels = card_i18n.REGISTER_LABELS[lang]
    by_idx = {v.idx: v for v in verdicts}
    total = len(proposals)
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{strings['card_title']} {total}", "emoji": True},
        }
    ]
    if confirmation is not None and confirmation.total > 0:
        blocks.append(_confirmation_block(confirmation, strings))

    shown = 0
    overflowed = False
    for project, indices in _project_groups(proposals):
        label = project if project else strings["unassigned_project"]
        label_block = {"type": "section", "text": {"type": "mrkdwn", "text": f"　\n*{label}*"}}
        label_pending = True
        for idx in indices:
            proposal = proposals[idx]
            row = _proposal_row(idx, total, proposal, by_idx.get(idx), strings, register_labels)
            addition = ([label_block] if label_pending else []) + row
            remaining_after = total - shown - 1
            reserve = 1 if remaining_after > 0 else 0
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
    return blocks
