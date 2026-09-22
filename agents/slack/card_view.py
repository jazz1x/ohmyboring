#!/usr/bin/env python3
"""The morning card's Block Kit layout — no Slack client, no LLM, no engine.

build_blocks is the only entry point: proposals in (grouped by project), a Block Kit list
out, judged rows showing their mark instead of buttons. Every display string comes from
card_i18n.STRINGS, keyed by the language the caller passes — no literal display text lives
in this file's own code."""

from __future__ import annotations

from collections.abc import Iterable

import card_i18n
from card_types import CHOICES, ButtonVerdict, Confirmation, Evidence, Proposal


def _actions_block(idx: int, proposal: Proposal, strings: dict[str, str]) -> dict:
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": strings[f"button_{choice}"], "emoji": True},
                "action_id": f"card:{idx}:{choice}",
                "value": proposal.note,
            }
            for choice in CHOICES
        ],
    }


#: How much of a cited quote shows on the card — the note+line is the coordinate, the quote
#: is just enough for the owner to recognize it without opening the note.
EVIDENCE_QUOTE_CHARS = 60
#: Evidence lines shown per proposal — a candidate may ground on more, the card shows the head.
EVIDENCE_LINES_SHOWN = 2


def _note_label(note: str) -> str:
    """`/vault/wiki/wiki-0576.md` → `wiki-0576` — the short form the owner already reads."""
    base = note.rsplit("/", 1)[-1]
    return base[:-3] if base.endswith(".md") else base


def _evidence_block(evidence: list[Evidence]) -> dict:
    lines = [
        f"{_note_label(e.note)} L{e.line} 「{e.quote[:EVIDENCE_QUOTE_CHARS]}」"
        for e in evidence[:EVIDENCE_LINES_SHOWN]
    ]
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": "\n".join(lines)}]}


def _confirmation_block(confirmation: Confirmation, strings: dict[str, str]) -> dict:
    text = strings["confirmation_line"].format(
        total=confirmation.total, done=len(confirmation.done), pending=len(confirmation.pending)
    )
    if confirmation.unknown:
        text += strings["confirmation_unknown"].format(unknown=len(confirmation.unknown))
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _project_groups(proposals: list[Proposal]) -> list[tuple[str, list[int]]]:
    """proposals grouped by `.project`, each project's rows kept together under one header —
    in first-seen order, so a priority (아직) pick from project B ahead of project A's own
    picks still puts B's header first. Indices are into the original `proposals` list; the
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
    registers — present only when there were any), then one header and one section+button
    row per proposal, proposals grouped under their project's own header (the unassigned
    bucket's name comes from card_i18n too). A judged row shows its mark instead of its
    buttons — the card is edited in place as 판정들 arrive. Buttons carry the note path: a
    verdict attaches to the note."""
    strings = card_i18n.STRINGS[lang]
    by_idx = {v.idx: v for v in verdicts}
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{strings['card_title']} {len(proposals)}",
                "emoji": True,
            },
        }
    ]
    if confirmation is not None and confirmation.total > 0:
        blocks.append(_confirmation_block(confirmation, strings))
    for project, indices in _project_groups(proposals):
        label = project if project else strings["unassigned_project"]
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": label, "emoji": False}})
        for idx in indices:
            proposal = proposals[idx]
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*{idx + 1}. {proposal.advice}*\n"
                            f"{strings['bottleneck_label']}: {proposal.bottleneck}\n"
                            f"_{proposal.register_} · {proposal.subject}_"
                        ),
                    },
                }
            )
            blocks.append(_evidence_block(proposal.evidence))
            verdict = by_idx.get(idx)
            if verdict is None:
                blocks.append(_actions_block(idx, proposal, strings))
            else:
                mark = strings[f"verdict_{verdict.choice}"]
                blocks.append(
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": f"{mark} — <@{verdict.user}>"}],
                    }
                )
    return blocks
