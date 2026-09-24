#!/usr/bin/env python3
"""The morning card's value layer — no Slack, no LLM, no engine, no other card_* import.

Every shape the rest of the card modules pass between each other lives here: the register
shapes, the model's structured-output wrapper, the evidence-grounding result, the
proposal/verdict/confirmation values, and the button-vocabulary constants. Nothing here
calls out to the network or decides anything — card_registers, card_advice, card_verdicts,
and card_view each build on this layer, never on each other."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class PastVerdictPair(BaseModel):
    """One past button press, joined back to the note+evidence its card_proposal event
    named — a card_verdict event alone carries only card_ts, idx, and choice, so the join
    happens on the read side (card.py) before this value ever reaches `suppressed`."""

    note: str
    evidence_note: str
    evidence_line: int
    choice: Choice
    at: str


class PastApproved(BaseModel):
    """One 「해」 from a past card, as /approved reports it."""

    session: str
    note: str
    at: str


#: The /claim-source 404 reason — the door answered, and the subject has no current claim.
#: Every other Unresolved reason is a door failure (5xx, unreachable), which proves nothing.
#: Lives here, not in card_verdicts (is_absence's own module), because card_live's
#: _live_resolve needs the same string and card_live must not depend on card_verdicts.
NO_CURRENT_CLAIM = "no current claim for subject"


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


class ButtonVerdict(BaseModel):
    """One button press, already judged trustworthy by parse_action."""

    idx: int
    choice: Choice
    user: str
    at: str


class ProposedVerdict(BaseModel):
    """One session-end classification the agent already made and proposed — not the owner's.
    `session_id` is the session that judged the note (the /consumption edge's own session),
    so an owner flip judges that same session, never the card's. The card's review lane shows
    these; the owner may agree or flip, and silence records nothing."""

    session_id: str
    note: str
    kind: Literal["used", "contested"]
    at: str


class Rejected(BaseModel):
    """A button press that is not a verdict. A value, never an exception — the socket loop
    logs the reason and keeps listening."""

    reason: str


class PostedCard(BaseModel):
    """Where the card lives in Slack; also the engine session's identity (slack:<channel>:<ts>)."""

    channel: str
    ts: str


class Repair(BaseModel):
    """One split-subject group from the door's GET /repairs/split-subjects — the execute
    lane's row shape, distinct from Proposal (the advice lane's). `variants` holds the raw
    spellings the engine's canon() folds into `subject`; `rows`/`notes` are the door's own
    counts, shown as-is."""

    subject: str
    variants: list[str] = Field(min_length=2)
    rows: int
    notes: int


class RepairDone(BaseModel):
    """execute_repair's success value — the door's own committed counts, straight through."""

    subject: str
    deleted_rows: int
    reread_notes: int
    remaining_variants: int | None = None
    owner_held: list[str] = []


class RepairFailed(BaseModel):
    """execute_repair's failure value. A door-level failure (its own 502) still carries the
    counts of what it already committed before the sync call failed — those are real, not a
    symptom to discard, so they ride along here too. An unreachable door has nothing to
    report and both counts stay 0. Never raised: F2 — a 502 or a slow sync must not end the
    card's whole run over one button."""

    subject: str
    deleted_rows: int
    reread_notes: int
    reason: str
    owner_held: list[str] = []


class Registers(BaseModel):
    """The four engine registers as prompt text, and per register the allow-list a
    proposal's `source_note` must come from."""

    texts: dict[str, str]
    sources: dict[str, list[str]]
