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

import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from card_effects import (  # noqa: F401
    ENGINE_TIMEOUT,
    _door_url,
    _live_consumption,
    _live_execute_repair,
    _live_record,
)
from card_press import CARD_ANSWERABLE_HOURS
from card_types import (
    NO_CURRENT_CLAIM,
    PastApproved,
    PastCardHistory,
    PastUnansweredPair,
    PastVerdictPair,
    ProposedVerdict,
    ResolvedNote,
    Unresolved,
)
from pydantic import ValidationError

from ohmyboring import config as omb_env
from ohmyboring.adapters.engine import DrudgeClient
from ohmyboring.result import Err, Ok

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "memory"))
from retriever import BoringRetriever  # noqa: E402


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
    """GET /events, one event name at a time. §정정 (2026-09-22): the given assumed /events
    sits among the door's proxied routes — measured false: `data/contract/engine-contract.json`
    http_routes has 24 entries and /events is not one of them (`curl :7710/events` → 404),
    while the engine answers it directly (`curl :7700/events?limit=3` → 200). /events is a
    plain, non-DB-backed engine route, so this reads the engine directly (omb_env.drudge_url(),
    the same resolution DrudgeClient uses) rather than adding a route to the door that the
    door's own job (DB-backed answers the engine cannot give) never needed. maybe_truncated is
    not a value to shrug at here: a clipped 7-day window would silently under-suppress (a
    do/drop that should have hidden a repeat candidate falls outside the page handed back), so
    it is raised, the same way an unreadable window is raised anywhere else in this file."""
    url = f"{omb_env.drudge_url()}/events?event={urllib.parse.quote(event_name)}&since_hours={since_hours}&limit=1000"
    payload = _door_json(url)
    if payload.get("maybe_truncated"):
        raise OSError(
            f"/events?event={event_name}&since_hours={since_hours} maybe_truncated=true — "
            "a clipped judged-history window cannot ground 판정 suppression"
        )
    return payload["entries"]


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


def _live_handover(session: str, at: str, paths: list[str]) -> dict:
    # Err→예외는 카드 그래프가 예외를 계약으로 삼는 동안의 임시 경계 — 실패한 카드는 게시되지 않는다.
    match DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).handover(session, at, paths):
        case Ok(resp):
            return resp
        case Err(failure):
            raise OSError(str(failure))


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

#: how many of the agent's session-end classifications the review lane shows.
REVIEW_LIMIT = 3


def _live_proposed(since_hours: int) -> list[ProposedVerdict]:
    """The review lane's rows: session-end verdict_proposed events, contested first, then
    newest first, at most REVIEW_LIMIT. A row missing a field this value needs is malformed —
    raise (F5/ROP), never skip: a dropped row here is a flip the owner was never shown."""
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
                )
            )
        except (KeyError, TypeError, ValidationError) as e:
            raise ValueError(f"malformed verdict_proposed row: {attrs!r}: {e}") from e
    parsed.sort(key=lambda p: p.at, reverse=True)  # newest first
    parsed.sort(key=lambda p: p.kind != "contested")  # stable: contested keeps the head
    return parsed[:REVIEW_LIMIT]


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
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None
