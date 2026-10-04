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

#: The review lane's fourth button — 「맡길게요」 takes the agent's own call as-is. The wire
#: admits it (parse_action_common), but only the review lane's buttons emit it and only its
#: effects know what it means; every other lane refuses it in parse_press.
DELEGATE: str = "delegate"

#: The judge a delegated review writes on the edge — the engine's `agent:<name>` vocabulary
#: (drudge/src/frontmatter.rs:53-61), accepted with no Rust change. Never `owner`: the owner
#: handed this call back, so the 채점 줄 can tell the two apart.
AGENT_DELEGATED: str = "agent:delegated"

#: The judge the 이름 맞추기 repair judgment names on its 사건 — the same `agent:<name>`
#: lineage as AGENT_DELEGATED: the agent decided before the owner's card, never the owner.
REPAIR_JUDGE: str = "agent:repair-judge"

#: Bumped whenever the judge's question changes; judgments asked under another version are
#: not read. v1 (2026-10-03) asked "same spelling?" and called 18/20 same_name, next step included.
REPAIR_JUDGE_PROMPT_VERSION: int = 2

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
    superseded_by: list[str] = []


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
    downstream recomputes it: how many candidates were tried, how many became proposals,
    why the rest were refused, how many were skipped before a call because their note
    was resting, and how often the grounding had to refetch the candidate's own note (plus
    the check's reasons whenever grounding was short of one). AC5 asks the dry-run executor
    to be able to quote a run's numbers back — before this, `NotWorth.reason` and the
    `Ungrounded` reasons were discarded the moment `advise` read them."""

    calls: int
    proposals_passed: int
    not_worth: int
    ungrounded: int
    skipped_resting: int
    refetched_own_note: int = 0  # hits missed the candidate's own note → read directly
    not_worth_reasons: list[str] = []
    ungrounded_reasons: list[str] = []
    sufficiency_reasons: list[str] = []  # why the grounding was short, in the check's words


class PastVerdictPair(BaseModel):
    """One past button press, joined back to the note+evidence its card_proposal event
    named — a card_verdict event alone carries only card_ts, idx, and choice, so the join
    happens on the read side (card.py) before this value ever reaches `suppressed`."""

    note: str
    evidence_note: str
    evidence_line: int
    choice: Choice
    at: str


class PastUnansweredPair(BaseModel):
    """One proposal the card showed but nobody judged — no card_verdict row exists for its
    (card_ts, idx). The same (note, evidence) key as PastVerdictPair; `at` is the
    card_proposal event's own observed_at, the moment the owner saw the proposal, not a
    press. Such a pair rests REST_HOURS before the card may propose it again."""

    note: str
    evidence_note: str
    evidence_line: int
    at: str


class PastCardHistory(BaseModel):
    """One read of the card's own past over a window: judged pairs (a card_verdict landed,
    미뤄 포함) and unanswered pairs (shown, never judged). Two typed lists, never one shape
    with a flag — the suppress rule (7d, 해/빼 only) and the rest rule (사흘) read different
    lists with different windows."""

    judged: list[PastVerdictPair] = []
    unanswered: list[PastUnansweredPair] = []


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
    """One button press, already judged trustworthy by parse_action. `choice` admits the
    review lane's delegate too: mark_pressed rebuilds the pressed row from this value, and
    parse_action itself (the advice-era parser) still mints only do/defer/drop."""

    idx: int
    choice: Literal["do", "defer", "drop", "delegate"]
    user: str
    at: str


class ProposedVerdict(BaseModel):
    """One session-end classification the agent already made and proposed — not the owner's.
    `session_id` is the session that judged the note (the /consumption edge's own session),
    so an owner flip judges that same session, never the card's. The card's review lane shows
    these; the owner may agree, flip, delegate, or hold. Rows whose (note, kind) matches
    another session's proposal are grouped: `sessions`/`works` are the parallel lists of every
    grouped session and its work line (`[]` on an ungrouped row), because one grouped row's
    press fans out to all of them. `reason` is the sentence the session-end scorer caught the
    mark in ("" for rows proposed before reasons were stored)."""

    session_id: str
    note: str
    kind: Literal["used", "contested"]
    at: str
    # What the owner reads to know which note and which piece of work this is: the note's own
    # title and the judging session's note ("project · MM-DD · title"); empty when the vault
    # has neither, and the row falls back to the ids.
    note_title: str = ""
    work: str = ""
    # Grouped row: every session that judged this note this way, and their work lines, both
    # in first-seen (newest-first) order, parallel to each other. Empty on an ungrouped row.
    sessions: list[str] = []
    works: list[str] = []
    # Why the agent judged so — one assistant sentence, verbatim from the transcript.
    reason: str = ""


class Rejected(BaseModel):
    """A button press that is not a verdict. A value, never an exception — the socket loop
    logs the reason and keeps listening."""

    reason: str


class CardPress(BaseModel):
    """One press carrying its own lane data — what card_view packed into the button value,
    so a process with none of the card's in-memory state can still answer it. `note` stays
    in the `wiki-NNNN` label form the card shows; effects expands it back to a path."""

    idx: int
    choice: Choice
    user: str
    card_ts: str
    channel: str


class RepairPress(CardPress):
    """The execute lane's press — its subject is all the door's merge needs."""

    lane: Literal["repair"] = "repair"
    subject: str


class AdvicePress(CardPress):
    """The advice lane's press — its note is all the consumption verdict needs."""

    lane: Literal["advice"] = "advice"
    note: str


class ReviewPress(CardPress):
    """The review lane's press — `session` is the proposing session a flip would judge;
    `sessions` is every session the row grouped (the press fans out to all of them; empty
    for an ungrouped row, where `session` alone is the list)."""

    lane: Literal["review"] = "review"
    choice: Literal["do", "delegate", "defer", "drop"]
    session: str
    sessions: list[str] = []
    note: str
    kind: Literal["used", "contested"]


#: parse_press's success return — discriminated on the value's own lane tag.
Press = Annotated[RepairPress | AdvicePress | ReviewPress, Field(discriminator="lane")]


class DelegatedJudgment(BaseModel):
    """One 「맡길게요」 press's model judgment — what the agent decided and why. `kind` is
    the judged edge kind: the model answered right (제안 그대로) or wrong (뒤집은 판정),
    and parse folded that into the engine's used|contested vocabulary before this value
    ever reached the decision table. `reason` is the model's one line, already capped."""

    kind: Literal["used", "contested"]
    reason: str


class DelegationFailed(BaseModel):
    """The model could not judge — the note was unreadable, the proposal's 근거 사건 was
    gone, the call died, or the answer was not the promised shape. No 판정 may be written
    for this press; the decision table turns this into the 사건 one line that says so."""

    reason: str


#: What a delegate press carries into card_press.effects — the model's answer or its absence.
Delegated = DelegatedJudgment | DelegationFailed


class Record(BaseModel):
    """One engine event-log row to write."""

    effect: Literal["record"] = "record"
    event: str
    fields: dict


class Consumption(BaseModel):
    """One /consumption verdict — a used|contested edge onto a session. `judge` rides the
    edge verbatim: None leaves the interpreter's owner default (a button press is the
    owner's hand); AGENT_DELEGATED names the hand the owner handed the call back to."""

    effect: Literal["consumption"] = "consumption"
    session: str
    kind: Literal["used", "contested"]
    paths: list[str]
    judge: str | None = None


class ExecuteRepair(BaseModel):
    """One door merge (POST /repairs/split-subjects) — the only effect that calls the door."""

    effect: Literal["execute_repair"] = "execute_repair"
    subject: str


#: One element of card_press.effects' answer — which collaborator, with what arguments.
Effect = Annotated[Record | Consumption | ExecuteRepair, Field(discriminator="effect")]


class PostedCard(BaseModel):
    """Where the card lives in Slack; also the engine session's identity (slack:<channel>:<ts>)."""

    channel: str
    ts: str


#: The repair judge's closed verdict vocabulary — same_name(같은 이름, 합칠 것) · generic(이름이
#: 아닌 흔한 말, 카드에서 뺄 것) · unsure(못 가름). The wire vocabulary itself, like CHOICES:
#: parse validates against this, never against a language table, so a Japanese-language
#: run folds the same three words.
REPAIR_JUDGE_VERDICTS: tuple[str, ...] = ("same_name", "generic", "unsure")


class RepairJudgment(BaseModel):
    """One 이름 맞추기 group's agent judgment — the verdict the repair-judge run left on a
    repair_judged 사건 and the card rides on its row (이유 한 줄 + 철자 목록 전부). `judge`
    names the deciding hand in the engine's agent:<name> vocabulary, like AGENT_DELEGATED —
    the owner may flip a generic call later precisely because this line is never `owner`."""

    subject: str
    variants: list[str] = Field(min_length=2)
    verdict: Literal["same_name", "generic", "unsure"]
    reason: str
    judge: str = REPAIR_JUDGE
    prompt_version: int = REPAIR_JUDGE_PROMPT_VERSION


class RepairJudgeFailed(BaseModel):
    """The repair-judge run could not judge this group — the model call died or the answer
    was not the promised shape. A value, never an exception: the 사건 carries the fact and
    the group simply is not judged, so the next day's run tries it again (a failure never
    counts as a 판정 and never blocks the rest of the day's queue)."""

    subject: str
    variants: list[str] = Field(min_length=2)
    reason: str


#: What one judge call folds into — the judgment, or the recorded fact of its absence.
RepairJudgeAnswer = RepairJudgment | RepairJudgeFailed


class Repair(BaseModel):
    """One split-subject group from the door's GET /repairs/split-subjects — the execute
    lane's row shape, distinct from Proposal (the advice lane's). `variants` holds the raw
    spellings the engine's canon() folds into `subject`; `rows`/`notes` are the door's own
    counts, shown as-is. `judgment` is the agent's 판정 for exactly this (subject, variants)
    pair — None while the repair-judge has not judged it, and the card only ever rises rows
    that carry one (a raw engine candidate must not reach the owner)."""

    subject: str
    variants: list[str] = Field(min_length=2)
    rows: int
    notes: int
    judgment: RepairJudgment | None = None


class RepairDone(BaseModel):
    """execute_repair's success value — the door's own committed counts, straight through."""

    subject: str
    deleted_rows: int
    reread_notes: int
    remaining_variants: int | None = None
    owner_held: list[str] = []


class RepairFailed(BaseModel):
    """execute_repair's failure value — the door answered and reported its own counts. A
    door-level failure (its own 502) still carries the counts of what it already committed
    before the sync call failed — those are real, not a symptom to discard, so they ride
    along here too. Never raised: F2 — a 502 or a slow sync must not end the card's whole
    run over one button."""

    subject: str
    deleted_rows: int
    reread_notes: int
    reason: str
    owner_held: list[str] = []


class RepairUnanswered(BaseModel):
    """execute_repair's failure value — the door never reported its counts (timeout,
    unreachable, non-JSON body, or JSON without count keys). The rows may already be
    deleted, so this value must not carry a count at all — writing 0 would be a lie."""

    subject: str
    reason: str


class RowRef(BaseModel):
    """Where one card row lives: the message and the button idx — what the door needs to
    settle a merge row itself once its reread has finished."""

    channel: str
    card_ts: str
    idx: int


class Pending(BaseModel):
    """A pressed row whose work has not finished."""

    outcome: Literal["pending"] = "pending"


class Done(BaseModel):
    """A pressed row whose work finished; `text` is the whole mark the row shows."""

    outcome: Literal["done"] = "done"
    text: str


class Failed(BaseModel):
    outcome: Literal["failed"] = "failed"
    reason: str


#: What a pressed row shows — folded into a status block in one place (card_view.status_text).
Outcome = Annotated[Pending | Done | Failed, Field(discriminator="outcome")]


class Registers(BaseModel):
    """The four engine registers as prompt text, and per register the allow-list a
    proposal's `source_note` must come from."""

    texts: dict[str, str]
    sources: dict[str, list[str]]
