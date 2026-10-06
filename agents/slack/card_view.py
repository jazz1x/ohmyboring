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
import re
import sys
from collections.abc import Iterable
from typing import NamedTuple, assert_never
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))

import label_core  # noqa: E402
from card_types import (  # noqa: E402
    CHOICES,
    COMMENT,
    COMMENT_ACTION_ID,
    COMMENT_BLOCK_ID,
    COMMENT_CALLBACK_ID,
    COMMENT_MAX_CHARS,
    MORE_ACTION_PREFIX,
    SAMPLE_CONFIRM,
    ButtonVerdict,
    CardRow,
    Confirmation,
    Done,
    Evidence,
    Failed,
    MoreSent,
    Outcome,
    Pending,
    Press,
    Proposal,
    ProposedVerdict,
    Received,
    Rejected,
    Repair,
    RepairDone,
    RepairFailed,
    RepairJudgment,
    RepairPress,
    RepairUnanswered,
    ReviewPress,
    Score,
    Scored,
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


def _lane_data(lane: str, data: dict, *, note: str | None = None) -> dict:
    """Lane name + the lane's own data — the value a row stands for. A note must be a flat
    `/vault/wiki/<name>.md` — a subfolder or a `.md`-less name cannot round-trip through
    the label, so the card is refused instead of shipping a button it cannot answer."""
    if note is not None:
        name = note.removeprefix("/vault/wiki/")
        if name == note or "/" in name or not name.endswith(".md") or name == ".md":
            raise ValueError(f"note {note!r} is not a flat /vault/wiki/<name>.md note")
        data = {**data, "note": note_label(note)}
    return {"lane": lane, **data}


def _lane_value(lane: str, data: dict, *, note: str | None = None) -> str:
    """A row's value as a button's `value` — the shape the advice and 표본 rows still carry."""
    return _button_value(_lane_data(lane, data, note=note))


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


def _repair_row_value(repair: Repair) -> dict:
    return _lane_data("repair", {"subject": repair.subject, "variants": list(repair.variants)})


def _bare_button(idx: int, choice: str, label: str) -> dict:
    """A button that carries nothing but its action_id — the row's values live in card_row,
    read back by (card_ts, idx)."""
    button = {
        "type": "button",
        "text": {"type": "plain_text", "text": label, "emoji": True},
        "action_id": f"card:{idx}:{choice}",
    }
    if choice == "do":
        button["style"] = "primary"
    elif choice == "drop":
        button["style"] = "danger"
    return button


def _repair_actions_block(idx: int, strings: dict[str, str]) -> dict:
    elements = [_bare_button(idx, choice, strings[f"repair_button_{choice}"]) for choice in CHOICES]
    elements.append(_bare_button(idx, COMMENT, strings["repair_button_comment"]))
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

#: The shortest a free-text line (title, work, evidence excerpt) is ever cut to. Past it the
#: card gives up rows, not words. Chosen on 2026-10-06's material — see the commit message.
TEXT_FLOOR = 70

#: Cut steps for the free-text lines when a card is over budget; the last is the floor.
TEXT_MAX_STEPS = (REVIEW_TEXT_MAX, 120, TEXT_FLOOR)


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
        else _repair_actions_block(idx, strings)
    )
    return row


def _mrkdwn_plain(text: str, limit: int = REVIEW_TEXT_MAX) -> str:
    """A vault string shown as-is in mrkdwn, cut to `limit` — one row must stay well under
    Slack's 3,000-char section limit, and the whole card under CARD_CHARS_BUDGET."""
    return _cut_words(text, limit).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cut_words(text: str, limit: int) -> str:
    """`text` within `limit` chars, ending on a word boundary when it has one to end on."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    on_boundary = text[limit - 1].isspace() or head[-1:].isspace()
    if not on_boundary and " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip() + "…"


_OPENERS = "([{"
_CLOSERS = ")]}"
_LEADING_JUNK = " `)]}.,;:·-—"


def _clipped_front(token: str) -> bool:
    """A first token that is the tail of a longer one: an odd backtick, or a closing bracket
    with nothing before it to close."""
    if token.count("`") % 2:
        return True
    depth = 0
    for ch in token:
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            if depth == 0:
                return True
            depth -= 1
    return False


def _unclosed_from(text: str) -> int:
    """Where a bracket or backtick that never closes begins — len(text) when none."""
    opened: list[int] = []
    tick: int | None = None
    for i, ch in enumerate(text):
        if ch == "`":
            tick = None if tick is not None else i
        elif ch in _OPENERS:
            opened.append(i)
        elif ch in _CLOSERS and opened:
            opened.pop()
    return min([*opened, *([tick] if tick is not None else [])], default=len(text))


def evidence_excerpt(reason: str) -> str:
    """The part of a stored reason sentence that reads as a quote: it begins on a whole word
    and ends on one. The scorer's sentence can start mid-token and ends clipped at its own
    cap ("…"), so the first token goes when it is a clipped tail, leading punctuation goes,
    the last word of a clipped sentence goes, and a bracket or backtick left open goes with
    everything after it."""
    text = " ".join(reason.split())
    clipped = text.endswith("…")
    text = text.removesuffix("…")
    first, sep, rest = text.partition(" ")
    if sep and _clipped_front(first):
        text = rest
    text = text.lstrip(_LEADING_JUNK)
    if clipped and " " in text:
        text = text.rsplit(" ", 1)[0]
    return text[: _unclosed_from(text)].rstrip(" ,;:·-—")


#: 「이」 is left out: standing alone at a sentence head it is the demonstrative (「이 노트는…」).
_PARTICLES = frozenset("은는가을를의에로와과도만")
_SENTENCE_END = re.compile(r"(?:[.!?。]|[다요죠까음함됨임])[\"'”’)\]」』.!?。]*$")


def _is_whole_sentence(reason: str, excerpt: str) -> bool:
    """The excerpt is the stored sentence itself and it reads as one: nothing was trimmed by
    evidence_excerpt, it does not open on a lowercase letter or a bare particle (the tail of
    a longer sentence), and it ends on a terminator or a Korean closing ending. Such a
    sentence is a quote at any length."""
    text = " ".join(reason.split())
    first = text.split(" ", 1)[0]
    opens_mid_sentence = first[:1].islower() and first[:1].isascii() or first in _PARTICLES
    return excerpt == text and not opens_mid_sentence and bool(_SENTENCE_END.search(text))


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
    *,
    show_work: bool = True,
) -> dict:
    """What the agent judged, why, on which note, from which piece of work — ids stand in
    when the vault has no title or no session note. The reason is a plain line per kind; the
    scorer's own sentence rides under it as a 근거 quote only when it still reads as one
    after evidence_excerpt (a whole sentence at any length, a clipped one only at TEXT_FLOOR
    chars or more). A row proposed before reasons were
    stored shows the honest 근거 없음 line instead of inventing one. `show_work` is off for
    a row that follows another of the same session — the head of the run says it once."""
    label = note_label(review.note)
    note_line = (
        strings["review_note_titled"].format(title=_mrkdwn_plain(review.note_title, text_max), note=label)
        if review.note_title
        else strings["review_note_bare"].format(note=label)
    ) + note_links_text(label, note_links, strings)
    reason_lines = [
        strings[f"review_reason_{review.kind}"] if review.reason else strings["review_reason_none"]
    ]
    quote = evidence_excerpt(review.reason)
    if quote and (_is_whole_sentence(review.reason, quote) or len(quote) >= TEXT_FLOOR):
        reason_lines.append(strings["review_evidence"].format(quote=_mrkdwn_plain(quote, text_max)))
    work_lines = [_review_work_line(review, strings, text_max)] if show_work else []
    text = "\n".join((strings[f"review_judged_{review.kind}"], *reason_lines, note_line, *work_lines))
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


#: 확인용 표본 줄의 버튼 — 오너 판정 둘뿐. 맡길게요는 오너 표본이 아니게 되고, 보류는
#: 안 누른 것과 같다.
SAMPLE_CHOICES = ("do", "drop")


def _review_actions_block(
    idx: int, review: ProposedVerdict, strings: dict[str, str], *, sample: bool = False
) -> dict:
    # 맞아요/아니에요는 오너 판정, 맡길게요는 에이전트 판정을 그대로 받는 맡긴 판정,
    # 보류는 판정 없이 사건 한 줄 — 넷 다 묶인 세션 전부에 닿는다. 안 누르는 것도 여전히
    # 아무 흔적 없는 보류다.
    if sample:
        # 표본 줄은 값을 자기 버튼에 그대로 들고, 코멘트 단추도 없다 — 오너 표본 계약은 그대로다.
        value = _button_value(_sample_row_value(review))
        elements = []
        for choice in SAMPLE_CHOICES:
            button = _bare_button(idx, choice, strings[f"review_button_{choice}"])
            button["value"] = value
            elements.append(button)
        return {"type": "actions", "elements": elements}
    elements = [
        _bare_button(idx, choice, strings[f"review_button_{choice}"])
        for choice in ("do", "drop", "delegate", "defer")
    ]
    elements.append(_bare_button(idx, COMMENT, strings["review_button_comment"]))
    return {"type": "actions", "elements": elements}


def _review_row_data(review: ProposedVerdict) -> dict:
    data = {"session": review.session_id, "kind": review.kind}
    if review.sessions:
        data["sessions"] = list(review.sessions)
    return data


def _review_row_value(review: ProposedVerdict) -> dict:
    return _lane_data("review", _review_row_data(review), note=review.note)


def _sample_row_value(review: ProposedVerdict) -> dict:
    return _lane_data("review", {**_review_row_data(review), "sample": SAMPLE_CONFIRM}, note=review.note)


def _advice_row_value(proposal: Proposal) -> dict:
    return _lane_data("advice", {}, note=proposal.note)


def _left_out_row(
    idx: int, lane: str, value: dict, item: Proposal | Repair | ProposedVerdict, pos: int, total: int
) -> CardRow:
    """A row the card left out, kept whole: the [더보기] page draws it from this alone.
    `pos`/`total` are where the row sat in its lane — the 「k/N」 tag."""
    return CardRow(
        idx=idx,
        lane=lane,
        value=value,
        shown=False,
        detail={"item": item.model_dump(by_alias=True), "pos": pos, "total": total},
    )


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
    *,
    sample: bool = False,
    show_work: bool = True,
) -> list[dict]:
    row = [
        {"type": "divider"},
        _review_tag_block(review, strings, note_links, text_max, show_work=show_work),
    ]
    row.append(
        _review_verdict_block(review, verdict, strings)
        if verdict is not None
        else _review_actions_block(idx, review, strings, sample=sample)
    )
    return row


def _works_once(rows: list[ProposedVerdict]) -> list[bool]:
    """Per row: does it say its work? A row of the same session(s) as the one before rides
    under that row's work line instead of repeating it."""
    keys = [tuple(r.sessions or [r.session_id]) for r in rows]
    return [i == 0 or keys[i] != keys[i - 1] for i in range(len(keys))]


def scoreline_text(score: Score, strings: dict[str, str]) -> str:
    """채점 줄 — 세션 끝 판정의 기계 대 오너 일치. 표본이 label_core.MIN_COMPARED 아래면 분자도
    비율도 쓰지 않는다(숫자가 근거처럼 읽히므로). 읽지 못했으면 읽지 못했다고 쓴다."""
    match score:
        case Scored(agreed=agreed, compared=compared):
            rate = label_core.agreement(agreed, compared)
            text = (
                strings["score_line_short"].format(compared=compared, min=label_core.MIN_COMPARED)
                if rate is None
                else strings["score_line"].format(
                    agreed=agreed,
                    compared=compared,
                    pct=round(rate * 100),
                    floor=round(label_core.AGREEMENT_FLOOR * 100),
                )
            )
        case _:
            text = strings["score_line_unreadable"]
    return text


def _scoreline_block(score: Score, strings: dict[str, str]) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": scoreline_text(score, strings)}]}


#: 확인용 표본 머리 블록의 이름 — 카드에 표본이 실렸는지 그리기 밖에서 읽는 표지.
SAMPLES_BLOCK_ID = "card:samples"

#: Samples the card keeps through every give-up: one. Naming is a call only the owner can make;
#: a sample is an optional press, so its second row is the first thing to go.
SAMPLES_KEPT = 1


def _samples_header_block(strings: dict[str, str]) -> dict:
    return {
        "type": "context",
        "block_id": SAMPLES_BLOCK_ID,
        "elements": [{"type": "mrkdwn", "text": f"*{strings['sample_header']}*"}],
    }


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
            return strings["progress_failed"].format(reason=_mrkdwn_plain(reason))
        case Received(head=head):
            return strings["comment_received"].format(head=head)
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
        case Failed() | Received() if actions is not None:
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
        case Failed() | Received():
            return _status_above_buttons(blocks, idx, status)
        case _:
            return _status_instead(blocks, idx, status)


#: How much of a comment the 받았어요 line quotes.
COMMENT_HEAD_CHARS = 30


def received_outcome(text: str) -> Received:
    """The 받았어요 mark for a comment: its first COMMENT_HEAD_CHARS characters on one line,
    mrkdwn-escaped, with 「…」 when there was more."""
    one_line = " ".join(text.split())
    head = (
        one_line[:COMMENT_HEAD_CHARS].rstrip().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return Received(head=head + ("…" if len(one_line) > COMMENT_HEAD_CHARS else ""))


def comment_modal(metadata: str, *, lang: str) -> dict:
    """The comment modal: one multi-line input, the row's key as private_metadata."""
    strings = card_i18n.STRINGS[lang]
    return {
        "type": "modal",
        "callback_id": COMMENT_CALLBACK_ID,
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": strings["comment_modal_title"]},
        "submit": {"type": "plain_text", "text": strings["comment_modal_submit"]},
        "close": {"type": "plain_text", "text": strings["comment_modal_close"]},
        "blocks": [
            {
                "type": "input",
                "block_id": COMMENT_BLOCK_ID,
                "label": {"type": "plain_text", "text": strings["comment_modal_label"]},
                "element": {
                    "type": "plain_text_input",
                    "action_id": COMMENT_ACTION_ID,
                    "multiline": True,
                    "max_length": COMMENT_MAX_CHARS,
                },
            }
        ],
    }


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


class RowCaps(NamedTuple):
    """How many rows each lane may show (the first n, bottom rows go) — or, as a result, how
    many each lane left out."""

    repairs: int = sys.maxsize
    advice: int = sys.maxsize
    reviews: int = sys.maxsize


UNCAPPED = RowCaps()


class Built(NamedTuple):
    blocks: list[dict]
    left_out: RowCaps
    advice_shown: tuple[int, ...] = ()
    rows: tuple[CardRow, ...] = ()
    samples_shown: int = 0


def build_blocks(*args, **kwargs) -> list[dict]:
    return build_card(*args, **kwargs).blocks


def build_card(
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
    score: Score | None = None,
    samples: Iterable[ProposedVerdict] = (),
    caps: RowCaps = UNCAPPED,
    samples_hidden: Iterable[ProposedVerdict] = (),
) -> Built:
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
    a zero-proposal morning must not change the card's shape.
    `score` adds the 채점 줄 right under the head lines; `samples` adds the 확인용 무작위 표본
    below the review lane, 맞아요/아니에요 only, idx slots after the reviews. The score line
    and the first sample never give way: the lanes above reserve that sample's blocks (a
    second one rides only in what is left under the 50-block cap), and `caps`
    (the first n rows of a lane) is how a caller makes room — what a lane left out, by cap
    or by the 50-block limit, comes back in `Built.left_out`, each counted in its overflow
    line, which carries the lane's one [더보기] button. Button idx slots are numbered from
    the full lists, so a hidden row never moves another row's idx. Every row left out comes
    back in `Built.rows` as a card_row with `shown` False and the whole row in `detail`;
    `samples_hidden` is the 표본 the caller already dropped before asking."""
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
    if score is not None:
        blocks.append(_scoreline_block(score, strings))

    reviews = list(reviews)
    samples = list(samples)
    samples_hidden = list(samples_hidden)
    sample_more = 1 if samples_hidden or len(samples) > SAMPLES_KEPT else 0
    sample_tail = (1 + 3 * min(len(samples), SAMPLES_KEPT) + sample_more) if samples else 0
    repairs_shown = 0
    card_rows: list[CardRow] = []
    if show_lanes:
        if repairs:
            blocks.append(_lane_header_block(strings["todo_header"]))
            for ridx, repair in list(enumerate(repairs))[: caps.repairs]:
                row = _repair_row(
                    ridx, n_repairs, repair, by_idx.get(ridx), repair_results.get(ridx), strings, text_max
                )
                if len(blocks) + len(row) + 2 + sample_tail > block_limit:
                    break
                blocks.extend(row)
                card_rows.append(CardRow(idx=ridx, lane="repair", value=_repair_row_value(repair)))
                repairs_shown += 1
            if repairs_shown < n_repairs:
                blocks.append(_overflow_block(n_repairs - repairs_shown, strings, "repair"))
                card_rows.extend(
                    _left_out_row(
                        ridx,
                        "repair",
                        _repair_row_value(repairs[ridx]),
                        repairs[ridx],
                        pos=ridx,
                        total=n_repairs,
                    )
                    for ridx in range(repairs_shown, n_repairs)
                )
        blocks.append(_lane_header_block(strings["advice_header"]))

    shown = 0
    advice_shown: list[int] = []
    overflowed = False
    # the review lane below must fit inside the same 50-block cap: the advice loop
    # reserves its whole tail (header + one 3-block row per review), and the review loop
    # reserves its own overflow line — an unshown row is counted there, never dropped
    # silently. A card that filled all 50 blocks with advice rows used to push the
    # review header past the cap. The first sample's blocks are reserved the same way: it
    # never gives way; the second rides only in what is left.
    review_tail = ((1 + 3 * len(reviews)) if reviews else 0) + sample_tail
    for _project, indices in _project_groups(proposals):
        for idx in indices:
            if shown >= caps.advice:
                overflowed = True
                break
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
            advice_shown.append(idx)
            shown += 1
        if overflowed:
            break

    if overflowed:
        blocks.append(_overflow_block(total - shown, strings, "advice"))
        card_rows.extend(
            _left_out_row(
                n_repairs + idx,
                "advice",
                _advice_row_value(proposals[idx]),
                proposals[idx],
                pos=idx,
                total=total,
            )
            for idx in range(total)
            if idx not in advice_shown
        )

    r_shown = 0
    if reviews:
        # the review lane sits below the advice one — the agent's own session-end calls,
        # which the owner may agree with, flip, hand back, or hold. No reviews, no header:
        # a lane the agent never filled must not render as an empty promise.
        blocks.append(_lane_header_block(strings["review_header"]))
        n_slots = n_repairs + total
        r_overflowed = False
        say_work = _works_once(reviews)
        for ridx, review in enumerate(reviews):
            if r_shown >= caps.reviews:
                r_overflowed = True
                break
            addition = _review_row(
                n_slots + ridx,
                review,
                by_idx.get(n_slots + ridx),
                strings,
                note_links,
                text_max,
                show_work=say_work[ridx],
            )
            remaining_after = len(reviews) - ridx - 1
            reserve = (1 if remaining_after > 0 else 0) + sample_tail
            if len(blocks) + len(addition) + reserve > block_limit:
                r_overflowed = True
                break
            blocks.extend(addition)
            card_rows.append(CardRow(idx=n_slots + ridx, lane="review", value=_review_row_value(review)))
            r_shown += 1
        if r_overflowed:
            blocks.append(_overflow_block(len(reviews) - r_shown, strings, "review"))
            card_rows.extend(
                _left_out_row(
                    n_slots + ridx,
                    "review",
                    _review_row_value(reviews[ridx]),
                    reviews[ridx],
                    ridx,
                    len(reviews),
                )
                for ridx in range(r_shown, len(reviews))
            )
    s_shown = 0
    first_slot = n_repairs + total + len(reviews)
    if samples and len(blocks) + sample_tail <= block_limit:
        blocks.append(_samples_header_block(strings))
        say_work = _works_once(samples)
        for sidx, sample in enumerate(samples):
            row = _review_row(
                first_slot + sidx,
                sample,
                by_idx.get(first_slot + sidx),
                strings,
                note_links,
                text_max,
                sample=True,
                show_work=say_work[sidx],
            )
            more_after = 1 if sidx < len(samples) - 1 or samples_hidden else 0
            if sidx >= SAMPLES_KEPT and len(blocks) + len(row) + more_after > block_limit:
                break
            blocks.extend(row)
            s_shown += 1
    s_left = [*samples[s_shown:], *samples_hidden]
    if s_left:
        if s_shown:
            blocks.append(_overflow_block(len(s_left), strings, "sample"))
        card_rows.extend(
            _left_out_row(
                first_slot + s_shown + k,
                "sample",
                _sample_row_value(sample),
                sample,
                s_shown + k,
                len(s_left) + s_shown,
            )
            for k, sample in enumerate(s_left)
        )
    return Built(
        blocks,
        RowCaps(repairs=n_repairs - repairs_shown, advice=total - shown, reviews=len(reviews) - r_shown),
        tuple(advice_shown),
        tuple(card_rows),
        s_shown,
    )


def _more_id(lane: str) -> str:
    return f"{MORE_ACTION_PREFIX}{lane}"


def _overflow_block(n: int, strings: dict[str, str], lane: str) -> dict:
    """The lane's 「N건 더 있음」 line with its one [더보기] button — a section, so the button
    costs no block of its own. The button carries no value: the left-out rows are read back
    by (card_ts, lane) from card_row."""
    return {
        "type": "section",
        "block_id": _more_id(lane),
        "text": {"type": "mrkdwn", "text": strings["overflow_line"].format(n=n)},
        "accessory": {
            "type": "button",
            "text": {"type": "plain_text", "text": strings["more_button"], "emoji": True},
            "action_id": _more_id(lane),
        },
    }


def _more_sent_block(lane: str, n: int, strings: dict[str, str]) -> dict:
    return {
        "type": "context",
        "block_id": _more_id(lane),
        "elements": [{"type": "mrkdwn", "text": strings["more_sent"].format(n=n)}],
    }


def _more_status_id(lane: str) -> str:
    return f"{_more_id(lane)}:status"


def more_progress(
    blocks: list[dict], lane: str, outcome: MoreSent | Failed, *, lang: str
) -> list[dict] | Rejected:
    """`blocks` — the message as Slack holds it now — with the lane's [더보기] line settled:
    sent turns the line into 「↓ 이어서 보냈어요 (N건)」 (no button left); a failure puts its
    reason above the line and keeps the button, so the owner can press again. Only that
    block changes. A message with no such line is Rejected."""
    strings = card_i18n.STRINGS[lang]
    kept = [block for block in blocks if block.get("block_id") != _more_status_id(lane)]
    at = next((i for i, block in enumerate(kept) if block.get("block_id") == _more_id(lane)), None)
    if at is None:
        return Rejected(reason=f"lane {lane} has no more line")
    match outcome:
        case MoreSent(n=n):
            return [*kept[:at], _more_sent_block(lane, n, strings), *kept[at + 1 :]]
        case Failed(reason=reason):
            status = {
                "type": "context",
                "block_id": _more_status_id(lane),
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": strings["progress_failed"].format(reason=_mrkdwn_plain(reason)),
                    }
                ],
            }
            if kept[at]["type"] != "section":
                return Rejected(reason=f"lane {lane} was already continued")
            return [*kept[:at], status, *kept[at:]]
        case _:
            assert_never(outcome)


def card_chars(blocks: list[dict]) -> int:
    return len(json.dumps(blocks, ensure_ascii=False))


class FitReport(NamedTuple):
    """What the card did to fit: its size, the longest a free-text line was let to run, the
    rows each lane left out, and whether the 확인 표본 rode. card.post_card records it as the
    card_fit event."""

    chars: int
    text_max: int
    left_out: RowCaps
    samples_shown: bool
    advice_shown: tuple[int, ...]
    rows: tuple[CardRow, ...] = ()


class Fitted(NamedTuple):
    blocks: list[dict]
    samples: list[ProposedVerdict]
    report: FitReport


def _fit_attempts(n: RowCaps, n_samples: int = 0) -> Iterable[tuple[int, RowCaps, int]]:
    """Cheapest give-up first: shorter prose down to the floor, then the samples past the
    first, then rows — the advice lane's bottom rows, the repair lane's, the review lane's.
    The score line and the first sample are never on this list."""
    for text_max in TEXT_MAX_STEPS:
        yield text_max, RowCaps(), n_samples
    floor = TEXT_MAX_STEPS[-1]
    kept = min(n_samples, SAMPLES_KEPT)
    if kept < n_samples:
        yield floor, RowCaps(), kept
    for k in range(n.advice - 1, -1, -1):
        yield floor, RowCaps(advice=k), kept
    for k in range(n.repairs - 1, -1, -1):
        yield floor, RowCaps(advice=0, repairs=k), kept
    for k in range(n.reviews - 1, -1, -1):
        yield floor, RowCaps(advice=0, repairs=0, reviews=k), kept


def fit_card(proposals: list[Proposal], *, samples: Iterable[ProposedVerdict] = (), **kwargs) -> Fitted:
    """build_card under CARD_CHARS_BUDGET. The prose shortens to TEXT_FLOOR and no further; past
    that, the second 확인 표본 goes first, then whole rows into the lane's overflow line (counted,
    not dropped). The 채점 줄 and the first 표본 stay. A card that still does not fit raises — a
    refused post must say why."""
    samples = list(samples)
    reviews = list(kwargs.pop("reviews", ()))
    n = RowCaps(repairs=len(kwargs.get("repairs") or ()), advice=len(proposals), reviews=len(reviews))
    last = 0
    for text_max, caps, n_samples in _fit_attempts(n, len(samples)):
        kept = samples[:n_samples]
        built = build_card(
            proposals,
            reviews=reviews,
            samples=kept,
            samples_hidden=samples[n_samples:],
            text_max=text_max,
            caps=caps,
            **kwargs,
        )
        last = card_chars(built.blocks)
        if last <= CARD_CHARS_BUDGET:
            carried = any(block.get("block_id") == SAMPLES_BLOCK_ID for block in built.blocks)
            report = FitReport(
                chars=last,
                text_max=text_max,
                left_out=built.left_out,
                samples_shown=carried,
                advice_shown=built.advice_shown,
                rows=built.rows,
            )
            return Fitted(built.blocks, kept[: built.samples_shown], report)
    raise ValueError(f"card is {last} chars with every row left out, over {CARD_CHARS_BUDGET}")


class PageRow(NamedTuple):
    """One left-out row as the [더보기] page draws it: `idx` the button slot it always had,
    `pos`/`total` its 「k/N」 place in its lane, `item` the row itself."""

    idx: int
    pos: int
    total: int
    item: Proposal | Repair | ProposedVerdict


class Page(NamedTuple):
    """The next message of one lane: its blocks, the rows it carries, the rows it leaves for
    a further [더보기], and the card_row for each of them (the carried repair/review rows with
    their values, the left-out rows whole)."""

    blocks: list[dict]
    shown: tuple[PageRow, ...]
    left: tuple[PageRow, ...]
    rows: tuple[CardRow, ...]


_LANE_ITEMS: dict[str, type[Proposal] | type[Repair] | type[ProposedVerdict]] = {
    "repair": Repair,
    "advice": Proposal,
    "review": ProposedVerdict,
    "sample": ProposedVerdict,
}


def page_rows(lane: str, details: list[tuple[int, dict]]) -> list[PageRow] | Rejected:
    """The left-out rows' `detail` values (with their idx) back as page rows. A detail that
    does not rebuild is Rejected, never skipped — a lane page missing a row would hide it again."""
    model = _LANE_ITEMS[lane]
    out: list[PageRow] = []
    for idx, detail in details:
        try:
            out.append(
                PageRow(idx, int(detail["pos"]), int(detail["total"]), model.model_validate(detail["item"]))
            )
        except (KeyError, TypeError, ValueError) as e:
            return Rejected(reason=f"{lane} row {idx} does not rebuild: {e}")
    return out


_LANE_TITLES = {
    "repair": "todo_header",
    "advice": "advice_header",
    "review": "review_header",
    "sample": "sample_header",
}


def page_title(lane: str, lang: str) -> str:
    """The lane's own title — a page's header block, and the text its message notifies with."""
    return card_i18n.STRINGS[lang][_LANE_TITLES[lane]]


def _page_row_blocks(
    lane: str,
    row: PageRow,
    strings: dict[str, str],
    register_labels: dict[str, str],
    note_links: tuple[NoteLink, ...],
    text_max: int,
    show_work: bool,
) -> list[dict]:
    match lane:
        case "repair":
            return _repair_row(row.idx, row.total, row.item, None, None, strings, text_max)
        case "advice":
            return _proposal_row(
                row.pos,
                row.total,
                row.item,
                None,
                strings,
                register_labels,
                action_idx=row.idx,
                note_links=note_links,
            )
        case "review":
            return _review_row(row.idx, row.item, None, strings, note_links, text_max, show_work=show_work)
        case "sample":
            return _review_row(
                row.idx, row.item, None, strings, note_links, text_max, sample=True, show_work=show_work
            )
        case _:
            raise ValueError(f"no such lane {lane!r}")


_ROW_VALUES = {
    "repair": _repair_row_value,
    "advice": _advice_row_value,
    "review": _review_row_value,
    "sample": _sample_row_value,
}

#: The lanes whose buttons carry no value — only their rows that rode have a card_row. The
#: advice and 표본 rows still carry their value in the button, as on the first card.
_VALUELESS_LANES = ("repair", "review")


def _page_card_rows(lane: str, shown: Iterable[PageRow], left: Iterable[PageRow]) -> list[CardRow]:
    value = _ROW_VALUES[lane]
    rode = (
        [CardRow(idx=r.idx, lane=lane, value=value(r.item)) for r in shown]
        if lane in _VALUELESS_LANES
        else []
    )
    kept = [_left_out_row(r.idx, lane, value(r.item), r.item, r.pos, r.total) for r in left]
    return [*rode, *kept]


def build_page(
    lane: str,
    rows: list[PageRow],
    *,
    lang: str,
    note_links: tuple[NoteLink, ...] = (),
    text_max: int = REVIEW_TEXT_MAX,
    cap: int = sys.maxsize,
    block_limit: int = BLOCK_LIMIT,
) -> Page:
    """The lane's next message: its header, then the rows drawn by the very functions that drew
    them on the card, buttons and all (the same slot idx, so a press or a comment reads its
    card_row by the new message's ts like any card's). What does not fit the block cap or
    `cap` is counted in the lane's overflow line with its own [더보기]."""
    strings = card_i18n.STRINGS[lang]
    blocks = [
        _samples_header_block(strings) if lane == "sample" else _lane_header_block(page_title(lane, lang))
    ]
    say_work = _works_once([row.item for row in rows]) if lane in ("review", "sample") else [True] * len(rows)
    shown: list[PageRow] = []
    for i, row in enumerate(rows):
        if i >= cap:
            break
        segment = _page_row_blocks(
            lane, row, strings, card_i18n.REGISTER_LABELS[lang], note_links, text_max, say_work[i]
        )
        more_after = 1 if i < len(rows) - 1 else 0
        if i > 0 and len(blocks) + len(segment) + more_after > block_limit:
            break
        blocks.extend(segment)
        shown.append(row)
    left = rows[len(shown) :]
    if left:
        blocks.append(_overflow_block(len(left), strings, lane))
    return Page(blocks, tuple(shown), tuple(left), tuple(_page_card_rows(lane, shown, left)))


def fit_page(lane: str, rows: list[PageRow], *, lang: str, note_links: tuple[NoteLink, ...] = ()) -> Page:
    """build_page under CARD_CHARS_BUDGET, giving up the way the card does: shorter prose down to
    TEXT_FLOOR first, then rows from the bottom — never the first. A page that still does not
    fit raises: a refused post must say why."""
    last = 0
    attempts = [(text_max, sys.maxsize) for text_max in TEXT_MAX_STEPS]
    attempts += [(TEXT_MAX_STEPS[-1], k) for k in range(len(rows) - 1, 0, -1)]
    for text_max, cap in attempts:
        page = build_page(lane, rows, lang=lang, note_links=note_links, text_max=text_max, cap=cap)
        last = card_chars(page.blocks)
        if last <= CARD_CHARS_BUDGET:
            return page
    raise ValueError(f"{lane} page is {last} chars with one row, over {CARD_CHARS_BUDGET}")
