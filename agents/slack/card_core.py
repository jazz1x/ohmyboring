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
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import card_i18n
from pydantic import BaseModel, ConfigDict, Field, ValidationError

RegisterName = Literal["next_actions", "risks", "stalled", "recurrences"]
Choice = Literal["do", "defer", "drop"]

ANSWER_REGISTERS: tuple[str, ...] = ("next_actions", "risks", "stalled")
REGISTER_NAMES: tuple[str, ...] = ANSWER_REGISTERS + ("recurrences",)

#: The button/verdict vocabulary itself — language-independent, unlike its label text
#: (card_i18n.STRINGS). parse_action validates a press against this, never against a
#: language table, so a Japanese-language card still accepts the same action_ids.
CHOICES: tuple[str, ...] = ("do", "defer", "drop")

#: card_i18n.STRINGS keys this file's display-building functions may render.
DISPLAY_LANGS: tuple[str, ...] = ("en", "ko", "ja")


def resolve_lang(raw: str) -> str:
    """boring.json's note_lang, folded to one of the three languages the card's display
    table (card_i18n.STRINGS) knows. An explicit ko/ja/en passes straight through; anything
    else — 'auto' included — falls back to English, the same way distill_core's own
    lang_instruction dict falls back for an unmapped lang, except a card's title and
    buttons have no transcript to auto-detect a language from, so the fallback here is a
    fixed value rather than distill's 'write in whatever language the transcript is'."""
    return raw if raw in DISPLAY_LANGS else "en"


#: The advise loop's per-candidate model prompt gets one language-instruction line appended,
#: attached the same way distill_core._build_prompt attaches its lang_instruction — the rest
#: of the prompt (JSON schema, field-format rules) is not a display string and stays as-is.
ADVICE_LANG_INSTRUCTION: dict[str, str] = {
    "ko": "bottleneck 과 advice 는 반드시 한국어 문장으로 써라.",
    "ja": "bottleneck と advice は必ず日本語の文で書け。",
    "en": "Write bottleneck and advice as English sentences.",
}


def _advice_lang_instruction(lang: str) -> str:
    return ADVICE_LANG_INSTRUCTION.get(
        lang, "Write bottleneck and advice in the same language as the past record above."
    )


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
    consumption all cite `note`, never `subject`). `project` is which active project's
    register call this candidate came from — "" for the unassigned bucket, never absent.
    The attribute is `register_` only because a field literally named `register` would
    shadow BaseModel.register; the JSON name stays `register` via the alias."""

    model_config = ConfigDict(populate_by_name=True)

    subject: str = Field(min_length=1)
    note: str = ""
    project: str = ""
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


#: The suppression window, hours — 7 days, matching the contract's "지난 7일" rule.
SUPPRESS_WINDOW_HOURS = 168


class PastVerdictPair(BaseModel):
    """One past button press, joined back to the note+evidence its card_proposal event
    named — a card_verdict event alone carries only card_ts, idx, and choice, so the join
    happens on the read side (card.py) before this value ever reaches `suppressed`."""

    note: str
    evidence_note: str
    evidence_line: int
    choice: Choice
    at: str


def suppressed(
    candidates: list[Proposal],
    past: list[PastVerdictPair],
    now: datetime | None = None,
) -> tuple[list[Proposal], list[Proposal]]:
    """Split resolved candidates into (kept, dropped): a candidate is dropped when its own
    note and its first evidence's (note, line) match a pair that already got a 해/빼 verdict
    within the last 7 days. 미뤄 leaves no trace here — card.py never asks record_verdict for
    a consumption call on defer, so a deferred pair can never enter `past` and can never
    suppress. A pair older than the window does not suppress either: the owner may see the
    same bottleneck again once enough time has passed for it to be worth asking again. A
    `past` entry whose `at` does not parse is not skipped: a judged-history row this function
    cannot place in time is exactly the case the contract calls a wrong card, not a quiet
    gap — it raises, same as an unresolvable pair upstream in card.py's own read."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=SUPPRESS_WINDOW_HOURS)
    recent: set[tuple[str, str, int]] = set()
    for verdict in past:
        if verdict.choice not in ("do", "drop"):
            continue
        at = datetime.fromisoformat(verdict.at)
        if at < cutoff:
            continue
        recent.add((verdict.note, verdict.evidence_note, verdict.evidence_line))
    kept: list[Proposal] = []
    dropped: list[Proposal] = []
    for candidate in candidates:
        if not candidate.evidence:
            kept.append(candidate)
            continue
        key = (candidate.note, candidate.evidence[0].note, candidate.evidence[0].line)
        (dropped if key in recent else kept).append(candidate)
    return kept, dropped


def proposal_event_fields(proposal: Proposal, lang: str, card_ts: str, idx: int) -> dict[str, Any]:
    """The card_proposal event's fields — everything the owner saw plus enough to join a
    later card_verdict event back to it (card_ts + idx, the only two fields a button press
    itself carries)."""
    return {
        "register": proposal.register_,
        "project": proposal.project,
        "subject": proposal.subject,
        "note": proposal.note,
        "bottleneck": proposal.bottleneck,
        "advice": proposal.advice,
        "evidence": [e.model_dump() for e in proposal.evidence],
        "lang": lang,
        "card_ts": card_ts,
        "idx": idx,
    }


def verdict_event_fields(verdict: ButtonVerdict, card_ts: str) -> dict[str, Any]:
    """The card_verdict event's fields — deliberately thin; the note and evidence a press
    judged are recovered by joining back to that card_ts+idx's card_proposal event."""
    return {"card_ts": card_ts, "idx": verdict.idx, "choice": verdict.choice}


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


def collect_registers(fetch: Callable[[str, str], dict[str, Any]], project: str = "") -> Registers:
    """Shape one project's four register answers through the injected `fetch` (path, project
    in — engine JSON out). `project=""` is not "no filter": the engine's own register filter
    treats an explicit empty string as "unassigned documents only" (measured 2026-09-22), so
    every call — active project or the unassigned bucket alike — always names one. A
    malformed payload is a ValueError, not a skip — proposals are grounded on these, and half
    a register set means grounding on nothing."""

    texts: dict[str, str] = {}
    sources: dict[str, list[str]] = {}
    for name in ANSWER_REGISTERS:
        data = fetch(f"/{name}", project)
        answer, srcs = data.get("answer"), data.get("sources")
        if not isinstance(answer, str) or not isinstance(srcs, list):
            raise ValueError(f"register {name}: expected {{answer, sources}}, got keys {sorted(data)!r}")
        texts[name] = answer
        sources[name] = [str(s) for s in srcs]
    data = fetch("/recurrences", project)
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


def merge_project_candidates(
    project_order: Iterable[str],
    project_registers: dict[str, Registers],
    priority: Iterable[tuple[str, str]] = (),
) -> list[tuple[str, str, RegisterName]]:
    """One (project, subject, register) queue across every active project plus the
    unassigned bucket — reusing candidate_order per project so the advise loop's own
    per-project ordering (재발 → 위험 → 정체) is unchanged. `priority` is (project, subject)
    pairs from cross_check's 아직 set and always leads, whichever project they came from.
    A subject is deduplicated globally, not per project: the same string appearing in two
    projects' registers would otherwise spend two of the loop's eight calls on one idea."""
    seen: set[str] = set()
    out: list[tuple[str, str, RegisterName]] = []
    for project, subject in priority:
        if subject in seen:
            continue
        registers = project_registers.get(project)
        if registers is None:
            continue
        matched = candidate_order(registers, [subject])
        if not matched:
            continue
        seen.add(subject)
        out.append((project, subject, matched[0][1]))
    for project in project_order:
        registers = project_registers.get(project)
        if registers is None:
            continue
        for subject, register in candidate_order(registers):
            if subject in seen:
                continue
            seen.add(subject)
            out.append((project, subject, register))
    return out


def build_advice_prompt(subject: str, register: RegisterName, hits: list[dict[str, Any]], lang: str) -> str:
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
        f"- {_advice_lang_instruction(lang)}\n"
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
    if choice not in CHOICES:
        return Rejected(reason=f"unknown choice {choice!r}")
    if idx < 0 or idx >= n_proposals:
        return Rejected(reason=f"no proposal {idx}")
    user = (payload.get("user") or {}).get("id") or ""
    if not user:
        return Rejected(reason="no user")
    if owner_id is not None and user != owner_id:
        return Rejected(reason=f"user {user} is not the owner")
    return ButtonVerdict(idx=idx, choice=choice, user=user, at=at or datetime.now(UTC).isoformat())
