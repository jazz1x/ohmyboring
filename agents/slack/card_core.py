#!/usr/bin/env python3
"""The morning card's pure core — no Slack, no LLM, no engine.

The card advises on things picked from the engine's four registers (up to three, fewer
when a candidate is not worth it or its subject refuses to resolve), and a button press
(해 · 미뤄 · 빼) is the verdict. Everything that decides what a card means lives here: the
register shapes, the per-candidate advice prompt, the evidence-grounding parser (a quote
that does not appear in its cited note's own text is dropped, never trusted), the
subject→note resolution values, the past-approval cross-check, the Block Kit layout, and
the button-payload parser that returns a Rejected value instead of raising. The graph
(card.py) and the socket (card.py main) are thin because this file is strict.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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

#: Registers a bottleneck can come from. next_actions is already actionable on its own
#: (deterministic, no LLM) — advice is for what is stuck, risky, or has happened before.
BOTTLENECK_REGISTERS: tuple[RegisterName, ...] = ("recurrences", "risks", "stalled")

MIN_BOTTLENECK_CHARS = 10
MIN_ADVICE_CHARS = 10
MIN_QUOTE_CHARS = 12


class EvidenceInput(BaseModel):
    """One cited note as the model gives it — no line. The model is never trusted with a
    coordinate; parse_advised computes line from the note's own text or drops the evidence."""

    note: str = Field(min_length=1)
    quote: str = Field(min_length=MIN_QUOTE_CHARS)


class Evidence(BaseModel):
    """One cited note, grounded: `quote` verified verbatim (whitespace collapsed) in `note`'s
    own text, `line` the 1-based line the parser found it on."""

    note: str
    quote: str
    line: int


class ProposalInput(BaseModel):
    """What gemma4 returns for one candidate that is worth advising on. `kind` discriminates
    Advised — the model chooses this branch or NotWorthInput, never both."""

    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["proposal"] = "proposal"
    bottleneck: str = Field(min_length=MIN_BOTTLENECK_CHARS)
    advice: str = Field(min_length=MIN_ADVICE_CHARS)
    evidence: list[EvidenceInput] = Field(min_length=1)


class NotWorthInput(BaseModel):
    """The model's other branch: this candidate earns no advice today. A legitimate answer,
    not a failure — 실험 1's recall precision (1/5) means most candidates should land here."""

    kind: Literal["not_worth"] = "not_worth"
    reason: str = Field(min_length=1)


class AdvisedInput(BaseModel):
    """The structured-output wrapper for one candidate call — a discriminated union so the
    model states which branch it is answering, not something inferred from field presence."""

    result: Annotated[ProposalInput | NotWorthInput, Field(discriminator="kind")]


class NotWorth(BaseModel):
    """A candidate the model judged not worth advising on. Passes through parse_advised
    unchanged — it is not a rejection, it is the model's answer."""

    reason: str


class Ungrounded(BaseModel):
    """A candidate whose JSON did not parse, did not fit the schema, or lost every piece of
    evidence to the quote check. A value, never an exception: one weak candidate must not
    stop the advise loop over the rest of the queue."""

    reason: str


class Advice(BaseModel):
    """One candidate's grounded pitch, evidence already verified. Subject and register are
    not here — parse_advised only grounds evidence against note text; the advise loop (which
    already knows which candidate it asked about) attaches them when it builds the Proposal."""

    bottleneck: str
    advice: str
    evidence: list[Evidence] = Field(min_length=1)


class Proposal(BaseModel):
    """One thing the card advises: a bottleneck sentence, a today-do-this sentence, and the
    evidence (past notes, quoted and located) that grounds them. `subject` is the register
    entry that led to this pick; `note` is the current claim path the door resolved that
    subject to (empty until the resolve node fills it — the button value, handover, and
    consumption all cite `note`, never `subject`). The attribute is `register_` only because
    a field literally named `register` would shadow BaseModel.register; the JSON name stays
    `register` via the alias."""

    model_config = ConfigDict(populate_by_name=True)

    subject: str = Field(min_length=1)
    note: str = ""
    register_: RegisterName = Field(alias="register")
    bottleneck: str = Field(min_length=MIN_BOTTLENECK_CHARS)
    advice: str = Field(min_length=MIN_ADVICE_CHARS)
    evidence: list[Evidence] = Field(min_length=1)


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


class AdviseStats(BaseModel):
    """The advise loop's own tally, computed once in `advise` and carried in state so nobody
    downstream recomputes it: how many candidates were tried, how many became proposals, and
    why the rest were refused. AC5 asks the dry-run executor to be able to quote a run's
    numbers back — before this, `NotWorth.reason` and the `Ungrounded` reasons were discarded
    the moment `advise` read them."""

    calls: int
    proposals_passed: int
    not_worth: int
    ungrounded: int
    not_worth_reasons: list[str] = []
    ungrounded_reasons: list[str] = []


def handover_paths(proposals: Iterable[Proposal]) -> list[str]:
    """Every note path a card actually cited to the owner: each proposal's own resolved note,
    plus every note its evidence quoted — de-duplicated, first-seen order. A card that quoted
    a note in its evidence line without listing it here would leave the engine unable to see
    what actually grounded the pitch (AC4)."""
    seen: set[str] = set()
    out: list[str] = []
    for proposal in proposals:
        for note in (proposal.note, *(e.note for e in proposal.evidence)):
            if note and note not in seen:
                seen.add(note)
                out.append(note)
    return out


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


def candidate_order(registers: Registers, priority: Iterable[str] = ()) -> list[tuple[str, RegisterName]]:
    """The advise loop's queue: 「아직」 subjects first (a past 「해」 still claimed today —
    wiki-1765's step 3 asks for these first), then the bottleneck registers in the owner's
    order (재발 → 막힘/위험 → 정체). next_actions never appears — it is already actionable,
    nothing to advise about. Each subject appears once, in the register it actually came from."""
    seen: set[str] = set()
    out: list[tuple[str, RegisterName]] = []
    for subject in priority:
        if subject in seen:
            continue
        seen.add(subject)
        register = next(
            (name for name in BOTTLENECK_REGISTERS if subject in registers.sources.get(name, [])),
            BOTTLENECK_REGISTERS[0],
        )
        out.append((subject, register))
    for name in BOTTLENECK_REGISTERS:
        for subject in registers.sources.get(name, []):
            if subject in seen:
                continue
            seen.add(subject)
            out.append((subject, name))
    return out


def build_advice_prompt(subject: str, register: RegisterName, hits: list[dict[str, Any]]) -> str:
    """One candidate's whole task: a subject, its register, and up to three past hits
    (`/search` with `claims`) to ground advice in. The model only quotes and reasons —
    parse_advised is the boundary that checks a quote actually appears in its note."""
    sections = []
    for hit in hits[:3]:
        note = hit.get("source_path", "")
        snippet = str(hit.get("snippet") or "")[:600]
        claims = "\n".join(
            f"  - {c.get('subject', '')} — {c.get('predicate', '')}: {c.get('value', '')}"
            for c in (hit.get("claims") or [])
        )
        # No brackets or other punctuation around the path — a candidate that displayed
        # `[note]` had gemma4 copy the brackets into evidence.note, so every quote from that
        # hit failed to verify (the real note path never carries them). The label is its own
        # line so nothing but the path itself sits after "노트 경로:".
        sections.append(f"노트 경로: {note}\n{snippet}\n{claims}".rstrip())
    hits_text = "\n\n".join(sections) if sections else "(과거 기록 없음)"
    return (
        f"오늘의 후보 주어: {subject!r} (레지스터: {register})\n\n"
        f"과거 기록(검색 결과):\n{hits_text}\n\n"
        "이 주어가 오늘 조언할 가치가 있으면 아래 JSON 하나만 출력해라:\n"
        '{"result": {"kind": "proposal", '
        f'"bottleneck": "병목 한 문장({MIN_BOTTLENECK_CHARS}자 이상)", '
        f'"advice": "오늘 할 것 한 문장({MIN_ADVICE_CHARS}자 이상)", '
        '"evidence": [{"note": "위 노트 경로 줄의 경로 문자열", '
        f'"quote": "그 노트 본문에서 글자 그대로 옮긴 인용({MIN_QUOTE_CHARS}자 이상)"'
        "}]}}\n\n"
        "조언할 가치가 없으면:\n"
        '{"result": {"kind": "not_worth", "reason": "한 문장 이유"}}\n\n'
        "규칙:\n"
        "- 새로운 사실을 지어내지 마라. quote 는 위 과거 기록 본문에서 그대로 옮겨라 — "
        "지어내면 그 근거는 버려진다.\n"
        '- note 는 "노트 경로: " 뒤에 오는 문자열 그대로다. 대괄호나 다른 기호를 붙이지 마라. '
        '예: 노트 경로: /vault/wiki/wiki-0900.md 이면 "note": "/vault/wiki/wiki-0900.md" 다.\n'
        "- 근거가 하나도 검증되지 않을 것 같으면 처음부터 not_worth 를 골라라.\n"
    )


def _find_quote_line(text: str, quote: str) -> int | None:
    """The 1-based line where `quote` verbatim-appears in `text`, whitespace runs collapsed
    (the model retypes rather than copy-pastes, so a newline-for-space difference is not a
    fabrication) — or None when no line contains it."""

    def norm(s: str) -> str:
        return " ".join(s.split())

    target = norm(quote)
    if not target:
        return None
    for i, line in enumerate(text.splitlines(), start=1):
        if target in norm(line):
            return i
    return None


def parse_advised(llm_json: str, note_texts: dict[str, str]) -> Advice | NotWorth | Ungrounded:
    """One candidate's structured JSON in, a grounded value out — never an exception: a weak
    candidate is a fact about that candidate, not a reason to stop the advise loop over the
    rest of the queue. Evidence whose quote does not verify against its own note's text
    (looked up in `note_texts`, keyed by note path) is dropped; a proposal left with none is
    Ungrounded (실험 1, wiki-1064: 좌표 없는 조언은 타입이 거부). NotWorth passes through."""
    try:
        data = json.loads(llm_json)
    except json.JSONDecodeError as e:
        return Ungrounded(reason=f"not JSON: {e}")
    try:
        advised = AdvisedInput.model_validate(data).result
    except ValidationError as e:
        return Ungrounded(reason=f"schema: {e}")
    if isinstance(advised, NotWorthInput):
        return NotWorth(reason=advised.reason)
    grounded: list[Evidence] = []
    for item in advised.evidence:
        text = note_texts.get(item.note)
        if text is None:
            continue
        line = _find_quote_line(text, item.quote)
        if line is None:
            continue
        grounded.append(Evidence(note=item.note, quote=item.quote, line=line))
    if not grounded:
        return Ungrounded(reason="no evidence verified against note text")
    return Advice(bottleneck=advised.bottleneck, advice=advised.advice, evidence=grounded)


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
                        f"*{idx + 1}. {proposal.advice}*\n병목: {proposal.bottleneck}\n"
                        f"_{proposal.register_} · {proposal.subject}_"
                    ),
                },
            }
        )
        blocks.append(_evidence_block(proposal.evidence))
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
