#!/usr/bin/env python3
"""The morning card's pure core — no Slack, no LLM, no engine.

The card proposes things picked from the engine's four registers (up to three, fewer when
subjects refuse to resolve), and a button press (해 · 미뤄 · 빼) is the verdict. Everything
that decides what a card means lives here: the register shapes, the prompt, the proposal
parser that refuses anything not grounded in a register's sources, the subject→note
resolution values, the past-approval cross-check, the Block Kit layout, and the
button-payload parser that returns a Rejected value instead of raising. The graph (card.py)
and the socket (card.py main) are thin because this file is strict.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RegisterName = Literal["next_actions", "risks", "stalled", "recurrences"]
Choice = Literal["do", "defer", "drop"]

ANSWER_REGISTERS: tuple[str, ...] = ("next_actions", "risks", "stalled")
REGISTER_NAMES: tuple[str, ...] = ANSWER_REGISTERS + ("recurrences",)

CARD_TITLE = "☀️ 오늘 제안"
BUTTON_LABELS: dict[str, str] = {"do": "해", "defer": "미뤄", "drop": "빼"}
VERDICT_MARKS: dict[str, str] = {"do": "✓ 해", "defer": "… 미뤄", "drop": "✕ 빼"}

#: Per-register cap on the text that enters the prompt. The engine answers are already capped,
#: but three of them plus the sources lists still outgrow a small local model's context.
PROMPT_SECTION_BUDGET = 3500


class Proposal(BaseModel):
    """One thing the card proposes. `subject` is one of the strings its register listed —
    that is what the model picks and parse_proposals grounds. `note` is the note path the
    subject resolves to through the door's /claim-source; the graph fills it after propose,
    and until then it is empty. The button value, the handover, and the consumption verdict
    all cite `note` — a verdict attaches to doc:<note path>, never to a bare subject. The
    attribute is `register_` only because a field literally named `register` would shadow
    BaseModel.register (pydantic 2.13 then treats the inherited method as the field's
    default); the JSON name stays `register` via the alias."""

    model_config = ConfigDict(populate_by_name=True)

    title: str = Field(min_length=1)
    why: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    note: str = ""
    register_: RegisterName = Field(alias="register")


class Proposals(BaseModel):
    """The structured-output schema handed to the local model — exactly three proposals,
    each grounded in a different subject. Two, four, or one subject twice is refused here
    with the reason, before a card exists. `note` is not in this schema's vocabulary: the
    model only picks subjects, the graph resolves them afterwards."""

    proposals: list[Proposal] = Field(min_length=3, max_length=3)

    @field_validator("proposals")
    @classmethod
    def _distinct_subjects(cls, proposals: list[Proposal]) -> list[Proposal]:
        subjects = [p.subject for p in proposals]
        if len(set(subjects)) != len(subjects):
            dupes = sorted({s for s in subjects if subjects.count(s) > 1})
            raise ValueError(f"proposals: duplicate subject(s): {', '.join(dupes)}")
        return proposals


class ResolvedNote(BaseModel):
    """A subject the door resolved to its current claim's note path."""

    subject: str
    note: str


class Unresolved(BaseModel):
    """A subject with no current claim — or no answer at all. A value, never an exception:
    the card drops the proposal and reports the reason in the aggregate refusal."""

    model_config = ConfigDict(populate_by_name=True)

    subject: str
    register_: str = Field(alias="register")
    reason: str


class PastApproved(BaseModel):
    """One 「해」 from a past card, as /approved reports it."""

    session: str
    note: str
    at: str


#: The /claim-source 404 reason — the door answered, and the subject has no current claim.
#: Every other Unresolved reason is a door failure (5xx, unreachable), which proves nothing.
NO_CURRENT_CLAIM = "no current claim for subject"


def is_absence(reason: str) -> bool:
    """404 establishes absence; 5xx·불통 leave the question open."""
    return reason == NO_CURRENT_CLAIM


class Confirmation(BaseModel):
    """The past-approval cross-check for the card's head, as note paths. A past 「해」 whose
    note no longer resolves from any of today's register subjects is 했다 (the register let
    it go) — but only a clean run earns that: a door failure (5xx·불통) leaves its absence
    unproven, so it lands in unknown, never a false 했다. One whose note still resolves is
    아직 — its subject comes back as a proposal candidate."""

    total: int
    done: list[str]
    pending: list[str]
    unknown: list[tuple[str, str]] = []
    session: str | None = None


def confirm_past(
    approved: list[PastApproved],
    today_notes: set[str],
    failures: Iterable[str] = (),
) -> Confirmation:
    """Partition past approvals against the note paths today's registers resolve to. A
    failure during today's resolves degrades the run: what a dead door cannot disprove
    stays unknown, not done."""
    failures = list(failures)
    reason = "; ".join(sorted(set(failures)))
    done: list[str] = []
    pending: list[str] = []
    unknown: list[tuple[str, str]] = []
    for item in approved:
        if item.note in today_notes:
            pending.append(item.note)
        elif failures:
            unknown.append((item.note, reason))
        else:
            done.append(item.note)
    return Confirmation(
        total=len(approved),
        done=done,
        pending=pending,
        unknown=unknown,
        session=approved[0].session if approved else None,
    )


class ButtonVerdict(BaseModel):
    """One button press, already judged trustworthy by parse_action."""

    idx: int
    choice: Choice
    user: str
    at: str


class Rejected(BaseModel):
    """A button press that is not a verdict. A value, never an exception — the socket loop
    logs the reason and keeps listening."""

    reason: str


class PostedCard(BaseModel):
    """Where the card lives in Slack; also the engine session's identity (slack:<channel>:<ts>)."""

    channel: str
    ts: str


class Registers(BaseModel):
    """The four engine registers as prompt text, and per register the allow-list a
    proposal's `source_note` must come from."""

    texts: dict[str, str]
    sources: dict[str, list[str]]


def _note_path(node: Any) -> str | None:
    path = (node or {}).get("source_path")
    return str(path) if path else None


def _recurrence_paths(rows: list[dict]) -> list[str]:
    out: set[str] = set()
    for row in rows:
        newer = _note_path(row.get("newer"))
        if newer:
            out.add(newer)
        for older in row.get("older") or []:
            path = _note_path(older)
            if path:
                out.add(path)
    return sorted(out)


def _recurrence_text(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        newer = row.get("newer") or {}
        head = " — ".join(str(newer[k]) for k in ("subject", "predicate", "value") if newer.get(k))
        if head:
            lines.append(f"* {head}")
    return "\n".join(lines)


def collect_registers(fetch: Callable[[str], dict[str, Any]]) -> Registers:
    """Shape the four register answers through the injected `fetch` (path in, engine JSON out).
    A malformed payload is a ValueError, not a skip — proposals are grounded on these, and
    half a register set means grounding on nothing."""

    texts: dict[str, str] = {}
    sources: dict[str, list[str]] = {}
    for name in ANSWER_REGISTERS:
        data = fetch(f"/{name}")
        answer, srcs = data.get("answer"), data.get("sources")
        if not isinstance(answer, str) or not isinstance(srcs, list):
            raise ValueError(f"register {name}: expected {{answer, sources}}, got keys {sorted(data)!r}")
        texts[name] = answer
        sources[name] = [str(s) for s in srcs]
    data = fetch("/recurrences")
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"register recurrences: expected rows list, got keys {sorted(data)!r}")
    texts["recurrences"] = _recurrence_text(rows)
    sources["recurrences"] = _recurrence_paths(rows)
    return Registers(texts=texts, sources=sources)


def build_prompt(
    registers: Registers,
    exclude: set[str] | None = None,
    priority: Iterable[str] = (),
) -> str:
    """The whole proposal task: the registers with their allow-lists, and the rules. The model
    only picks and justifies — invention is refused later, in parse_proposals. `exclude`
    narrows the allow-lists to what a previous round has not already picked or failed to
    resolve; the re-propose after a resolve failure runs on the remainder. `priority` lists
    subjects whose past 「해」 is still claimed today — the card asks for them first."""
    exclude = exclude or set()
    sections = []
    for name in REGISTER_NAMES:
        text = registers.texts.get(name, "")[:PROMPT_SECTION_BUDGET]
        listed = "\n".join(
            f"{i}. {s}"
            for i, s in enumerate((s for s in registers.sources.get(name, []) if s not in exclude), 1)
        )
        sections.append(f"[{name}]\n{text}\n\nsources:\n{listed}")
    rules = (
        "아래 네 개 레지스터는 엔진이 오늘 본 것들이다. 이 안에서 오늘 제안 셋(정확히 3개)을 골라라.\n"
        "\n"
        "출력은 JSON 하나뿐이다:\n"
        '{"proposals": [{"title": "짧은 명사구", "why": "한 문장", "subject": "…", '
        '"register": "…"}, …]}\n'
        "\n"
        "규칙:\n"
        "- 새로운 사실을 지어내지 마라. 레지스터에 있는 것만 제안할 수 있다.\n"
        "- subject 는 그 섹션의 sources 목록 원소 하나를 글자 그대로 복사한 것이다. "
        "목록에 없는 문자열은 무효다.\n"
        '- 좋은 예: 목록이 `1. next_action` 이면 "subject": "next_action" 이다.\n'
        '- 나쁜 예: "subject": "next_action — task_list: 정본…" — 본문 줄이지 목록의 '
        "원소가 아니므로 무효다.\n"
        "- register 는 그 제안을 고른 섹션 이름이다(next_actions | risks | stalled | recurrences).\n"
        "- title 과 why 는 한국어로.\n"
    )
    priority = list(priority)
    if priority:
        listed = "\n".join(f"- {s}" for s in priority)
        sections.append(
            "[우선 후보]\n"
            "아직 끝나지 않은 지난 승인의 주어들이다. 가능하면 제안 셋을 이 목록에서 고른 "
            f"subject 로 채워라.\n{listed}"
        )
    return rules + "\n" + "\n\n".join(sections)


def parse_proposals(llm_json: str, registers: Registers, exclude: set[str] | None = None) -> list[Proposal]:
    """JSON text in, grounded proposals out — or ValueError. This is the gate the schema
    cannot build: the model had the allow-lists, and anything not in them is refused here,
    before a card exists. `exclude` is the same narrowing build_prompt showed — a re-propose
    must not regurgitate a subject the resolve step already dropped."""

    exclude = exclude or set()
    try:
        data = json.loads(llm_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"proposals: not JSON: {e}") from e
    proposals = Proposals.model_validate(data).proposals
    for proposal in proposals:
        allowed = [s for s in (registers.sources.get(proposal.register_) or []) if s not in exclude]
        if proposal.subject not in allowed:
            raise ValueError(
                f"proposals: subject {proposal.subject!r} not in register {proposal.register_!r} sources"
            )
    return proposals


def _actions_block(idx: int, proposal: Proposal) -> dict:
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": label, "emoji": True},
                "action_id": f"card:{idx}:{choice}",
                "value": proposal.note,
            }
            for choice, label in BUTTON_LABELS.items()
        ],
    }


def _confirmation_block(confirmation: Confirmation) -> dict:
    text = (
        f"지난 승인 {confirmation.total} · 했다 {len(confirmation.done)} · 아직 {len(confirmation.pending)}"
    )
    if confirmation.unknown:
        text += f" · 확인불가 {len(confirmation.unknown)}"
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def build_blocks(
    proposals: list[Proposal],
    verdicts: Iterable[ButtonVerdict] = (),
    confirmation: Confirmation | None = None,
) -> list[dict]:
    """Block Kit for the card: the head line (past approvals cross-checked against today's
    registers — present only when there were any), then one section and one button row per
    proposal. A judged row shows its mark instead of its buttons — the card is edited in
    place as 판정들 arrive. Buttons carry the note path: a verdict attaches to the note."""
    by_idx = {v.idx: v for v in verdicts}
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{CARD_TITLE} {len(proposals)}", "emoji": True},
        }
    ]
    if confirmation is not None and confirmation.total > 0:
        blocks.append(_confirmation_block(confirmation))
    for idx, proposal in enumerate(proposals):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{idx + 1}. {proposal.title}*\n{proposal.why}\n"
                        f"_{proposal.register_} · {proposal.subject}_"
                    ),
                },
            }
        )
        verdict = by_idx.get(idx)
        if verdict is None:
            blocks.append(_actions_block(idx, proposal))
        else:
            mark = VERDICT_MARKS[verdict.choice]
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"{mark} — <@{verdict.user}>"}],
                }
            )
    return blocks


def parse_action(
    payload: dict,
    *,
    owner_id: str | None,
    n_proposals: int,
    at: str | None = None,
) -> ButtonVerdict | Rejected:
    """A block_actions payload in, one verdict out — or Rejected with the reason. Nothing here
    raises: a weird button is a fact about the world, not a crash. When an owner is configured,
    nobody else's press counts."""

    if payload.get("type") != "block_actions":
        return Rejected(reason="not block_actions")
    actions = payload.get("actions") or []
    if len(actions) != 1:
        return Rejected(reason="not exactly one action")
    action_id = actions[0].get("action_id") or ""
    parts = action_id.split(":")
    if len(parts) != 3 or parts[0] != "card":
        return Rejected(reason=f"unknown action_id {action_id!r}")
    try:
        idx = int(parts[1])
    except ValueError:
        return Rejected(reason=f"unknown action_id {action_id!r}")
    choice = parts[2]
    if choice not in BUTTON_LABELS:
        return Rejected(reason=f"unknown choice {choice!r}")
    if idx < 0 or idx >= n_proposals:
        return Rejected(reason=f"no proposal {idx}")
    user = (payload.get("user") or {}).get("id") or ""
    if not user:
        return Rejected(reason="no user")
    if owner_id is not None and user != owner_id:
        return Rejected(reason=f"user {user} is not the owner")
    return ButtonVerdict(idx=idx, choice=choice, user=user, at=at or datetime.now(UTC).isoformat())
