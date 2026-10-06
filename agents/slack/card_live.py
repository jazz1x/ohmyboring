#!/usr/bin/env python3
"""The morning card's live collaborators — every read-side seam that reaches the door, the
engine, or the host vault. Depends only on card_types (plus stdlib and the shared engine
clients) — never on card_registers/card_advice/card_verdicts/card_view/card — so card.py
can import this module for its default Collaborators without a cycle back.

The write-side effects (record/consumption/execute_repair) and the fold that applies a
card_press.effects list live in card_effects — a module that must stay importable inside
the hermes venv, which has no langchain. This module imports them back so existing
references (card.py's Collaborators, tests) keep working; `_door_url` and ENGINE_TIMEOUT
moved there with them.

`_live_past_verdicts`'s proposal window is card_press.CARD_ANSWERABLE_HOURS wider than its
verdict window; the hermes plugin refuses presses older than that, which is what keeps the
join whole."""

from __future__ import annotations

import glob
import json
import math
import os
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

import card_repair_judge
from card_effects import (  # noqa: F401
    ENGINE_TIMEOUT,
    _door_url,
    _live_consumption,
    _live_execute_repair,
    _live_handover,
    _live_record,
)
from card_press import CARD_ANSWERABLE_HOURS
from card_types import (
    NO_CURRENT_CLAIM,
    PastApproved,
    PastCardHistory,
    PastUnansweredPair,
    PastVerdictPair,
    Proposal,
    ProposedVerdict,
    RepairJudgment,
    ResolvedNote,
    Scored,
    ScoreReading,
    ScoreUnreadable,
    Unresolved,
)
from pydantic import ValidationError

from ohmyboring import config as omb_env
from ohmyboring.adapters.engine import DrudgeClient
from ohmyboring.result import Err, Ok

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "memory"))
from retriever import BoringRetriever  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import label_core  # noqa: E402
import vault_note  # noqa: E402


def _live_fetch(path: str, project: str) -> dict[str, Any]:
    # project is always sent, even "" — the engine's own filter treats an explicit empty
    # string as "unassigned documents only", not "no filter" (measured 2026-09-22).
    # Err→예외는 카드 그래프가 예외를 계약으로 삼는 동안의 임시 경계(card.py 의 except 가 주인).
    match DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).request("POST", path, {"project": project}):
        case Ok(payload):
            return payload
        case Err(failure):
            raise OSError(str(failure))


def _door_json(url: str) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=ENGINE_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except OSError as e:
        raise OSError(f"{url}: {e}") from e


def _live_active_projects(active_days: int) -> list[str]:
    """The door's own GET /projects?active_days=N — DB-backed, unlike the engine's plain
    /projects (no activity filter), so this asks the door, not DrudgeClient's engine URL."""
    url = f"{_door_url()}/projects?active_days={active_days}"
    payload = _door_json(url)
    return [str(item["project"]) for item in payload["projects"]]


def _live_events(event_name: str, since_hours: int) -> list[dict[str, Any]]:
    """GET /events, one event name at a time. E4-α: /events 는 이제 문의 프록시 표에 있고
    (계약 스냅샷 재생성 — 옛 측정(2026-09-22)이 404 로 찍었던 그 경로다), 문은 표에 있는
    경로를 엔진에 바이트 그대로 넘기니 여기서도 omb_env.door_url() 하나로 읽는다. 문이
    없던 경로를 엔진에 붙이던 임시 직행은 사라진다. maybe_truncated is not a value to shrug
    at here: a clipped 7-day window would silently under-suppress (a do/drop that should
    have hidden a repeat candidate falls outside the page handed back), so it is raised,
    the same way an unreadable window is raised anywhere else in this file."""
    url = f"{omb_env.door_url()}/events?event={urllib.parse.quote(event_name)}&since_hours={since_hours}&limit=1000"
    payload = _door_json(url)
    if payload.get("maybe_truncated"):
        raise OSError(
            f"/events?event={event_name}&since_hours={since_hours} maybe_truncated=true — "
            "a clipped judged-history window cannot ground 판정 suppression"
        )
    return payload["entries"]


def _live_posted_proposals() -> list[Proposal]:
    """The advice rows of the card already posted today (KST), rebuilt from its card_proposal
    events in idx order — what a render-only run puts in the advice lane instead of asking
    the model again. No card today, no rows. A row that does not rebuild is malformed:
    raised, never skipped. A [더보기] page's rows (`more_of`) are not the card: counted in,
    the newest page would stand in for the whole card."""
    kst = timezone(timedelta(hours=9))
    today = datetime.now(kst).date()
    entries = [
        entry
        for entry in _live_events("card_proposal", 24)
        if datetime.fromisoformat(entry["observed_at"].replace("Z", "+00:00")).astimezone(kst).date() == today
        and "more_of" not in entry["attributes"]
    ]
    if not entries:
        return []
    newest = max(entries, key=lambda e: e["observed_at"])["attributes"]["card_ts"]
    todays = sorted(
        (e["attributes"] for e in entries if e["attributes"]["card_ts"] == newest), key=lambda a: a["idx"]
    )
    try:
        return [
            Proposal.model_validate(
                {
                    key: attrs[key]
                    for key in ("register", "project", "subject", "note", "bottleneck", "advice", "evidence")
                }
            )
            for attrs in todays
        ]
    except (KeyError, TypeError, ValidationError) as e:
        raise ValueError(f"malformed card_proposal row for card {newest}: {e}") from e


def _live_past_verdicts(since_hours: int) -> PastCardHistory:
    """Join card_proposal and card_verdict events by (card_ts, idx) — a card_verdict event
    alone carries no note or evidence, only the button press (knowns: card_ts·idx·choice).
    Proposals are read over a wider window than verdicts: a press can land up to
    CARD_ANSWERABLE_HOURS after its card posted, so a verdict at hour 167 of the 168h window
    would otherwise be joined against a proposal that already fell outside it. The same
    join also yields the shown-but-unanswered pairs: a card_proposal with no card_verdict
    in the window is a proposal the owner saw and never judged, returned with the
    proposal event's own observed_at so suppressed can rest it REST_HOURS. A card_verdict
    with no matching card_proposal, or a matching one missing note/evidence/choice/
    timestamp, is a malformed row — F5/ROP: that is a visible failure (ValueError naming
    the row), never a silently skipped one, because a dropped row here is exactly a
    suppression pair going missing without anyone knowing."""
    proposal_window = since_hours + math.ceil(CARD_ANSWERABLE_HOURS)
    proposals_by_key: dict[tuple[Any, Any], dict[str, Any]] = {}
    for entry in _live_events("card_proposal", proposal_window):
        attrs = entry.get("attributes") or {}
        key = (attrs.get("card_ts"), attrs.get("idx"))
        if key[0] is not None and key[1] is not None:
            proposals_by_key[key] = entry
    judged: list[PastVerdictPair] = []
    answered: set[tuple[Any, Any]] = set()
    for entry in _live_events("card_verdict", since_hours):
        attrs = entry.get("attributes") or {}
        key = (attrs.get("card_ts"), attrs.get("idx"))
        answered.add(key)
        proposal_entry = proposals_by_key.get(key)
        if proposal_entry is None:
            raise ValueError(f"card_verdict {key!r} has no matching card_proposal event: {attrs!r}")
        proposal = proposal_entry.get("attributes") or {}
        evidence = proposal.get("evidence") or []
        if not evidence:
            raise ValueError(f"card_proposal {key!r} was recorded with no evidence: {proposal!r}")
        first = evidence[0]
        try:
            judged.append(
                PastVerdictPair(
                    note=proposal["note"],
                    evidence_note=first["note"],
                    evidence_line=int(first["line"]),
                    choice=attrs["choice"],
                    at=entry["observed_at"],
                )
            )
        except (KeyError, TypeError, ValueError, ValidationError) as e:
            raise ValueError(f"malformed card_verdict/card_proposal pair {key!r}: {e}") from e
    unanswered: list[PastUnansweredPair] = []
    for key, proposal_entry in proposals_by_key.items():
        if key in answered:
            continue
        proposal = proposal_entry.get("attributes") or {}
        evidence = proposal.get("evidence") or []
        if not evidence:
            raise ValueError(f"card_proposal {key!r} was recorded with no evidence: {proposal!r}")
        first = evidence[0]
        try:
            unanswered.append(
                PastUnansweredPair(
                    note=proposal["note"],
                    evidence_note=first["note"],
                    evidence_line=int(first["line"]),
                    at=proposal_entry["observed_at"],
                )
            )
        except (KeyError, TypeError, ValueError, ValidationError) as e:
            raise ValueError(f"malformed card_proposal {key!r}: {e}") from e
    return PastCardHistory(judged=judged, unanswered=unanswered)


def _live_resolve(subject: str, register: str) -> ResolvedNote | Unresolved:
    """subject → current claim's note path through the door. A recurrence source is already
    a note path — it resolves to itself without a door round-trip. Everything else asks
    /claim-source; every failure mode is an Unresolved value: the resolve node drops that
    proposal and the card ships with whatever is left, even zero."""
    if subject.startswith("/"):
        return ResolvedNote(subject=subject, note=subject)
    url = f"{_door_url()}/claim-source?subject={urllib.parse.quote(subject)}"
    try:
        with urllib.request.urlopen(url, timeout=ENGINE_TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            reason = NO_CURRENT_CLAIM
        else:
            reason = f"claim-source answered {e.code}"
        return Unresolved(subject=subject, register=register, reason=reason)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return Unresolved(subject=subject, register=register, reason=f"claim-source unreachable: {e}")
    return ResolvedNote(subject=subject, note=payload["note"])


def _live_approved(since_hours: int) -> list[PastApproved]:
    url = f"{_door_url()}/approved?since_hours={since_hours}"
    payload = _door_json(url)
    return [
        PastApproved(session=item["session"], note=item["note"], at=item["at"])
        for item in payload["approved"]
    ]


#: how many split-subject groups the "오늘 할 일" lane shows — the door's own default too.
REPAIRS_LIMIT = 3

#: The repair lane's scan width — 문 한 번에 주는 상한(문 _MAX_REPAIR_LIMIT)만큼 받아 판정으로
#: 거른 뒤 REPAIRS_LIMIT 개를 앉힌다. 거른 뒤 3개가 안 차도 실패가 아니다.
REPAIRS_SCAN_LIMIT = 50


def _live_repair_judgments() -> dict[tuple[str, tuple[str, ...]], RepairJudgment]:
    return card_repair_judge.judged_map(_live_events("repair_judged", card_repair_judge.JUDGED_WINDOW_HOURS))


#: how many of the agent's session-end classifications the review lane shows.
REVIEW_LIMIT = 3

#: The held-pair (보류) window, hours — 이레, the same window as the advice lane's
#: SUPPRESS_WINDOW_HOURS (card_verdicts): a pair the owner held comes back only after the
#: window lets it go. card_live never imports card_verdicts, so the number lives here with
#: that pointer rather than a cross-import.
REVIEW_DEFER_WINDOW_HOURS = 168


def _deferred_review_pairs() -> set[tuple[str, str]]:
    """(note, kind) pairs the owner held (보류) within the 이레 window — read from the
    verdict_reviewed 사건 a hold leaves. Agree/flip/delegate rows are ignored here: only a
    hold suppresses (the delegate rows a successful judgment leaves are read by
    _delegated_reasons, which runs before this filter). A hold row missing the note or its
    kind is malformed, same as a malformed proposal: raise (F5/ROP), never skip — a dropped
    row here is exactly a held pair coming back unannounced."""
    out: set[tuple[str, str]] = set()
    for entry in _live_events("verdict_reviewed", REVIEW_DEFER_WINDOW_HOURS):
        attrs = entry.get("attributes") or {}
        if attrs.get("choice") != "defer":
            continue
        note, kind = attrs.get("note"), attrs.get("proposed_kind")
        if not isinstance(note, str) or not note or kind not in ("used", "contested"):
            raise ValueError(f"malformed verdict_reviewed hold row: {attrs!r}")
        out.add((note, kind))
    return out


def _delegated_reasons(since_hours: int) -> dict[tuple[str, str], tuple[str, str, str]]:
    """(note, proposed_kind) → (judged kind, 이유, observed_at) for every successful
    「맡길게요」 judgment — a verdict_reviewed delegate row that actually carries the model's
    kind and reason. The word-flag stamps b300c46 wrote (choice=delegate with no kind or
    reason) are not judgments: they are skipped, never mistaken for one. A failed delegation
    (error field, no kind) leaves no judgment either — the row comes back unanswered."""
    out: dict[tuple[str, str], tuple[str, str, str]] = {}
    for entry in _live_events("verdict_reviewed", since_hours):
        attrs = entry.get("attributes") or {}
        if attrs.get("choice") != "delegate":
            continue
        note, proposed_kind = attrs.get("note"), attrs.get("proposed_kind")
        kind, reason = attrs.get("kind"), attrs.get("reason")
        if (
            not isinstance(note, str)
            or not note
            or proposed_kind not in ("used", "contested")
            or kind not in ("used", "contested")
            or not isinstance(reason, str)
            or not reason
        ):
            continue
        observed_at = entry.get("observed_at")
        if not isinstance(observed_at, str):
            continue
        key = (note, proposed_kind)
        if key not in out or observed_at >= out[key][2]:
            out[key] = (kind, reason, observed_at)
    return out


def _group_reviews(rows: list[ProposedVerdict]) -> list[ProposedVerdict]:
    """같은 노트를 같은 판정으로 (작업만 다르게) 본 행을 한 줄로 묶는다 — 오너가 묶음을
    고를 때 판정은 묶인 세션 전부에 가야 하니, 묶음 전체가 한 단추에 실린다. 첫 목격
    (최신) 순서가 묶음 안에서도 그대로고, 이유는 묶음에서 첫 번째로 남은 근거 문장이다."""
    order: list[tuple[str, str]] = []
    groups: dict[tuple[str, str], list[ProposedVerdict]] = {}
    for row in rows:
        key = (row.note, row.kind)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    out = []
    for key in order:
        members = groups[key]
        first = members[0]
        if len(members) == 1:
            out.append(first)
            continue
        out.append(
            first.model_copy(
                update={
                    "sessions": [m.session_id for m in members],
                    "reason": next((m.reason for m in members if m.reason), ""),
                }
            )
        )
    return out


def _proposed_rows(since_hours: int) -> list[ProposedVerdict]:
    """Every session-end verdict_proposed event as a value, event order. A row missing a
    field this value needs is malformed — raise (F5/ROP), never skip."""
    parsed: list[ProposedVerdict] = []
    for entry in _live_events("verdict_proposed", since_hours):
        attrs = entry.get("attributes") or {}
        try:
            parsed.append(
                ProposedVerdict(
                    session_id=attrs["session_id"],
                    note=attrs["note"],
                    kind=attrs["kind"],
                    at=entry["observed_at"],
                    reason=attrs.get("reason") or "",
                )
            )
        except (KeyError, TypeError, ValidationError) as e:
            raise ValueError(f"malformed verdict_proposed row: {attrs!r}: {e}") from e
    return parsed


def _with_titles_and_works(rows: list[ProposedVerdict]) -> list[ProposedVerdict]:
    works = _session_works() if rows else {}
    out = []
    for r in rows:
        update: dict = {"note_title": _note_title(r.note), "work": works.get(r.session_id, "")}
        if r.sessions:
            update["works"] = [works.get(s, "") for s in r.sessions]
        out.append(r.model_copy(update=update))
    return out


def _live_proposed(since_hours: int) -> list[ProposedVerdict]:
    """The review lane's rows: session-end verdict_proposed events, contested first, then
    newest first, the same (note, kind) proposed by several sessions folded into one row,
    a pair a successful 「맡길게요」 judged carrying the model's 이유 in place of the scorer's
    sentence (the kind line itself stays the session's own claim), pairs the owner held
    within the 이레 window left off, at most REVIEW_LIMIT. A row missing a field this value
    needs is malformed — raise (F5/ROP), never skip: a dropped row here is a judgment the
    owner was never shown."""
    parsed = _proposed_rows(since_hours)
    parsed.sort(key=lambda p: p.at, reverse=True)  # newest first
    parsed.sort(key=lambda p: p.kind != "contested")  # stable: contested keeps the head
    # The same judgement can land twice (two SessionEnd hooks, 1ms apart — 2026-09-30).
    seen: set[tuple[str, str, str]] = set()
    unique = [p for p in parsed if (key := (p.session_id, p.note, p.kind)) not in seen and not seen.add(key)]
    grouped = _group_reviews(unique)
    if grouped:
        delegated = _delegated_reasons(since_hours)
        if delegated:
            # 맡긴 판정이 있는 짝은 이유 한 줄을 그 판정의 것으로 바꾼다 — 다음 카드에 보이는
            # 이유가 모델이 남긴 문장. 짝의 종류 줄은 세션이 말한 것을 그대로 말하는 문구라
            # (「말이 나왔어요」) 판정에 따라 바뀌지 않는다: 뒤집힌 종류는 엔진 간선에만 실린다.
            grouped = [
                r.model_copy(update={"reason": delegated[(r.note, r.kind)][1]})
                if (r.note, r.kind) in delegated
                else r
                for r in grouped
            ]
        deferred = _deferred_review_pairs()
        grouped = [r for r in grouped if (r.note, r.kind) not in deferred]
    return _with_titles_and_works(grouped[:REVIEW_LIMIT])


#: The 채점 줄's reach — every owner press since the review lane began, not the card's window.
SCORE_WINDOW_HOURS = 24 * 90

#: The 확인용 표본's population: session-end calls this recent. Held to three days so a
#: day's draw stays inside /events' 1,000-row page and the owner can still place the work.
SAMPLE_POPULATION_HOURS = 72

#: How far back a 「보인」 record keeps a pair from being asked again.
SAMPLE_SHOWN_WINDOW_HOURS = REVIEW_DEFER_WINDOW_HOURS

#: At most this many 확인용 표본 a day.
CONFIRM_SAMPLE_MAX = 2

Pair = tuple[str, str]  # (session id, note path)


def _row_pairs(attrs: dict[str, Any]) -> list[Pair]:
    """The (session, note) pairs one verdict_reviewed row speaks for — a hold or delegate
    row carries every grouped session in `sessions`, an agree/flip row one `session_id`.
    A row naming no note or no session names no pair."""
    note = attrs.get("note")
    sessions = attrs.get("sessions") or [attrs.get("session_id")]
    if not isinstance(note, str) or not note:
        return []
    return [(s, note) for s in sessions if isinstance(s, str) and s]


def tally(reviewed: list[dict[str, Any]]) -> Scored:
    """agree/flip presses folded to one per (session, note) — the last press wins. delegate
    rows are the agent's own hand and a hold is 「안 물어봄」: neither enters the pair. An
    agree/flip row naming no session or note is malformed — raise, never skip."""
    last: dict[Pair, tuple[str, str]] = {}
    for entry in reviewed:
        attrs = entry.get("attributes") or {}
        choice = attrs.get("choice")
        if choice not in ("agree", "flip"):
            continue
        pairs = _row_pairs(attrs)
        at = entry.get("observed_at")
        if len(pairs) != 1 or not isinstance(at, str):
            raise ValueError(f"malformed verdict_reviewed {choice} row: {attrs!r}")
        key = pairs[0]
        if key not in last or at >= last[key][0]:
            last[key] = (at, choice)
    return Scored(agreed=sum(1 for _, c in last.values() if c == "agree"), compared=len(last))


def pressed_pairs(reviewed: list[dict[str, Any]]) -> set[Pair]:
    return {pair for entry in reviewed for pair in _row_pairs(entry.get("attributes") or {})}


def pick_confirm_samples(
    population: list[ProposedVerdict],
    *,
    excluded: set[Pair],
    day: date,
    compared: int,
) -> list[ProposedVerdict]:
    """The day's 확인용 무작위 표본: calls the owner has not pressed, not seen as a sample,
    and not on today's review lane — at most CONFIRM_SAMPLE_MAX, drawn by a generator
    seeded with the KST date so a second run the same day draws the same ones. The pool is
    ordered by (session, note) before the draw, so event order never moves the pick. Once
    `compared` reaches label_core.MIN_COMPARED the draw is empty: the loop stops on its own."""
    if compared >= label_core.MIN_COMPARED:
        return []
    pool: dict[Pair, ProposedVerdict] = {}
    for row in population:
        key = (row.session_id, row.note)
        if key not in excluded and key not in pool:
            pool[key] = row
    ordered = [pool[key] for key in sorted(pool)]
    return random.Random(day.isoformat()).sample(ordered, min(CONFIRM_SAMPLE_MAX, len(ordered)))


def _live_score(reviews: list[ProposedVerdict], day: date) -> ScoreReading:
    """The tally of the owner's presses, and the day's 확인용 표본. A tally that cannot be
    read is a ScoreUnreadable value the card prints as such — never a number, never a
    skipped line; a sample pool that cannot be read draws nothing and says why on stderr."""
    try:
        reviewed = _live_events("verdict_reviewed", SCORE_WINDOW_HOURS)
        score = tally(reviewed)
    except (OSError, ValueError) as e:
        return ScoreReading(score=ScoreUnreadable(reason=" ".join(str(e).split())))
    try:
        shown = {
            pair
            for entry in _live_events("verdict_sample_shown", SAMPLE_SHOWN_WINDOW_HOURS)
            for pair in _row_pairs(entry.get("attributes") or {})
        }
        on_lane = {(s, r.note) for r in reviews for s in (r.sessions or [r.session_id])}
        samples = pick_confirm_samples(
            _proposed_rows(SAMPLE_POPULATION_HOURS),
            excluded=pressed_pairs(reviewed) | shown | on_lane,
            day=day,
            compared=score.compared,
        )
    except (OSError, ValueError) as e:
        print(f"[card] 확인 표본을 못 뽑았다: {' '.join(str(e).split())}", file=sys.stderr)
        return ScoreReading(score=score)
    return ScoreReading(score=score, samples=_with_titles_and_works(samples))


_FRONTMATTER_FIELD = re.compile(r"^(title|project|date|omb_session_id):[ \t]*(.*?)[ \t]*$", re.M)


def _frontmatter_fields(text: str) -> dict[str, str]:
    return {
        key: value.strip("'\"")
        for key, value in _FRONTMATTER_FIELD.findall(vault_note.frontmatter_text(text))
    }


def _note_title(note: str) -> str:
    text = _live_read_note(note)
    return _frontmatter_fields(text).get("title", "") if text is not None else ""


def _work_line(fields: dict[str, str]) -> str:
    date = fields.get("date", "")
    parts = (fields.get("project", ""), date[5:10] if len(date) >= 10 else date, fields.get("title", ""))
    return " · ".join(part for part in parts if part)


def _session_works() -> dict[str, str]:
    """omb_session_id → the work line of the note that session left, over the whole vault. A
    session with more than one note (21 of 1,650 on 2026-09-30) names its first by file name —
    the note it left first — so the card says the same thing on every run."""
    works: dict[str, str] = {}
    unreadable = 0
    for path in sorted(glob.glob(os.path.join(_vault_dir(), "wiki", "*.md"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
        except OSError:
            unreadable += 1
            continue
        fields = _frontmatter_fields(head)
        if (session := fields.get("omb_session_id")) and session not in works:
            works[session] = _work_line(fields)
    if unreadable:
        print(f"[card] {unreadable} vault note(s) unreadable — their sessions show by id", file=sys.stderr)
    return works


def _held_repair_subjects() -> set[str]:
    """Subjects the owner held or dropped on the repair lane within the 이레 window."""
    out: set[str] = set()
    for entry in _live_events("repair_reviewed", REVIEW_DEFER_WINDOW_HOURS):
        subject = (entry.get("attributes") or {}).get("subject")
        if not isinstance(subject, str) or not subject:
            raise ValueError(f"malformed repair_reviewed row: {entry!r}")
        out.add(subject)
    return out


def _live_repairs(limit: int = REPAIRS_LIMIT) -> dict[str, Any]:
    url = f"{_door_url()}/repairs/split-subjects?limit={limit}"
    return _door_json(url)


def _live_merged_yesterday() -> int | None:
    """Sum of yesterday's subject_merged deleted_rows, or None when nothing merged — the head
    line's optional clause. Read straight from the engine's /events, the same route
    _live_past_verdicts already reads."""
    entries = _live_events("subject_merged", 24)
    if not entries:
        return None
    return sum(int((e.get("attributes") or {}).get("deleted_rows") or 0) for e in entries)


def _live_search(subject: str) -> list[dict[str, Any]]:
    """A candidate's past record — the BoringRetriever over the door's /search with claims=3:
    the door's own recall of what the engine has decided about this subject before
    (wiki-1765 step 1), read through the LangChain seam the advice slot's retriever plugs
    into. Returned in the dict shape advise has always read: 'id', 'snippet', and the hit
    metadata (source_path, counts, claims) unpacked alongside."""
    retriever = BoringRetriever(base_url=_door_url(), max_results=3, claims=3)
    return [{"id": doc.id, "snippet": doc.page_content, **doc.metadata} for doc in retriever.invoke(subject)]


def _vault_dir() -> str:
    return os.path.expanduser(os.environ.get("BORING_VAULT_DIR") or "~/oh-my-boring/vault")


def _live_read_note(note: str) -> str | None:
    """A hit's own note text, read from the host vault (the card runs on the host, not in a
    container). `note` looks like `/vault/wiki/wiki-NNNN.md`; missing on disk is a value —
    that evidence simply cannot verify — never an exception here."""
    relative = note.removeprefix("/vault/") if note.startswith("/vault/") else note.lstrip("/")
    path = os.path.join(_vault_dir(), relative)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None
