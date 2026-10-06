#!/usr/bin/env python3
"""The morning card — one LangGraph run: read repairs+registers, advise, resolve, post.

`make card` runs g_card once: post the card, print its ts, exit. read_repairs scans the
door's top split-subject groups and keeps only the ones the agent already judged 같은 이름
or 못 가름 — generic, unjudged, and held groups never reach the owner — plus yesterday's
merged-row count for the head line; read_registers calls the door for the active-project
list (14d) plus the unassigned bucket and reads each one's four registers separately;
cross_check checks the past 48h of past approvals against the union of today's registers
for the card's confirmation
line (a door failure leaves an approval unresolved — never a false "already handled"); advise
walks the merged (project, subject) queue one candidate at a time — priority pairs first —
searching each subject's past record, with a sufficiency check that refetches the candidate's
own note when the search missed it (a recurrence's path search returns unrelated notes only),
and asking the local model (in whichever language boring.json's note_lang resolves to)
for a grounded pitch, up to three proposals or eight calls total, regardless of how many
projects were active. read_history reads the card's own judged history once, before advise,
and carries it in state; advise skips a candidate with no search and no model call when its
note is resting — shown but unanswered in the last 72h, or 해/빼-judged in the last 7 days.
resolve turns each surviving proposal's subject into its note path via
/claim-source, then drops any candidate whose (note, evidence) pair already got a verdict in
the last 7 days or was shown but never judged in the last 3 days (`card_verdicts.suppressed`)
— reading that history is not optional: a card that cannot read it does not ship. post_card
sends the Block Kit card (repair rows above the advice rows, each grouped and labeled its own
way), records a card_proposal event per advice row the card showed (plus card_fit), and ends the run. The verdict's
idx spans all three lanes, repair rows first, then advice rows, then the review rows; the
button press carrying it never reaches this process — hermes owns the only socket (wiki-2049)
and its boring-card plugin answers the press: card_press.parse_press parses it,
card_press.effects is the one decision table, and card_effects.run folds it through the same
live collaborators — a repair row's adopt calls the door's own merge (POST
/repairs/split-subjects); an advice row's press logs a card_verdict event — card_proposal
exists only for these rows, and card_live._live_past_verdicts joins the two by (card_ts, idx),
raising on a verdict with no proposal — then its adopt/reject writes the verdict to the
engine while its hold leaves no trace beyond the event; a review row's agree leaves only a
verdict_reviewed event, its flip writes the opposite-kind verdict to the proposing session
and the same event — the agent's own edge is never deleted. An unanswered row stays
answerable for card_press.CARD_ANSWERABLE_HOURS: the hermes plugin refuses an older press,
and _live_past_verdicts widens its proposal read by the same hours so every press it
accepted still joins its proposal.
CARD_DRY_RUN=1 stops right after post_card and prints the proposals instead of touching
Slack, handover, or the event log. CARD_RENDER_ONLY=1 draws today's card from the same
reads and prints its blocks and size, writing nothing and never reaching /search or the
model — the way to see a card's layout without polluting what the card measures.

During the launchd→hermes handover both schedulers fire the same tool for a while: the
second runner of one KST morning reads the first card's own events (card_fit /
card_confirmation / card_proposal / verdict_sample_shown — no new state file) and exits 0 with one line, "already posted today
(ts=…)", instead of posting a second card.

The socket lives in hermes, not here. Everything decided lives in
card_types/card_registers/card_advice/card_verdicts/card_view/card_press; everything
external is a collaborator (card_live, plus this file's own _env_send/make_propose/dry-run
stubs).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, NamedTuple, TypedDict

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "src"))

import card_advice  # noqa: E402
import card_live  # noqa: E402
import card_press  # noqa: E402
import card_registers  # noqa: E402
import card_types  # noqa: E402
import card_verdicts  # noqa: E402
import card_view  # noqa: E402
from langgraph.graph import START, StateGraph  # noqa: E402
from langgraph.graph.state import CompiledStateGraph  # noqa: E402

from ohmyboring import config as boring_config  # noqa: E402
from ohmyboring.adapters import events as event_log  # noqa: E402
from ohmyboring.adapters import slack as slack_post  # noqa: E402
from ohmyboring.i18n import card as card_i18n  # noqa: E402

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

_KST = timezone(timedelta(hours=9))

#: The card's own ledger: a posted card leaves card_confirmation (one per card, naming its
#: card_ts), a card_fit (one per card, always — the only trace a card whose advice rows were
#: all left out for size leaves when it also had no samples or confirmation), a card_proposal
#: per advice row it showed, and a verdict_sample_shown per 확인용 표본. The once-a-day guard
#: reads exactly these — no new state file.
_CARD_LEDGER_EVENTS = ("card_proposal", "card_confirmation", "verdict_sample_shown", "card_fit")


def _parse_event_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _posted_today_ts(now: datetime | None = None) -> str | None:
    """The ts of the card already posted today (KST), from the card's own events — or None
    when this morning is still unposted. launchd and the hermes cron both fire the same tool
    during the handover; whichever runner goes second sees this and exits 0 instead of
    posting a second card. Every posted card records card_fit with its card_ts, so even one
    with no advice rows, no confirmation and no samples is seen; a card that showed samples
    also leaves verdict_sample_shown, so the daily sample cap holds. An unreadable event log is never a
    reason to skip a morning: the graph's own reads refuse loudly when the engine is
    truly down, so a dead read here just means the guard stays blind, silent, and out of
    the way."""
    now = now or datetime.now(UTC)
    today = now.astimezone(_KST).date()
    newest_at: datetime | None = None
    newest_ts: str | None = None
    for name in _CARD_LEDGER_EVENTS:
        for event in event_log.recent_events(50, event_name=name):
            at = _parse_event_ts(event.get("ts"))
            ts = event.get("card_ts")
            if at is None or not ts or at.astimezone(_KST).date() != today:
                continue
            if newest_at is None or at > newest_at:
                newest_at = at
                newest_ts = str(ts)
    return newest_ts


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
    score: card_types.Score | None  # 채점 줄 — None when the collaborator was never asked
    samples: list[card_types.ProposedVerdict]  # today's 확인용 무작위 표본, before the size budget
    message: card_types.PostedCard | None
    confirmation: card_types.Confirmation | None
    priority_subjects: list[tuple[str, str]]  # (project, subject)
    past_verdicts: card_types.PastCardHistory  # read once in read_history, shared by advise and resolve
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
    resolve: Callable[[str, str], card_types.ResolvedNote | card_types.Unresolved]
    approved: Callable[[int], list[card_types.PastApproved]]  # since_hours → past approvals
    record: Callable[[str, dict], None]  # event name, fields → engine event log
    active_projects: Callable[[int], list[str]]  # active_days → project names, doc-count desc
    past_verdicts: Callable[[int], card_types.PastCardHistory]  # since_hours → judged + unanswered pairs
    lang: str  # resolve_lang(boring_config.note_lang()) — a value, not a callable: no network
    # Defaulted (unlike everything above): every existing caller that never heard of the
    # repair lane keeps working unchanged. repairs(limit) is the door's GET;
    # merged_yesterday is the head line's optional "merged m rows yesterday"; proposed is
    # the review lane's engine read — defaulting to none keeps every pre-lane caller
    # rendering exactly the card it rendered before. repair_judgments is the lane's agent
    # judgments keyed by (subject, 정렬된 variants) — defaulting to none renders an empty
    # lane instead of crashing a caller that never heard of 판정.
    repairs: Callable[[int], dict[str, Any]] = lambda limit: {"groups": [], "total_groups": 0}
    merged_yesterday: Callable[[], int | None] = lambda: None
    proposed: Callable[[int], list[card_types.ProposedVerdict]] = lambda since_hours: []
    held_repairs: Callable[[], set[str]] = lambda: set()
    repair_judgments: Callable[[], dict[tuple[str, tuple[str, ...]], card_types.RepairJudgment]] = lambda: {}
    # (today's review rows, today KST) → the owner-press tally and the day's 확인용 표본;
    # defaulting to "nobody asked" draws no 채점 줄 and no sample.
    score: Callable[[list[card_types.ProposedVerdict], date], card_types.ScoreReading] = lambda reviews, day: (
        card_types.ScoreReading()
    )
    # CARD_RENDER_ONLY: when set, the advice lane is today's already-posted proposals — no
    # search, no model call, no suppression — instead of a fresh advise → resolve.
    posted_proposals: Callable[[], list[card_types.Proposal]] | None = None


def build_graph(collabs: Collaborators | None = None) -> CompiledStateGraph:
    if collabs is None:
        collabs = Collaborators(
            fetch=card_live._live_fetch,
            search=card_live._live_search,
            read_note=card_live._live_read_note,
            propose=make_propose(),
            send=_env_send,
            handover=card_live._live_handover,
            resolve=card_live._live_resolve,
            approved=card_live._live_approved,
            record=card_live._live_record,
            active_projects=card_live._live_active_projects,
            past_verdicts=card_live._live_past_verdicts,
            lang=card_advice.resolve_lang(boring_config.note_lang()),
            repairs=card_live._live_repairs,
            merged_yesterday=card_live._live_merged_yesterday,
            proposed=card_live._live_proposed,
            held_repairs=card_live._held_repair_subjects,
            repair_judgments=card_live._live_repair_judgments,
            score=card_live._live_score,
        )

    def read_repairs(_: CardState) -> dict:
        """The execute lane's rows — the door's 상한(REPAIRS_SCAN_LIMIT)만큼 받아 에이전트
        판정으로 거른다: generic·판정 없음·보류(held)는 빠지고, 같은 이름·못 가름만 이유 한
        줄과 철자 목록을 달고 오른다. 거른 뒤 3개가 안 차도 실패가 아니다 — 빈 칸이면 칸이
        안 그려지는 기존 동작 그대로다. A 5xx/unreachable door raises (same principle as
        /approved): a card that cannot read its own repair queue is the wrong card to send."""
        held = collabs.held_repairs()
        payload = collabs.repairs(card_live.REPAIRS_SCAN_LIMIT)
        judged = collabs.repair_judgments()
        repairs: list[card_types.Repair] = []
        for group in payload["groups"]:
            if len(repairs) >= card_live.REPAIRS_LIMIT:
                break
            if group["subject"] in held:
                continue
            judgment = judged.get((group["subject"], tuple(sorted(group["variants"]))))
            if judgment is None or judgment.verdict == "generic":
                continue
            repairs.append(card_types.Repair(**group, judgment=judgment))
        return {
            "repairs": repairs,
            "repairs_total_groups": payload["total_groups"],
            "merged_yesterday_rows": collabs.merged_yesterday(),
        }

    def read_proposed(_: CardState) -> dict:
        """The review lane's rows — what the agent classified at session end over the last
        day. A dead engine raises here the same way an unreadable judged history does in
        resolve: a card that cannot read the proposals it would ask the owner to flip is
        the wrong card to send. The 채점 줄 and the 확인용 표본 ride the same read: the sample
        draw needs today's review rows to leave them out, and an unreadable tally is a value
        the card prints, not a reason to hold the card."""
        reviews = collabs.proposed(REVIEW_SINCE_HOURS)
        reading = collabs.score(reviews, datetime.now(_KST).date())
        return {"reviews": reviews, "score": reading.score, "samples": reading.samples}

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

    def read_history(_: CardState) -> dict:
        """The card's own judged history over the suppression window — read once, carried in
        state for advise's resting-note skip and resolve's suppression. The read is not
        optional: a failing one raises and stops the run (같은 원칙: /approved) — a card
        that cannot read its own judged history is the wrong card to send."""
        return {"past_verdicts": collabs.past_verdicts(card_verdicts.SUPPRESS_WINDOW_HOURS)}

    def advise(state: CardState) -> dict:
        """The bottleneck pick, one candidate at a time (project+subject in, its register's
        search hits and gemma4's grounded pitch or refusal out) — up to three proposals, at
        most ADVISE_CALL_CAP calls, shared across every active project so the call budget
        does not scale with how many projects were active this window. Before any search
        or call, a candidate whose note is resting — shown but unanswered within
        REST_HOURS, or 해/빼-judged within SUPPRESS_WINDOW_HOURS — is skipped without a
        call (미뤄 never rests a note; an unresolvable subject is never skipped on this
        rule). A candidate that fails to ground (schema, quote, or NotWorth) is not an
        error: it just does not become a proposal, and the queue moves on — but its reason
        is kept in advise_stats, not discarded, so a dry run can quote why. Before the
        prompt, card_advice.check_sufficiency asks whether the grounding actually carries
        the candidate's own note — a recurrence's path search returns unrelated notes only
        — and a miss costs one read_note, the note riding first in the prompt's hits; an
        unresolvable subject or an unreadable note is a recorded reason, never a guess."""

        candidates = card_registers.merge_project_candidates(
            state["projects"], state["project_registers"], state["priority_subjects"]
        )
        per_project_candidates: dict[str, int] = {}
        for project, _subject, _register in candidates:
            per_project_candidates[project] = per_project_candidates.get(project, 0) + 1
        proposals: list[card_types.Proposal] = []
        not_worth_reasons: list[str] = []
        ungrounded_reasons: list[str] = []
        resting = card_verdicts.resting_notes(state["past_verdicts"])
        calls = 0
        skipped_resting = 0
        refetched_own_note = 0
        sufficiency_reasons: list[str] = []
        warned_empty_vault = False
        for project, subject, register in candidates:
            if len(proposals) >= 3 or calls >= ADVISE_CALL_CAP:
                break
            note = subject if register == "recurrences" else None
            if note is None:
                resolved = _resolve(subject, register)
                note = resolved.note if isinstance(resolved, card_types.ResolvedNote) else None
            if note is not None and note in resting:
                skipped_resting += 1
                continue
            # A recurrence subject is a note path, and /search on that path answers
            # unrelated notes only — skip it. The check below names the candidate's own
            # note when the grounding is short of it, and one read_note puts it first in
            # the prompt's hits. No extra model call.
            hits = [] if register == "recurrences" else collabs.search(subject)
            note_texts: dict[str, str] = {}
            refetched = False
            sufficiency = card_advice.check_sufficiency(note, hits)
            if isinstance(sufficiency, card_advice.MissingOwnNote):
                own_text = collabs.read_note(sufficiency.note)
                if own_text is None:
                    sufficiency_reasons.append(f"own note {sufficiency.note} absent from hits and unreadable")
                else:
                    refetched = True
                    hits = [card_advice.own_note_hit(sufficiency.note, own_text), *hits]
                    note_texts[sufficiency.note] = own_text
                    title = card_advice.note_title(own_text) if register == "recurrences" else None
                    if title:
                        extra = [h for h in collabs.search(title) if h.get("source_path") != note]
                        hits += extra[:2]
            elif isinstance(sufficiency, card_advice.Unknown):
                sufficiency_reasons.append(sufficiency.reason)
            if refetched:
                refetched_own_note += 1
            unread = [h for h in hits if h.get("source_path") not in note_texts]
            for path, text in _note_texts_for_hits(collabs.read_note, unread).items():
                note_texts[path] = text
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
            superseded_by = {
                h["source_path"]: card_advice.superseded_names(h) for h in hits if h.get("source_path")
            }
            advised = card_advice.parse_advised(collabs.propose(prompt), note_texts, superseded_by)
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
                skipped_resting=skipped_resting,
                refetched_own_note=refetched_own_note,
                not_worth_reasons=not_worth_reasons,
                ungrounded_reasons=ungrounded_reasons,
                sufficiency_reasons=sufficiency_reasons,
            ),
        }

    def resolve(state: CardState) -> dict:
        """subject → note path through the door, for whatever advise picked. A subject that
        does not resolve is dropped as a value — 「근거 노트 없음」 never rides a card. Fewer
        than three, even zero, ships: NotWorth and an unresolved subject are both legitimate
        answers now (실험 1's recall precision was 1/5), not a reason to refuse the card.
        A resolved candidate is then run through suppression (7-day 판정, 72h rest for
        shown-but-unanswered) on the history read_history already carried in state — read
        unconditionally, before advise, because a card that cannot read its own judged
        history is the wrong card to send (같은 원칙: /approved). advise's note-level skip is
        the coarser call-saver; this pair-level filter stays the source of truth."""
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
        kept, dropped = card_verdicts.suppressed(picked, state["past_verdicts"])
        return {"proposals": kept, "suppressed_count": len(dropped)}

    def post_card(state: CardState) -> dict:
        lang = state["lang"]
        n_repairs = len(state["repairs"])
        fitted = card_view.fit_card(
            state["proposals"],
            confirmation=state["confirmation"],
            repairs=state["repairs"],
            repairs_total_groups=state["repairs_total_groups"],
            merged_yesterday_rows=state["merged_yesterday_rows"],
            reviews=state["reviews"],
            lang=lang,
            note_links=boring_config.note_links(),
            score=state["score"],
            samples=state["samples"],
        )
        message = collabs.send(fitted.blocks)
        card_ts = message.ts
        for sample in fitted.samples:
            collabs.record(
                "verdict_sample_shown",
                {
                    "session_id": sample.session_id,
                    "note": sample.note,
                    "proposed_kind": sample.kind,
                    "card_ts": card_ts,
                },
            )
        left_out = fitted.report.left_out
        collabs.record(
            "card_fit",
            {
                "card_ts": card_ts,
                "chars": fitted.report.chars,
                "text_max": fitted.report.text_max,
                "left_out_advice": left_out.advice,
                "left_out_repairs": left_out.repairs,
                "left_out_reviews": left_out.reviews,
                "samples_asked": len(state["samples"]),
                "samples_shown": len(fitted.samples),
            },
        )
        shown = [(idx, state["proposals"][idx]) for idx in sorted(fitted.report.advice_shown)]
        collabs.handover(
            card_press.session_name(message.channel, message.ts),
            datetime.now(UTC).isoformat(),
            card_verdicts.handover_paths([proposal for _, proposal in shown]),
        )
        for idx, proposal in shown:
            # global idx (repairs first) — the button press's card_verdict event carries the
            # same idx, and _live_past_verdicts joins the two events on it. A row the card
            # left out is not "shown and unpressed": it gets no event, only card_fit's count.
            collabs.record(
                "card_proposal", card_verdicts.proposal_event_fields(proposal, lang, card_ts, n_repairs + idx)
            )
        confirmation = state["confirmation"]
        if confirmation is not None:
            fields: dict[str, Any] = {
                "done": len(confirmation.done),
                "pending": len(confirmation.pending),
                "session": confirmation.session,
                # The card's ts, like card_proposal carries — the once-a-day guard reads the
                # confirmation event too, and needs the ts to name the card it belongs to.
                "card_ts": card_ts,
            }
            if confirmation.unknown:
                fields["unknown"] = len(confirmation.unknown)
            collabs.record("card_confirmation", fields)
        return {"message": message}

    graph = StateGraph(CardState)
    graph.add_node("read_repairs", read_repairs)
    graph.add_node("read_proposed", read_proposed)
    graph.add_node("read_registers", read_registers)
    graph.add_node("cross_check", cross_check)
    graph.add_node("read_history", read_history)
    replay = collabs.posted_proposals
    graph.add_node("advise", advise if replay is None else _replay_advise(replay))
    graph.add_node("resolve", resolve if replay is None else _replay_resolve)
    graph.add_node("post_card", post_card)
    graph.add_edge(START, "read_repairs")
    graph.add_edge("read_repairs", "read_proposed")
    graph.add_edge("read_proposed", "read_registers")
    graph.add_edge("read_registers", "cross_check")
    graph.add_edge("cross_check", "read_history")
    graph.add_edge("read_history", "advise")
    graph.add_edge("advise", "resolve")
    graph.add_edge("resolve", "post_card")
    return graph.compile()


def _replay_advise(posted: Callable[[], list[card_types.Proposal]]) -> Callable[[CardState], dict]:
    def advise(_: CardState) -> dict:
        return {
            "proposals": posted(),
            "per_project_candidates": {},
            "advise_stats": card_types.AdviseStats(
                calls=0, proposals_passed=0, not_worth=0, ungrounded=0, skipped_resting=0
            ),
        }

    return advise


def _replay_resolve(state: CardState) -> dict:
    return {"proposals": state["proposals"], "suppressed_count": 0}


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
    head = blocks[0].get("text") or {}
    ts = slack_post.post_payload({"blocks": blocks, "text": head.get("text") or "아침 카드"})
    return card_types.PostedCard(channel=os.environ["SLACK_CARD_CHANNEL"], ts=ts)


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


def _dry_send(blocks: list[dict]) -> card_types.PostedCard:
    return card_types.PostedCard(channel="dry-run", ts="0")


def _dry_handover(session: str, at: str, paths: list[str]) -> dict:
    return {}


def _dry_record(event: str, fields: dict) -> None:
    return None


def _run_dry() -> int:
    """CARD_DRY_RUN=1: run the graph through post_card with everything live except Slack,
    handover, and the event log — print the proposals instead of sending. No send, no
    handover, no card_proposal/card_confirmation event — only stdout. active_projects and
    past_verdicts stay live: both are reads (GET /projects, GET /events), not writes, so
    the dry run's projects_called and suppression numbers are the real ones the next live
    card would see."""
    sent: list[list[dict]] = []

    def send(blocks: list[dict]) -> card_types.PostedCard:
        sent.append(blocks)
        return _dry_send(blocks)

    lang = card_advice.resolve_lang(boring_config.note_lang())
    collabs = Collaborators(
        fetch=card_live._live_fetch,
        search=card_live._live_search,
        read_note=card_live._live_read_note,
        propose=make_propose(),
        send=send,
        handover=_dry_handover,
        resolve=card_live._live_resolve,
        approved=card_live._live_approved,
        record=_dry_record,
        active_projects=card_live._live_active_projects,
        past_verdicts=card_live._live_past_verdicts,
        lang=lang,
        repairs=card_live._live_repairs,
        merged_yesterday=card_live._live_merged_yesterday,
        proposed=card_live._live_proposed,
        held_repairs=card_live._held_repair_subjects,
        repair_judgments=card_live._live_repair_judgments,
        score=card_live._live_score,
    )
    graph = build_graph(collabs)
    state = graph.invoke({})
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
                "skipped_resting": stats.skipped_resting,
                "refetched_own_note": stats.refetched_own_note,
                "not_worth_reasons": stats.not_worth_reasons,
                "sufficiency_reasons": stats.sufficiency_reasons,
                "ungrounded_reasons": stats.ungrounded_reasons,
                "suppressed": state["suppressed_count"],
                "score": state["score"].model_dump() if state["score"] else None,
                "scoreline": (
                    card_view.scoreline_text(state["score"], card_i18n.STRINGS[lang])
                    if state["score"]
                    else None
                ),
                "confirm_samples": [
                    {"session_id": s.session_id, "note": s.note, "kind": s.kind} for s in state["samples"]
                ],
                "card_chars": card_view.card_chars(sent[0]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _never(what: str) -> Callable[..., Any]:
    def refuse(*_: Any) -> Any:
        raise ValueError(f"CARD_RENDER_ONLY never calls {what}")

    return refuse


def _run_render_only() -> int:
    """CARD_RENDER_ONLY=1: draw today's card from live reads and write nothing — no Slack, no
    handover, no event, and no /search or model call (the advice lane is today's already-posted
    proposals, or empty). Prints the blocks and their size as one JSON object."""
    sent: list[list[dict]] = []
    fit: list[dict] = []

    def send(blocks: list[dict]) -> card_types.PostedCard:
        sent.append(blocks)
        return _dry_send(blocks)

    def record(event: str, fields: dict) -> None:
        if event == "card_fit":
            fit.append(fields)

    collabs = Collaborators(
        fetch=card_live._live_fetch,
        search=_never("search"),
        read_note=card_live._live_read_note,
        propose=_never("the model"),
        send=send,
        handover=_dry_handover,
        resolve=card_live._live_resolve,
        approved=card_live._live_approved,
        record=record,
        active_projects=card_live._live_active_projects,
        past_verdicts=card_live._live_past_verdicts,
        lang=card_advice.resolve_lang(boring_config.note_lang()),
        repairs=card_live._live_repairs,
        merged_yesterday=card_live._live_merged_yesterday,
        proposed=card_live._live_proposed,
        held_repairs=card_live._held_repair_subjects,
        repair_judgments=card_live._live_repair_judgments,
        score=card_live._live_score,
        posted_proposals=card_live._live_posted_proposals,
    )
    build_graph(collabs).invoke({})
    print(
        json.dumps(
            {"card_chars": card_view.card_chars(sent[0]), "fit": fit[0], "blocks": sent[0]},
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    render_only = bool(os.environ.get("CARD_RENDER_ONLY"))
    if not render_only and not os.environ.get("SLACK_BOT_TOKEN"):
        print("[card] SLACK_BOT_TOKEN must be set (see .env.example)", file=sys.stderr)
        return 2
    if not render_only and not os.environ.get("SLACK_CARD_CHANNEL"):
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

    if render_only:
        return _run_render_only()
    if os.environ.get("CARD_DRY_RUN"):
        return _run_dry()

    posted = _posted_today_ts()
    if posted is not None:
        # launchd and the hermes cron share the handover window; the second runner of the
        # morning sees the first one's card and stops — one card a day, said in one line.
        print(f"[card] already posted today (ts={posted})", flush=True)
        return 0

    from slack_sdk.web import WebClient

    web_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    try:
        web_client.auth_test()
    except Exception as e:  # noqa: BLE001 — say why in one line, not with a stack trace and no token
        print(f"[card] auth_test failed: {e}", file=sys.stderr)
        return 1

    graph = build_graph()
    try:
        state = graph.invoke({})
        print(f"[card] posted ts={state['message'].ts}", flush=True)
    except (ValueError, OSError) as e:
        # A malformed register, an ungrounded proposal, a dead door, a refused resolve —
        # say why in one line and stop. OSError covers URLError: the door was the only
        # road to /claim-source and /approved, and a card that cannot confirm is a card
        # that must not ship quietly.
        print(f"[card] 카드 거부: {' '.join(str(e).split())}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
