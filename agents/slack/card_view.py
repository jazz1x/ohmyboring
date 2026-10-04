#!/usr/bin/env python3
"""The morning card's Block Kit layout (v2) — no Slack client, no LLM, no engine.

build_blocks is the only entry point: proposals in (grouped by project), a Block Kit list
out, judged rows showing their mark instead of buttons. Every display string comes from
card_i18n.STRINGS/REGISTER_LABELS, keyed by the language the caller passes — no literal
display text lives in this file's own code. One proposal is five blocks: divider, a
register-tag context line (glyph · register · project · `wiki-NNNN` · k/N — the project
name rides in the tag, and the unassigned bucket shows no project segment), a section
(advice + bottleneck), a top-level rich_text quote (Slack's context blocks cannot carry
rich text), and an actions row or — once judged — a context line with the verdict mark.
There are no per-project headings: grouping shows a project's rows together, never a
same-size title above them. No `/vault/wiki/...` path appears anywhere in the output; only
the short `wiki-NNNN` form does. Slack caps one message at 50 blocks: past that, this
stops adding proposal rows and reports how many were left out in a final context line
instead of silently dropping them.

When the caller passes repair data (`repairs`/`repairs_total_groups`/`merged_yesterday_rows`
— all optional, and off by default so every existing single-lane caller renders exactly as
before), the card grows a second lane above the advice one: a head-line context block
("remaining groups n · merged yesterday m"), then, if `repairs` itself is non-empty, an
「오늘 할 일」 header and one row per repair group (divider, tag, section, the agent's
판정 block when the group carries one, actions-or-mark), then a 「짚어 둔 것」 header
before the existing advice rows. Every lane
titles itself with the same Slack `header` block — the single prominent title shape — so
no lane title floats as a section among its own rows. A repair row's button idx shares one
space with the advice rows — repairs first, 0..k-1 — so a button press can tell the two
lanes apart by idx alone."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from typing import assert_never
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "src"))

from card_types import (  # noqa: E402
    CHOICES,
    ButtonVerdict,
    Confirmation,
    Done,
    Evidence,
    Failed,
    Outcome,
    Pending,
    Press,
    Proposal,
    ProposedVerdict,
    Rejected,
    Repair,
    RepairDone,
    RepairFailed,
    RepairJudgment,
    RepairPress,
    RepairUnanswered,
    ReviewPress,
)

from ohmyboring.config import FileLink, NoteLink, ObsidianLink  # noqa: E402
from ohmyboring.i18n import card as card_i18n  # noqa: E402

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


def note_label(note: str) -> str:
    """`/vault/wiki/wiki-0576.md` → `wiki-0576` — the short form the owner already reads,
    and the only form of the note path this module ever puts in a block (AC7)."""
    base = note.rsplit("/", 1)[-1]
    return base[:-3] if base.endswith(".md") else base


def note_path(label: str) -> str:
    """`wiki-0576` → `/vault/wiki/wiki-0576.md` — note_label's inverse. Only flat
    `/vault/wiki/<name>.md` notes round-trip (the build refuses anything else), so a label
    always expands under `/vault/wiki/` — no path can sneak out through it."""
    return f"/vault/wiki/{label}.md"


#: Slack's own hard cap on a button `value`'s length.
VALUE_CHAR_LIMIT = 2000


def _button_value(data: dict) -> str:
    """One button value shape — compact `{lane, …}` JSON naming the lane and what that
    lane needs. Slack refuses an over-long value, so an over-limit card must not ship:
    a card that cannot be answered is exactly the card to refuse."""
    value = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    if len(value) > VALUE_CHAR_LIMIT:
        raise ValueError(f"button value over {VALUE_CHAR_LIMIT} chars")
    return value


def _lane_value(lane: str, data: dict, *, note: str | None = None) -> str:
    """Lane name + the lane's own data as a button value. A note must be a flat
    `/vault/wiki/<name>.md` — a subfolder or a `.md`-less name cannot round-trip through
    the label, so the card is refused instead of shipping a button it cannot answer."""
    if note is not None:
        name = note.removeprefix("/vault/wiki/")
        if name == note or "/" in name or not name.endswith(".md") or name == ".md":
            raise ValueError(f"note {note!r} is not a flat /vault/wiki/<name>.md note")
        data = {**data, "note": note_label(note)}
    return _button_value({"lane": lane, **data})


def _actions_block(idx: int, proposal: Proposal, strings: dict[str, str]) -> dict:
    elements = []
    for choice in CHOICES:
        button = {
            "type": "button",
            "text": {"type": "plain_text", "text": strings[f"button_{choice}"], "emoji": True},
            "action_id": f"card:{idx}:{choice}",
            "value": _lane_value("advice", {}, note=proposal.note),
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


def _note_link_url(label: str, link: NoteLink) -> str:
    match link:
        case ObsidianLink(vault=vault, folder=folder):
            target = f"{folder}/{label}" if folder else label
            return f"obsidian://open?vault={quote(vault, safe='')}&file={quote(target, safe='')}"
        case FileLink(folder=folder, editor=editor):
            return f"{quote(editor, safe='')}://file{quote(f'{folder}/{label}.md', safe='/')}"


def note_links_text(label: str, links: tuple[NoteLink, ...], strings: dict[str, str]) -> str:
    """` · <obsidian://…|Obsidian 으로 열기> · <file://…|파일로 열기>` for the configured links,
    or "" when boring.json names none."""
    words = {ObsidianLink: strings["note_link_obsidian"], FileLink: strings["note_link_file"]}
    return "".join(f" · <{_note_link_url(label, link)}|{words[type(link)]}>" for link in links)


def _tag_block(
    idx: int,
    total: int,
    proposal: Proposal,
    register_labels: dict[str, str],
    links_text: str = "",
) -> dict:
    icon = REGISTER_ICONS[proposal.register_]
    label = register_labels[proposal.register_]
    note = note_label(proposal.note)
    parts = [f"{icon} *{label}*"]
    if proposal.project:
        parts.append(proposal.project)
    parts.extend([f"`{note}`{links_text}", f"{idx + 1}/{total}"])
    text = " · ".join(parts)
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _section_block(proposal: Proposal) -> dict:
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*{proposal.advice}*\n{proposal.bottleneck}"},
    }


def _evidence_label(e: Evidence, strings: dict[str, str]) -> str:
    superseded = f" · {strings['superseded_label']} → {', '.join(e.superseded_by)}" if e.superseded_by else ""
    return f"\n{note_label(e.note)} L{e.line}{superseded}"


def _quote_block(evidence: list[Evidence], strings: dict[str, str]) -> dict:
    """A top-level `rich_text` block holding one `rich_text_quote` — Slack's `context` blocks
    only take mrkdwn/plain_text, never rich text, so the evidence quote cannot live there
    (AC4). Two or more pieces of evidence share one quote block, a blank line between them."""
    elements: list[dict] = []
    for i, e in enumerate(evidence[:EVIDENCE_LINES_SHOWN]):
        if i:
            elements.append({"type": "text", "text": "\n\n"})
        elements.append({"type": "text", "text": e.quote[:EVIDENCE_QUOTE_CHARS]})
        elements.append({"type": "text", "text": _evidence_label(e, strings), "style": {"code": True}})
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
    note_links: tuple[NoteLink, ...] = (),
) -> list[dict]:
    """`idx`/`total` are the display position within the advice lane (unchanged by the repair
    lane's presence); `action_idx` is the shared button-idx space a press reads —
    n_repairs + idx once a repair lane exists, idx alone otherwise."""
    links_text = note_links_text(note_label(proposal.note), note_links, strings)
    row = [
        {"type": "divider"},
        _tag_block(idx, total, proposal, register_labels, links_text),
        _section_block(proposal),
        _quote_block(proposal.evidence, strings),
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


def _lane_header_block(label: str) -> dict:
    """A lane title — Slack's `header` block, the one prominent title shape Block Kit has.
    Every lane (오늘 할 일 / 짚어 둔 것 / 에이전트가 가른 것) titles itself with it, so a
    lane title never stands as a same-size section among its own rows (the v1 look the
    owner read as a label floating alone)."""
    return {"type": "header", "text": {"type": "plain_text", "text": label, "emoji": True}}


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


def _repair_actions_block(idx: int, repair: Repair, strings: dict[str, str]) -> dict:
    elements = []
    for choice in CHOICES:
        button = {
            "type": "button",
            "text": {"type": "plain_text", "text": strings[f"repair_button_{choice}"], "emoji": True},
            "action_id": f"card:{idx}:{choice}",
            "value": _lane_value("repair", {"subject": repair.subject}),
        }
        if choice == "do":
            button["style"] = "primary"
        elif choice == "drop":
            button["style"] = "danger"
        elements.append(button)
    return {"type": "actions", "elements": elements}


def _repair_verdict_block(
    verdict: ButtonVerdict,
    result: RepairDone | RepairFailed | RepairUnanswered | None,
    strings: dict[str, str],
) -> dict:
    """An adopted repair shows the door's own numbers once execute_repair answered — done or
    failed get their own marks (F2: a failed merge still names the rows it already
    committed, never a plain "✓ 채택" and never silence). An unanswered merge names the
    reason and no count — the rows may already be gone, so 0 would be a lie. Hold/reject,
    or adopt before the door has answered, fall back to the same verdict words the advice
    lane uses — 보류/거절 mean the same thing in either lane."""
    if verdict.choice == "do" and isinstance(result, RepairDone):
        text = strings["repair_verdict_done"].format(
            deleted=result.deleted_rows, reread=result.reread_notes
        ) + _owner_held_suffix(result.owner_held, strings)
    elif verdict.choice == "do" and isinstance(result, RepairFailed):
        text = strings["repair_verdict_failed"].format(
            deleted=result.deleted_rows, reread=result.reread_notes, reason=result.reason
        ) + _owner_held_suffix(result.owner_held, strings)
    elif verdict.choice == "do" and isinstance(result, RepairUnanswered):
        text = strings["repair_verdict_unanswered"].format(reason=result.reason)
    else:
        text = strings[f"verdict_{verdict.choice}"]
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _owner_held_suffix(held: list[str], strings: dict[str, str]) -> str:
    notes = ", ".join(f"`{path}`" for path in held)
    return strings["repair_owner_held"].format(n=len(held), notes=notes) if held else ""


def _repair_tag_block(idx: int, total: int, repair: Repair, strings: dict[str, str]) -> dict:
    text = f"{REPAIR_ICON} *{strings['repair_tag_label']}* · `{repair.subject}` · {idx + 1}/{total}"
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _repair_section_block(repair: Repair, strings: dict[str, str]) -> dict:
    body = strings["repair_body"].format(
        variant=repair.variants[0], rows=f"{repair.rows:,}", notes=repair.notes, subject=repair.subject
    )
    return {"type": "section", "text": {"type": "mrkdwn", "text": body}}


REVIEW_TEXT_MAX = 200

#: Slack rejects a card past a total size it does not publish (msg_blocks_too_long). Measured
#: 2026-10-04 on blocks JSON (ensure_ascii=False): 9,445 chars posted, 9,545 refused.
CARD_CHARS_BUDGET = 9000

#: Cut steps for the free-text lines (reasons, titles, works) when a card is over budget —
#: rows and buttons stay, only their prose shortens.
TEXT_MAX_STEPS = (REVIEW_TEXT_MAX, 120, 80, 40)


def _repair_judgment_block(judgment: RepairJudgment, strings: dict[str, str], text_max: int) -> dict:
    """The agent's 판정 on this row — what the repair-judge run left before the card drew:
    verdict label(같은 이름·못 가름) + 이유 한 줄 + 철자 목록 전부. generic 판정은 카드에
    오르지 않으니 여기 그릴 일이 없다."""
    label = strings[f"repair_judgment_{judgment.verdict}"]
    variants = ", ".join(f"`{v}`" for v in judgment.variants)
    text = "\n".join(
        (
            strings["repair_judgment_line"].format(
                verdict=label, reason=_mrkdwn_plain(judgment.reason, text_max)
            ),
            strings["repair_variants_line"].format(variants=variants),
        )
    )
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _repair_row(
    idx: int,
    total: int,
    repair: Repair,
    verdict: ButtonVerdict | None,
    result: dict | None,
    strings: dict[str, str],
    text_max: int = REVIEW_TEXT_MAX,
) -> list[dict]:
    row = [
        {"type": "divider"},
        _repair_tag_block(idx, total, repair, strings),
        _repair_section_block(repair, strings),
    ]
    if repair.judgment is not None:
        row.append(_repair_judgment_block(repair.judgment, strings, text_max))
    row.append(
        _repair_verdict_block(verdict, result, strings)
        if verdict is not None
        else _repair_actions_block(idx, repair, strings)
    )
    return row


def _mrkdwn_plain(text: str, limit: int = REVIEW_TEXT_MAX) -> str:
    """A vault string shown as-is in mrkdwn, cut to `limit` — one row must stay well under
    Slack's 3,000-char section limit, and the whole card under CARD_CHARS_BUDGET."""
    cut = text if len(text) <= limit else text[: limit - 1] + "…"
    return cut.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _review_work_line(
    review: ProposedVerdict, strings: dict[str, str], text_max: int = REVIEW_TEXT_MAX
) -> str:
    """One row's 작업 line. A grouped row names every grouped work (smaller ones inline);
    past the second, the count folds into 「외 N건」 — the press still reaches every grouped
    session, so nothing waits for the next card unseen."""
    sessions = review.sessions or [review.session_id]
    works = review.works or [review.work]
    if len(sessions) == 1 and not works[0]:
        return strings["review_work_unknown"].format(session=sessions[0][:8])
    parts = []
    for session, work in zip(sessions, works):
        parts.append(
            _mrkdwn_plain(work, text_max)
            if work
            else strings["review_work_session"].format(session=session[:8])
        )
    if len(parts) > 2:
        parts = [*parts[:2], strings["review_work_more"].format(n=len(sessions) - 2)]
    return strings["review_work"].format(work="; ".join(parts))


def _review_tag_block(
    review: ProposedVerdict,
    strings: dict[str, str],
    note_links: tuple[NoteLink, ...] = (),
    text_max: int = REVIEW_TEXT_MAX,
) -> dict:
    """What the agent judged, why, on which note, from which piece of work — ids stand in
    when the vault has no title or no session note. `reason` is the sentence the session-end
    scorer caught the mark in; a row proposed before reasons were stored shows the honest
    근거 없음 line instead of inventing one."""
    label = note_label(review.note)
    note_line = (
        strings["review_note_titled"].format(title=_mrkdwn_plain(review.note_title, text_max), note=label)
        if review.note_title
        else strings["review_note_bare"].format(note=label)
    ) + note_links_text(label, note_links, strings)
    reason_line = (
        strings["review_reason"].format(reason=_mrkdwn_plain(review.reason, text_max))
        if review.reason
        else strings["review_reason_none"]
    )
    work_line = _review_work_line(review, strings, text_max)
    text = "\n".join((strings[f"review_judged_{review.kind}"], reason_line, note_line, work_line))
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _review_actions_block(idx: int, review: ProposedVerdict, strings: dict[str, str]) -> dict:
    # 맞아요/아니에요는 오너 판정, 맡길게요는 에이전트 판정을 그대로 받는 맡긴 판정,
    # 보류는 판정 없이 사건 한 줄 — 넷 다 묶인 세션 전부에 닿는다. 안 누르는 것도 여전히
    # 아무 흔적 없는 보류다.
    data = {"session": review.session_id, "kind": review.kind}
    if review.sessions:
        data["sessions"] = list(review.sessions)
    elements = []
    for choice in ("do", "drop", "delegate", "defer"):
        button = {
            "type": "button",
            "text": {"type": "plain_text", "text": strings[f"review_button_{choice}"], "emoji": True},
            "action_id": f"card:{idx}:{choice}",
            "value": _lane_value("review", data, note=review.note),
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
    elif verdict.choice == "delegate":
        text = strings["review_verdict_delegate"]
    elif verdict.choice == "defer":
        text = strings["review_verdict_defer"]
    else:
        flipped = strings[f"review_kind_{'contested' if review.kind == 'used' else 'used'}"]
        text = strings["review_verdict_flip"].format(kind=flipped)
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _review_row(
    idx: int,
    review: ProposedVerdict,
    verdict: ButtonVerdict | None,
    strings: dict[str, str],
    note_links: tuple[NoteLink, ...] = (),
    text_max: int = REVIEW_TEXT_MAX,
) -> list[dict]:
    row = [{"type": "divider"}, _review_tag_block(review, strings, note_links, text_max)]
    row.append(
        _review_verdict_block(review, verdict, strings)
        if verdict is not None
        else _review_actions_block(idx, review, strings)
    )
    return row


def mark_pressed(
    blocks: list[dict],
    press: Press,
    *,
    lang: str,
    repair_result: RepairDone | RepairFailed | RepairUnanswered | None = None,
) -> list[dict] | Rejected:
    """The posted card's blocks with `press`'s row rendered exactly as build_blocks renders
    it once that verdict exists — the row's actions block becomes the judged block (the
    repair lane's carries the door's answer when there is one), every other block untouched.
    The row is found by its buttons' `card:{idx}:*` action_ids, never by position: project
    grouping can display rows out of idx order, and the payload's blocks are all a press
    carries. When that actions block is not there — the row was already judged, or these
    blocks are not this card — the press is refused with a Rejected value, never an
    exception. build_blocks has no text that counts judged rows (the header counts
    proposals, the head line counts repair groups), so the parity is exact: this equals
    build_blocks(..., this one verdict) for the rows, byte for byte everywhere else."""
    strings = card_i18n.STRINGS[lang]
    verdict = ButtonVerdict(idx=press.idx, choice=press.choice, user=press.user, at="")
    prefix = f"card:{press.idx}:"
    out = list(blocks)
    for i, block in enumerate(out):
        if block.get("type") != "actions":
            continue
        if not any(
            isinstance(el, dict)
            and isinstance(el.get("action_id"), str)
            and el["action_id"].startswith(prefix)
            for el in block.get("elements", [])
        ):
            continue
        if isinstance(press, RepairPress):
            out[i] = _repair_verdict_block(verdict, repair_result, strings)
        elif isinstance(press, ReviewPress):
            out[i] = _review_verdict_block(press, verdict, strings)
        else:
            out[i] = _verdict_block(verdict, strings)
        return out
    return Rejected(reason=f"row {press.idx} is not pressable")


def _status_id(idx: int) -> str:
    return f"card:{idx}:status"


def status_text(outcome: Outcome, strings: dict[str, str]) -> str:
    match outcome:
        case Pending():
            return strings["progress_pending"]
        case Done(text=text):
            return text
        case Failed(reason=reason):
            return strings["progress_failed"].format(reason=reason)
        case _:
            assert_never(outcome)


def _is_row_actions(block: dict, idx: int) -> bool:
    prefix = f"card:{idx}:"
    return block.get("type") == "actions" and any(
        isinstance(el, dict) and isinstance(el.get("action_id"), str) and el["action_id"].startswith(prefix)
        for el in block.get("elements", [])
    )


def _row_at(blocks: list[dict], idx: int) -> int | None:
    return next(
        (
            i
            for i, block in enumerate(blocks)
            if block.get("block_id") == _status_id(idx) or _is_row_actions(block, idx)
        ),
        None,
    )


def _status_instead(blocks: list[dict], idx: int, status: dict) -> list[dict] | Rejected:
    """The row's buttons become the status line; an earlier status line of the row goes."""
    kept = [block for block in blocks if block.get("block_id") != _status_id(idx)]
    at = next((i for i, block in enumerate(kept) if _is_row_actions(block, idx)), None)
    if at is not None:
        return [*kept[:at], status, *kept[at + 1 :]]
    at = _row_at(blocks, idx)
    if at is None:
        return Rejected(reason=f"row {idx} has no buttons or status to replace")
    return [*blocks[:at], status, *blocks[at + 1 :]]


def _status_above_buttons(blocks: list[dict], idx: int, status: dict) -> list[dict] | Rejected:
    """A failure keeps the buttons so the owner can press again; the reason sits above them."""
    kept = [block for block in blocks if block.get("block_id") != _status_id(idx)]
    at = next((i for i, block in enumerate(kept) if _is_row_actions(block, idx)), None)
    if at is None:
        return _status_instead(blocks, idx, status)
    return [*kept[:at], status, *kept[at:]]


def _is_row_block(block: dict, idx: int) -> bool:
    return block.get("block_id") == _status_id(idx) or _is_row_actions(block, idx)


def _status_block(idx: int, outcome: Outcome, lang: str) -> dict:
    return {
        "type": "context",
        "block_id": _status_id(idx),
        "elements": [{"type": "mrkdwn", "text": status_text(outcome, card_i18n.STRINGS[lang])}],
    }


def row_progress(blocks: list[dict], idx: int, outcome: Outcome, *, lang: str) -> list[dict] | Rejected:
    """Row `idx`'s own blocks once `outcome` shows: the status line alone, or — for a failure —
    the status line above the row's buttons taken from `blocks`, so the owner can press again."""
    actions = next((block for block in blocks if _is_row_actions(block, idx)), None)
    if actions is None and not any(_is_row_block(block, idx) for block in blocks):
        return Rejected(reason=f"row {idx} has no buttons or status to replace")
    status = _status_block(idx, outcome, lang)
    match outcome:
        case Failed() if actions is not None:
            return [status, actions]
        case _:
            return [status]


def row_pressed(
    blocks: list[dict],
    press: Press,
    *,
    lang: str,
    repair_result: RepairDone | RepairFailed | RepairUnanswered | None = None,
) -> list[dict] | Rejected:
    """Row `press.idx`'s judged block alone, as mark_pressed renders it in place of the buttons."""
    marked = mark_pressed(blocks, press, lang=lang, repair_result=repair_result)
    match marked:
        case Rejected():
            return marked
    at = next(i for i, block in enumerate(blocks) if _is_row_actions(block, press.idx))
    return [marked[at]]


def replace_row(current: list[dict], idx: int, segment: list[dict]) -> list[dict] | Rejected:
    """`current` — the message as Slack holds it now — with row `idx`'s blocks (its status
    line, its buttons, or both) swapped for `segment`; every other row stays as it is now, so
    a row another writer settled meanwhile is not rolled back to a stale snapshot."""
    positions = [i for i, block in enumerate(current) if _is_row_block(block, idx)]
    if not positions:
        return Rejected(reason=f"row {idx} is not on the card any more")
    kept = [block for i, block in enumerate(current) if i not in positions]
    at = positions[0]
    return [*kept[:at], *segment, *kept[at:]]


def mark_progress(blocks: list[dict], idx: int, outcome: Outcome, *, lang: str) -> list[dict] | Rejected:
    """Only row `idx` changes, and its status line's block_id names the row so a later writer
    can find it again without knowing the buttons."""
    status = _status_block(idx, outcome, lang)
    match outcome:
        case Failed():
            return _status_above_buttons(blocks, idx, status)
        case _:
            return _status_instead(blocks, idx, status)


def merged_outcome(done: RepairDone, *, lang: str) -> Done:
    strings = card_i18n.STRINGS[lang]
    text = strings["progress_merged"].format(
        deleted=done.deleted_rows, reread=done.reread_notes
    ) + _owner_held_suffix(done.owner_held, strings)
    return Done(text=text)


def reread_failed_outcome(error: object, *, lang: str) -> Failed:
    return Failed(reason=card_i18n.STRINGS[lang]["reread_failed_reason"].format(error=error))


def _project_groups(proposals: list[Proposal]) -> list[tuple[str, list[int]]]:
    """proposals grouped by `.project`, each project's rows kept together in display order —
    in first-seen order, so a priority (아직) pick from project B ahead of project A's own
    picks still puts B's rows first. The project name rides in each row's tag line, never in
    a heading of its own. Indices are into the original `proposals` list; the button
    `action_id`s (and so a press's lane lookup by idx) must reference that list, never
    a position inside the display grouping."""
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
    repair_results: dict[int, RepairDone | RepairFailed | RepairUnanswered] | None = None,
    reviews: Iterable[ProposedVerdict] = (),
    *,
    lang: str,
    note_links: tuple[NoteLink, ...] = (),
    text_max: int = REVIEW_TEXT_MAX,
    block_limit: int = BLOCK_LIMIT,
) -> list[dict]:
    """Block Kit for the card: the head line (past approvals cross-checked against today's
    registers — present only when there were any), then one five-block row per proposal,
    proposals grouped by project in first-seen order with the project name in each row's
    tag (the unassigned bucket shows no project segment, and no per-project heading exists).
    A judged row shows its mark instead of its buttons — the card is edited in place as
    verdicts arrive. Slack's 50-block cap on one message means a large enough proposal list
    cannot all be shown; rather than drop rows silently, this stops adding rows once the
    next one would not fit and says how many were left out in a final context line (AC8).

    A repair lane sits above the advice one when the caller has repair data to show
    (`repairs_total_groups > 0`, or `repairs` itself non-empty, or a merge happened
    yesterday) — every existing single-lane caller leaves these at their defaults and
    renders exactly as before. When shown: a head-line context block, then, only if
    `repairs` itself is non-empty, an 「오늘 할 일」 header and one four-block row per repair,
    then a 「짚어 둔 것」 header before the advice rows. Repair rows occupy button idx
    0..len(repairs)-1; advice rows continue from there — one shared space, repairs first.
    A review lane sits below the advice lane when `reviews` is non-empty — the agent's own
    session-end classifications, one three-block row each (the same note judged the same way
    by several sessions rides as one grouped row), agree/flip/delegate/hold buttons occupying
    the idx slots after the advice rows, under the same 「에이전트가 가른 것」 header shape. It
    shares the 50-block cap: the advice lane reserves the review lane's tail, and review
    rows that still do not fit are reported in an overflow line, not dropped. Empty
    `reviews` leaves the card exactly as it was —
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
            blocks.append(_lane_header_block(strings["todo_header"]))
            for ridx, repair in enumerate(repairs):
                blocks.extend(
                    _repair_row(
                        ridx, n_repairs, repair, by_idx.get(ridx), repair_results.get(ridx), strings, text_max
                    )
                )
        blocks.append(_lane_header_block(strings["advice_header"]))

    shown = 0
    overflowed = False
    reviews = list(reviews)
    # the review lane below must fit inside the same 50-block cap: the advice loop
    # reserves its whole tail (header + one 3-block row per review), and the review loop
    # reserves its own overflow line — an unshown row is counted there, never dropped
    # silently. A card that filled all 50 blocks with advice rows used to push the
    # review header past the cap.
    review_tail = (1 + 3 * len(reviews)) if reviews else 0
    for _project, indices in _project_groups(proposals):
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
                note_links=note_links,
            )
            addition = row
            remaining_after = total - shown - 1
            reserve = (1 if remaining_after > 0 else 0) + review_tail
            if len(blocks) + len(addition) + reserve > block_limit:
                overflowed = True
                break
            blocks.extend(addition)
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
        # which the owner may agree with, flip, hand back, or hold. No reviews, no header:
        # a lane the agent never filled must not render as an empty promise.
        blocks.append(_lane_header_block(strings["review_header"]))
        n_slots = n_repairs + total
        r_shown = 0
        r_overflowed = False
        for ridx, review in enumerate(reviews):
            addition = _review_row(
                n_slots + ridx, review, by_idx.get(n_slots + ridx), strings, note_links, text_max
            )
            remaining_after = len(reviews) - ridx - 1
            reserve = 1 if remaining_after > 0 else 0
            if len(blocks) + len(addition) + reserve > block_limit:
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


def card_chars(blocks: list[dict]) -> int:
    return len(json.dumps(blocks, ensure_ascii=False))


def fit_blocks(*args, **kwargs) -> list[dict]:
    """build_blocks under CARD_CHARS_BUDGET: first shorten the prose, then lower the block cap
    so rows past it fall into the same overflow line the 50-block cap uses (counted, not
    dropped). A card that still does not fit raises — a refused post must say why."""
    for text_max in TEXT_MAX_STEPS:
        blocks = build_blocks(*args, text_max=text_max, **kwargs)
        if card_chars(blocks) <= CARD_CHARS_BUDGET:
            return blocks
    for block_limit in range(BLOCK_LIMIT - 1, 0, -1):
        blocks = build_blocks(*args, text_max=TEXT_MAX_STEPS[-1], block_limit=block_limit, **kwargs)
        if card_chars(blocks) <= CARD_CHARS_BUDGET:
            return blocks
    raise ValueError(
        f"card is {card_chars(blocks)} chars at the smallest cut and cap, over {CARD_CHARS_BUDGET}"
    )
