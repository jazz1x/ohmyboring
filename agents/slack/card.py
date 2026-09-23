#!/usr/bin/env python3
"""The morning card — one LangGraph run: read repairs+registers, advise, resolve, post, interrupt.

`make card` runs g_card once. read_repairs calls the door for its top split-subject groups
(the execute lane's rows) and yesterday's merged-row count for the head line; read_registers
calls the door for the active-project list (14d) plus the unassigned bucket and reads each
one's four registers separately; cross_check checks the past 48h of past approvals against
the union of today's registers for the card's confirmation line (a door failure leaves an
approval unresolved — never a false "already handled"); advise walks the merged (project,
subject) queue one candidate at a time — priority pairs first — searching each subject's past
record and asking the local model (in whichever language boring.json's note_lang resolves to)
for a grounded pitch, up to three proposals or eight calls total, regardless of how many
projects were active. resolve turns each proposal's subject into its note path via
/claim-source, then drops any candidate whose (note, evidence) pair already got a verdict in
the last 7 days (`card_verdicts.suppressed`) — reading that history is not optional: a card
that cannot read it does not ship. post_card sends the Block Kit card (repair rows above the
advice rows, each grouped and labeled its own way), records a card_proposal event per
surviving proposal, and stops at an interrupt; a Slack button press resumes it with
Command(resume=…) — the verdict's idx spans all three lanes, repair rows first, then
advice rows, then the review rows. record_verdict branches by lane: a repair row's adopt
button calls the door's own merge (POST /repairs/split-subjects); an advice row's press
logs a card_verdict event — card_proposal exists only for these rows, and
card_live._live_past_verdicts joins the two by (card_ts, idx), raising on a verdict with
no proposal — then its adopt/reject writes the verdict to the engine while its hold
leaves no trace beyond the event; a review row's agree leaves only a verdict_reviewed
event, its flip writes the opposite-kind verdict to the proposing session and the same
event — the agent's own edge is never deleted. The wait for presses
ends after CARD_WAIT_HOURS regardless of how many rows are still unanswered — an
unanswered row is left exactly as it is, the same as a hold. Sent and judged stay in one
state, so the card a person answered is exactly the card the engine remembers.
CARD_DRY_RUN=1 stops right after post_card and prints the proposals instead of touching
Slack, handover, or the event log.

The socket lives here, not in the secretary: hermes can only text, and the secretary answers
questions while this file asks them. Everything decided lives in card_types/card_registers/
card_advice/card_verdicts/card_view; everything external is a collaborator (card_live, plus
this file's own _env_send/make_propose/dry-run stubs).
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from queue import Empty
from typing import Annotated, Any, NamedTuple, TypedDict

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))

import boring_config  # noqa: E402
import card_advice  # noqa: E402
import card_live  # noqa: E402
import card_registers  # noqa: E402
import card_types  # noqa: E402
import card_verdicts  # noqa: E402
import card_view  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.graph.state import CompiledStateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402

DEFAULT_MODEL = os.environ.get("CARD_MODEL") or "gemma4:12b"
# The confirmation window on the card's head line — yesterday's and the day before's approvals.
CONFIRM_SINCE_HOURS = 48
# The advise loop's call budget: one gemma4 call per candidate, at most this many candidates
# tried per run — a small local model's context outgrows a whole day's registers otherwise.
ADVISE_CALL_CAP = 8
# The project axis: registers are called once per project active in this window, plus once
# more for the unassigned bucket (project="").
PROJECT_ACTIVE_DAYS = 14


# The review lane's window — what the agent proposed over the last day.
REVIEW_SINCE_HOURS = 24


def _append_verdicts(
    existing: list[card_types.ButtonVerdict] | None, updates: list[card_types.ButtonVerdict]
) -> list[card_types.ButtonVerdict]:
    return (existing or []) + updates


_RepairResult = card_types.RepairDone | card_types.RepairFailed


def _merge_repair_results(
    existing: dict[int, _RepairResult] | None, updates: dict[int, _RepairResult]
) -> dict[int, _RepairResult]:
    return {**(existing or {}), **updates}


class CardState(TypedDict):
    lang: str
    projects: list[str]  # active projects (14d) + [""] unassigned, in call order
    project_registers: dict[str, card_types.Registers]
    per_project_candidates: dict[str, int]
    proposals: list[card_types.Proposal]
    repairs: list[card_types.Repair]  # execute lane's rows — read_repairs' top-N groups
    repairs_total_groups: int  # read_repairs' full count, for the head line
    merged_yesterday_rows: int | None  # None when nothing merged in the last 24h
    reviews: list[card_types.ProposedVerdict]  # review lane's rows — the agent's own calls
    repair_results: Annotated[
        dict[int, _RepairResult], _merge_repair_results
    ]  # repair idx → door POST result
    message: card_types.PostedCard | None
    confirmation: card_types.Confirmation | None
    priority_subjects: list[tuple[str, str]]  # (project, subject)
    verdicts: Annotated[list[card_types.ButtonVerdict], _append_verdicts]
    advise_stats: card_types.AdviseStats
    suppressed_count: int


class Collaborators(NamedTuple):
    """Everything the graph reaches outside itself. Injected so the graph runs in tests with
    no engine, no model, no door and no Slack; live defaults are the module-level makers below."""

    fetch: Callable[[str, str], dict[str, Any]]  # register path, project → engine JSON
    search: Callable[[str], list[dict[str, Any]]]  # subject → past hits (claims included)
    read_note: Callable[[str], str | None]  # note path → its own text, or None if unreadable
    propose: Callable[[str], str]  # prompt → advised JSON text (one candidate)
    send: Callable[[list[dict]], card_types.PostedCard]  # Block Kit → posted card
    handover: Callable[[str, str, list[str]], dict]  # session, at, note paths
    consumption: Callable[[str, str, list[str]], dict]  # session, used|contested, paths
    resolve: Callable[[str, str], card_types.ResolvedNote | card_types.Unresolved]
    approved: Callable[[int], list[card_types.PastApproved]]  # since_hours → past approvals
    record: Callable[[str, dict], None]  # event name, fields → engine event log
    active_projects: Callable[[int], list[str]]  # active_days → project names, doc-count desc
    past_verdicts: Callable[[int], list[card_types.PastVerdictPair]]  # since_hours → 7d 판정 pairs
    lang: str  # resolve_lang(boring_config.note_lang()) — a value, not a callable: no network
    # Defaulted (unlike everything above): every existing caller that never heard of the
    # repair lane keeps working unchanged. repairs(limit) is the door's GET; execute_repair
    # is its POST; merged_yesterday is the head line's optional "merged m rows yesterday";
    # proposed is the review lane's engine read — defaulting to none keeps every pre-lane
    # caller rendering exactly the card it rendered before.
    repairs: Callable[[int], dict[str, Any]] = lambda limit: {"groups": [], "total_groups": 0}
    execute_repair: Callable[[str], _RepairResult] = lambda subject: card_types.RepairFailed(
        subject=subject, deleted_rows=0, reread_notes=0, reason="no repair collaborator configured"
    )
    merged_yesterday: Callable[[], int | None] = lambda: None
    proposed: Callable[[int], list[card_types.ProposedVerdict]] = lambda since_hours: []


def session_name(card: card_types.PostedCard) -> str:
    """The engine's name for this card — the same key /handover wrote and /consumption judges."""
    return f"slack:{card.channel}:{card.ts}"


def build_graph(collabs: Collaborators | None = None) -> CompiledStateGraph:
    if collabs is None:
        collabs = Collaborators(
            fetch=card_live._live_fetch,
            search=card_live._live_search,
            read_note=card_live._live_read_note,
            propose=make_propose(),
            send=_env_send,
            handover=card_live._live_handover,
            consumption=card_live._live_consumption,
            resolve=card_live._live_resolve,
            approved=card_live._live_approved,
            record=card_live._live_record,
            active_projects=card_live._live_active_projects,
            past_verdicts=card_live._live_past_verdicts,
            lang=card_advice.resolve_lang(boring_config.note_lang()),
            repairs=card_live._live_repairs,
            execute_repair=card_live._live_execute_repair,
            merged_yesterday=card_live._live_merged_yesterday,
            proposed=card_live._live_proposed,
        )

    def read_repairs(_: CardState) -> dict:
        """The execute lane's rows — the door's own top groups, plus yesterday's merged-row
        tally for the head line. A 5xx/unreachable door raises (same principle as /approved):
        a card that cannot read its own repair queue is the wrong card to send."""
        payload = collabs.repairs(card_live.REPAIRS_LIMIT)
        repairs = [card_types.Repair(**group) for group in payload["groups"]]
        return {
            "repairs": repairs,
            "repairs_total_groups": payload["total_groups"],
            "merged_yesterday_rows": collabs.merged_yesterday(),
        }

    def read_proposed(_: CardState) -> dict:
        """The review lane's rows — what the agent classified at session end over the last
        day. A dead engine raises here the same way an unreadable judged history does in
        resolve: a card that cannot read the proposals it would ask the owner to flip is
        the wrong card to send."""
        return {"reviews": collabs.proposed(REVIEW_SINCE_HOURS)}

    def read_registers(_: CardState) -> dict:
        active = collabs.active_projects(PROJECT_ACTIVE_DAYS)
        projects = [*active, ""]
        project_registers = {p: card_registers.collect_registers(collabs.fetch, p) for p in projects}
        return {"lang": collabs.lang, "projects": projects, "project_registers": project_registers}

    #: subject → resolution, shared by cross_check and resolve so the door is asked once
    #: per subject per run.
    resolve_cache: dict[tuple[str, str], card_types.ResolvedNote | card_types.Unresolved] = {}

    def _resolve(subject: str, register: str) -> card_types.ResolvedNote | card_types.Unresolved:
        key = (subject, register)
        if key not in resolve_cache:
            resolve_cache[key] = collabs.resolve(subject, register)
        return resolve_cache[key]

    def cross_check(state: CardState) -> dict:
        """The card's head line, before the model picks: yesterday's 「해」, each checked
        against whether its note path still resolves from today's registers — now the union
        across every active project plus the unassigned bucket, since a past approval does
        not remember which project's register it came from. A 404 establishes absence; a
        door failure (5xx·불통) leaves the approval unknown — the card never reads a dead
        door as 「했다」. A dead /approved is not a value either: the exception stops the
        run, and main exits 3 without posting."""
        project_registers = state["project_registers"]
        past = collabs.approved(CONFIRM_SINCE_HOURS)
        if not past:
            return {"confirmation": None, "priority_subjects": []}
        today_notes: set[str] = set()
        note_to_pick: dict[str, tuple[str, str]] = {}
        failures: list[str] = []
        for project, registers in project_registers.items():
            for name in card_types.ANSWER_REGISTERS:
                for subject in registers.sources.get(name, []):
                    result = _resolve(subject, name)
                    if isinstance(result, card_types.ResolvedNote):
                        today_notes.add(result.note)
                        note_to_pick.setdefault(result.note, (project, subject))
                    elif not card_verdicts.is_absence(result.reason):
                        failures.append(result.reason)
            for path in registers.sources.get("recurrences", []):
                today_notes.add(path)
                note_to_pick.setdefault(path, (project, path))
        confirmation = card_verdicts.confirm_past(past, today_notes, failures)
        priority = sorted({note_to_pick[note] for note in confirmation.pending if note in note_to_pick})
        return {"confirmation": confirmation, "priority_subjects": priority}

    def advise(state: CardState) -> dict:
        """The bottleneck pick, one candidate at a time (project+subject in, its register's
        search hits and gemma4's grounded pitch or refusal out) — up to three proposals, at
        most ADVISE_CALL_CAP calls, shared across every active project so the call budget
        does not scale with how many projects were active this window. A candidate that
        fails to ground (schema, quote, or NotWorth) is not an error: it just does not
        become a proposal, and the queue moves on — but its reason is kept in advise_stats,
        not discarded, so a dry run can quote why."""
        candidates = card_registers.merge_project_candidates(
            state["projects"], state["project_registers"], state["priority_subjects"]
        )
        per_project_candidates: dict[str, int] = {}
        for project, _subject, _register in candidates:
            per_project_candidates[project] = per_project_candidates.get(project, 0) + 1
        proposals: list[card_types.Proposal] = []
        not_worth_reasons: list[str] = []
        ungrounded_reasons: list[str] = []
        calls = 0
        warned_empty_vault = False
        for project, subject, register in candidates:
            if len(proposals) >= 3 or calls >= ADVISE_CALL_CAP:
                break
            hits = collabs.search(subject)
            note_texts = _note_texts_for_hits(collabs.read_note, hits)
            if hits and not note_texts and not warned_empty_vault:
                # Search had something to say about this subject, but every note it pointed
                # at came back unreadable — that is not the same as "no evidence exists" (a
                # wrong BORING_VAULT_DIR looks identical to a quiet morning otherwise).
                print(
                    f"[card] 검색 결과 {len(hits)}건은 있었지만 노트 본문을 하나도 못 읽었다 — "
                    f"BORING_VAULT_DIR={card_live._vault_dir()!r} 확인",
                    file=sys.stderr,
                )
                warned_empty_vault = True
            prompt = card_advice.build_advice_prompt(subject, register, hits, state["lang"])
            calls += 1
            advised = card_advice.parse_advised(collabs.propose(prompt), note_texts)
            if isinstance(advised, card_types.Advice):
                proposals.append(
                    card_types.Proposal(
                        subject=subject,
                        register=register,
                        project=project,
                        bottleneck=advised.bottleneck,
                        advice=advised.advice,
                        evidence=advised.evidence,
                    )
                )
            elif isinstance(advised, card_types.NotWorth):
                not_worth_reasons.append(advised.reason)
            else:
                ungrounded_reasons.append(advised.reason)
        return {
            "proposals": proposals,
            "per_project_candidates": per_project_candidates,
            "advise_stats": card_types.AdviseStats(
                calls=calls,
                proposals_passed=len(proposals),
                not_worth=len(not_worth_reasons),
                ungrounded=len(ungrounded_reasons),
                not_worth_reasons=not_worth_reasons,
                ungrounded_reasons=ungrounded_reasons,
            ),
        }

    def resolve(state: CardState) -> dict:
        """subject → note path through the door, for whatever advise picked. A subject that
        does not resolve is dropped as a value — 「근거 노트 없음」 never rides a card. Fewer
        than three, even zero, ships: NotWorth and an unresolved subject are both legitimate
        answers now (실험 1's recall precision was 1/5), not a reason to refuse the card.
        A resolved candidate is then run through the 7-day 판정 suppression: past_verdicts
        is read unconditionally, even when there is nothing left to suppress — a card that
        cannot read its own judged history is the wrong card to send (같은 원칙: /approved)."""
        picked: list[card_types.Proposal] = []
        seen_notes: set[str] = set()
        for proposal in state["proposals"]:
            result = _resolve(proposal.subject, proposal.register_)
            if isinstance(result, card_types.Unresolved):
                continue
            if result.note in seen_notes:
                continue
            seen_notes.add(result.note)
            picked.append(proposal.model_copy(update={"note": result.note}))
        past = collabs.past_verdicts(card_verdicts.SUPPRESS_WINDOW_HOURS)
        kept, dropped = card_verdicts.suppressed(picked, past)
        return {"proposals": kept, "suppressed_count": len(dropped)}

    def post_card(state: CardState) -> dict:
        lang = state["lang"]
        n_repairs = len(state["repairs"])
        message = collabs.send(
            card_view.build_blocks(
                state["proposals"],
                confirmation=state["confirmation"],
                repairs=state["repairs"],
                repairs_total_groups=state["repairs_total_groups"],
                merged_yesterday_rows=state["merged_yesterday_rows"],
                reviews=state["reviews"],
                lang=lang,
            )
        )
        card_ts = message.ts
        collabs.handover(
            session_name(message),
            datetime.now(UTC).isoformat(),
            card_verdicts.handover_paths(state["proposals"]),
        )
        for idx, proposal in enumerate(state["proposals"]):
            # global idx (repairs first) — record_verdict's later card_verdict event carries
            # the same button idx, and _live_past_verdicts joins the two events on it.
            collabs.record(
                "card_proposal", card_verdicts.proposal_event_fields(proposal, lang, card_ts, n_repairs + idx)
            )
        confirmation = state["confirmation"]
        if confirmation is not None:
            fields: dict[str, Any] = {
                "done": len(confirmation.done),
                "pending": len(confirmation.pending),
                "session": confirmation.session,
            }
            if confirmation.unknown:
                fields["unknown"] = len(confirmation.unknown)
            collabs.record("card_confirmation", fields)
        return {"message": message}

    def await_verdict(state: CardState) -> dict:
        total_rows = len(state["proposals"]) + len(state["repairs"]) + len(state["reviews"])
        verdict = interrupt({"judged": len(state["verdicts"]), "of": total_rows})
        return {"verdicts": [verdict]}

    def record_verdict(state: CardState) -> dict:
        verdict = state["verdicts"][-1]
        n_repairs = len(state["repairs"])
        if verdict.idx < n_repairs:
            # the execute lane: adopt calls the door's own merge; hold/reject leave no
            # trace at all — there is nothing to suppress in this lane.
            if verdict.choice != "do":
                return {}
            result = collabs.execute_repair(state["repairs"][verdict.idx].subject)
            return {"repair_results": {verdict.idx: result}}
        n_proposals = len(state["proposals"])
        if verdict.idx < n_repairs + n_proposals:
            # card_verdict is recorded only here, in the advice lane: card_proposal events
            # exist only for these rows, and _live_past_verdicts joins the two by
            # (card_ts, idx) — a press in any other lane would orphan that join and the
            # next morning's card would raise on it and refuse to ship.
            collabs.record("card_verdict", card_verdicts.verdict_event_fields(verdict, state["message"].ts))
            if verdict.choice == "defer":
                return {}  # a card_verdict event, but no consumption call — never suppresses
            kind = "used" if verdict.choice == "do" else "contested"
            note = state["proposals"][verdict.idx - n_repairs].note
            collabs.consumption(session_name(state["message"]), kind, [note])
            return {}
        # the review lane: the agent proposed, the owner may only agree or flip. Agreeing
        # records nothing but the review event — no edge, silence ≠ agreement elsewhere too.
        # Flipping judges the proposing session with the opposite kind: the agent's own edge
        # stays in the graph (an agent 표 and an owner 표, side by side, by design).
        review = state["reviews"][verdict.idx - n_repairs - n_proposals]
        fields: dict[str, Any] = {
            "session_id": review.session_id,
            "note": review.note,
            "proposed_kind": review.kind,
            "card_ts": state["message"].ts,
        }
        if verdict.choice == "do":
            collabs.record("verdict_reviewed", {**fields, "choice": "agree"})
            return {}
        collabs.consumption(
            review.session_id, "contested" if review.kind == "used" else "used", [review.note]
        )
        collabs.record("verdict_reviewed", {**fields, "choice": "flip"})
        return {}

    graph = StateGraph(CardState)
    graph.add_node("read_repairs", read_repairs)
    graph.add_node("read_proposed", read_proposed)
    graph.add_node("read_registers", read_registers)
    graph.add_node("cross_check", cross_check)
    graph.add_node("advise", advise)
    graph.add_node("resolve", resolve)
    graph.add_node("post_card", post_card)
    graph.add_node("await_verdict", await_verdict)
    graph.add_node("record_verdict", record_verdict)
    graph.add_edge(START, "read_repairs")
    graph.add_edge("read_repairs", "read_proposed")
    graph.add_edge("read_proposed", "read_registers")
    graph.add_edge("read_registers", "cross_check")
    graph.add_edge("cross_check", "advise")
    graph.add_edge("advise", "resolve")
    graph.add_edge("resolve", "post_card")
    graph.add_edge("post_card", "await_verdict")
    graph.add_edge("await_verdict", "record_verdict")
    graph.add_conditional_edges(
        "record_verdict",
        lambda state: (
            "await_verdict"
            if len(state["verdicts"])
            < len(state["proposals"]) + len(state["repairs"]) + len(state["reviews"])
            else END
        ),
    )
    return graph.compile(checkpointer=MemorySaver())


def _note_texts_for_hits(
    read_note: Callable[[str], str | None], hits: list[dict[str, Any]]
) -> dict[str, str]:
    out: dict[str, str] = {}
    for hit in hits:
        note = hit.get("source_path")
        if not note or note in out:
            continue
        text = read_note(note)
        if text is not None:
            out[note] = text
    return out


def _env_send(blocks: list[dict]) -> card_types.PostedCard:
    from slack_sdk.web import WebClient

    channel = os.environ["SLACK_CARD_CHANNEL"]  # main() validated before the graph runs
    web = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))
    resp = web.chat_postMessage(channel=channel, blocks=blocks)
    return card_types.PostedCard(channel=channel, ts=resp["ts"])


def make_propose(model: str = DEFAULT_MODEL) -> Callable[[str], str]:
    """The JSON-mode seam: one candidate's prompt in, raw completion text out. `format="json"`
    keeps gemma4 to syntactically valid JSON; the schema itself is only ever enforced at
    parse_advised, the one boundary built and tested to treat a malformed or partial
    completion as a value (Ungrounded), not an exception. A stricter LangChain
    with_structured_output(AdvisedInput) was tried first and raised on live gemma4: a real
    NotWorth answer that omitted the `kind` default failed a schema LangChain validates
    before parse_advised ever sees it — two boundaries disagreeing on the same JSON is the
    defect, not gemma4's output. `reasoning=False` — gemma4 is a thinking variant and the
    thinking is latency with no pick to show for it."""

    from langchain_core.runnables import Runnable
    from langchain_ollama import ChatOllama

    llm: Runnable = ChatOllama(model=model, format="json", temperature=0, reasoning=False, num_ctx=16384)

    def propose(prompt: str) -> str:
        return str(llm.invoke(prompt).content)

    return propose


def _lock_path(app_token: str) -> str:
    """One lock file per Slack app token, in /tmp — the same path every run computes from the
    same token, so a second concurrent card.py on that token always finds the first one's
    lock. Hashed rather than the raw token: this path can show up in an error message."""
    import hashlib

    digest = hashlib.sha256(app_token.encode("utf-8")).hexdigest()[:16]
    return f"/tmp/card-{digest}.lock"


def _acquire_single_instance_lock(app_token: str):
    """Refuse a second live card on the same app token — two Socket Mode clients would both
    receive every button press (F7: only CARD_WAIT_HOURS < 24h keeping the daily cadence from
    overlapping was not a guard, just a coincidence). Returns (open file handle, None) on
    success — keep it open for the process lifetime, closing releases the advisory lock — or
    (None, holder pid) when another instance already holds it."""
    import fcntl

    path = _lock_path(app_token)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        holder_pid = fh.read().strip() or "unknown"
        fh.close()
        return None, holder_pid
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh, None


class _Holder:
    """What the socket listener and the main loop share: the posted card, the repairs and
    proposals (idx space: repairs first), the presses already judged, and a queue of
    verdicts waiting to resume the graph."""

    def __init__(self) -> None:
        from queue import Queue

        self.queue: Queue[card_types.ButtonVerdict] = Queue()
        self.message: card_types.PostedCard | None = None
        self.repairs: list[card_types.Repair] = []
        self.proposals: list[card_types.Proposal] = []
        self.reviews: list[card_types.ProposedVerdict] = []
        self.judged: set[int] = set()


def _listener(holder: _Holder, owner_id: str | None) -> Callable:
    def on_request(client, req) -> None:
        try:
            from slack_sdk.socket_mode.response import SocketModeResponse

            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception as e:  # noqa: BLE001 — an unacked envelope is retried; a dead loop is not
            print(f"[card] ack failed: {e}", file=sys.stderr)
            return
        if getattr(req, "type", None) != "interactive":
            return
        payload = getattr(req, "payload", None) or {}
        if payload.get("type") != "block_actions":
            return
        if holder.message is None:
            print("[card] button before card — ignored", file=sys.stderr)
            return
        if (payload.get("message") or {}).get("ts") != holder.message.ts:
            return
        n_total = len(holder.repairs) + len(holder.proposals) + len(holder.reviews)
        verdict = card_verdicts.parse_action(payload, owner_id=owner_id, n_total=n_total)
        if isinstance(verdict, card_types.Rejected):
            print(f"[card] rejected: {verdict.reason}", file=sys.stderr)
            return
        if verdict.idx in holder.judged:
            print(f"[card] proposal {verdict.idx} already judged — ignored", file=sys.stderr)
            return
        holder.queue.put(verdict)

    return on_request


def _await_verdicts(
    holder: _Holder,
    graph: CompiledStateGraph,
    config: dict,
    state: dict,
    web_client,
    wait_hours: float,
) -> dict:
    """Resume the graph with each verdict as Slack delivers it, editing the card in place —
    until every proposal is judged or `wait_hours` has passed since this call started. A
    proposal still unanswered at that point is left exactly as it is, the same as 미뤄: this
    card's life is over once the next one is due, not once someone gets around to it."""
    deadline = time.monotonic() + wait_hours * 3600
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            verdict = holder.queue.get(timeout=remaining)
        except Empty:
            break
        holder.judged.add(verdict.idx)
        state = graph.invoke(Command(resume=verdict), config)
        web_client.chat_update(
            channel=holder.message.channel,
            ts=holder.message.ts,
            blocks=card_view.build_blocks(
                state["proposals"],
                state["verdicts"],
                confirmation=state["confirmation"],
                repairs=state["repairs"],
                repairs_total_groups=state["repairs_total_groups"],
                merged_yesterday_rows=state["merged_yesterday_rows"],
                repair_results=state["repair_results"],
                reviews=state["reviews"],
                lang=state["lang"],
            ),
        )
        if "__interrupt__" not in state:
            break
    unanswered = (
        len(state["proposals"]) + len(state["repairs"]) + len(state["reviews"]) - len(state["verdicts"])
    )
    if unanswered > 0:
        print(f"[card] 대기 끝 — 안 누른 제안 {unanswered}건은 그대로 둔다", file=sys.stderr)
    return state


def _dry_send(blocks: list[dict]) -> card_types.PostedCard:
    return card_types.PostedCard(channel="dry-run", ts="0")


def _dry_handover(session: str, at: str, paths: list[str]) -> dict:
    return {}


def _dry_record(event: str, fields: dict) -> None:
    return None


def _run_dry() -> int:
    """CARD_DRY_RUN=1: run the graph up through post_card with everything live except Slack,
    handover, and the event log — print the proposals and stop before the interrupt. No
    send, no handover, no /consumption, no card_proposal/card_confirmation event — only
    stdout. active_projects and past_verdicts stay live: both are reads (GET /projects,
    GET /events), not writes, so the dry run's projects_called and suppression numbers are
    the real ones the next live card would see."""
    collabs = Collaborators(
        fetch=card_live._live_fetch,
        search=card_live._live_search,
        read_note=card_live._live_read_note,
        propose=make_propose(),
        send=_dry_send,
        handover=_dry_handover,
        consumption=card_live._live_consumption,
        resolve=card_live._live_resolve,
        approved=card_live._live_approved,
        record=_dry_record,
        active_projects=card_live._live_active_projects,
        past_verdicts=card_live._live_past_verdicts,
        lang=card_advice.resolve_lang(boring_config.note_lang()),
        repairs=card_live._live_repairs,
        merged_yesterday=card_live._live_merged_yesterday,
        proposed=card_live._live_proposed,
    )
    graph = build_graph(collabs)
    config = {"configurable": {"thread_id": f"card-dry-{datetime.now(UTC):%Y%m%d%H%M%S}"}}
    state = graph.invoke({"verdicts": []}, config)
    confirmation = state["confirmation"]
    stats = state["advise_stats"]
    print(
        json.dumps(
            {
                "count": len(state["proposals"]),
                "proposals": [p.model_dump(by_alias=True) for p in state["proposals"]],
                "confirmation": confirmation.model_dump() if confirmation else None,
                "projects_called": len(state["projects"]),
                "per_project_candidates": state["per_project_candidates"],
                "calls": stats.calls,
                "proposals_passed": stats.proposals_passed,
                "not_worth": stats.not_worth,
                "ungrounded": stats.ungrounded,
                "not_worth_reasons": stats.not_worth_reasons,
                "ungrounded_reasons": stats.ungrounded_reasons,
                "suppressed": state["suppressed_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> int:
    app_token = os.environ.get("SLACK_APP_TOKEN")
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not app_token or not bot_token:
        print(
            "[card] SLACK_APP_TOKEN and SLACK_BOT_TOKEN must be set (see .env.example)",
            file=sys.stderr,
        )
        return 2
    if not os.environ.get("SLACK_CARD_CHANNEL"):
        print(
            "[card] SLACK_CARD_CHANNEL must be set — the channel id the morning card posts to "
            "(see .env.example)",
            file=sys.stderr,
        )
        return 2
    if not os.environ.get("BORING_DOOR_URL"):
        print(
            "[card] BORING_DOOR_URL must be set — the card cannot attach 판정 to notes without "
            "the door's /claim-source, and cannot cross-check past approvals without /approved "
            "(see .env.example)",
            file=sys.stderr,
        )
        return 2
    owner_id = os.environ.get("SECRETARY_OWNER_ID") or None
    if owner_id is None:
        print("[card] no SECRETARY_OWNER_ID — any user may judge", file=sys.stderr)

    if os.environ.get("CARD_DRY_RUN"):
        return _run_dry()

    lock_fh, holder_pid = _acquire_single_instance_lock(app_token)
    if lock_fh is None:
        print(
            f"[card] another card is already running (pid {holder_pid}) — refusing a second "
            "Socket Mode listener on the same app token",
            file=sys.stderr,
        )
        return 2

    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.web import WebClient

    web_client = WebClient(token=bot_token)
    try:
        web_client.auth_test()
    except Exception as e:  # noqa: BLE001 — say why in one line, not with a stack trace and no token
        print(f"[card] auth_test failed: {e}", file=sys.stderr)
        lock_fh.close()
        return 1

    holder = _Holder()
    client = SocketModeClient(app_token=app_token, web_client=web_client)
    client.socket_mode_request_listeners.append(_listener(holder, owner_id))
    try:
        client.connect()
        graph = build_graph()
        config = {"configurable": {"thread_id": f"card-{datetime.now(UTC):%Y%m%d}"}}
        try:
            state = graph.invoke({"verdicts": []}, config)
            holder.message = state["message"]
            holder.repairs = state["repairs"]
            holder.proposals = state["proposals"]
            holder.reviews = state["reviews"]
            _await_verdicts(holder, graph, config, state, web_client, card_live.CARD_WAIT_HOURS)
        except KeyboardInterrupt:
            print("[card] 결재 대기를 멈춘다 — 카드는 슬랙에 남아 있다.", file=sys.stderr)
        except (ValueError, OSError) as e:
            # A malformed register, an ungrounded proposal, a dead door, a refused resolve —
            # say why in one line and stop. OSError covers URLError: the door was the only
            # road to /claim-source and /approved, and a card that cannot confirm is a card
            # that must not ship quietly.
            print(f"[card] 카드 거부: {' '.join(str(e).split())}", file=sys.stderr)
            return 3
    finally:
        client.close()
        lock_fh.close()  # releases the flock — the file itself stays, harmlessly, for next time
    return 0


if __name__ == "__main__":
    sys.exit(main())
