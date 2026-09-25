#!/usr/bin/env python3
"""The morning card's live collaborators — every seam that reaches the door, the engine, or
the host vault. Depends only on card_types (plus stdlib and the shared engine clients) —
never on card_registers/card_advice/card_verdicts/card_view/card — so card.py can import
this module for its default Collaborators without a cycle back.

ENGINE_TIMEOUT and CARD_WAIT_HOURS live here rather than in card.py because the live reads
below are the only things that need them at module load: `_live_past_verdicts`'s proposal
window is CARD_WAIT_HOURS-wide, and every DrudgeClient/urlopen call here uses ENGINE_TIMEOUT.
card.py imports both back from this module for its own wait-loop default."""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

import omb_env
from card_types import (
    NO_CURRENT_CLAIM,
    PastApproved,
    PastVerdictPair,
    ProposedVerdict,
    RepairDone,
    RepairFailed,
    RepairUnanswered,
    ResolvedNote,
    Unresolved,
)
from drudge_client import OWNER, DrudgeClient, owner_headers
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "memory"))
from retriever import BoringRetriever  # noqa: E402

# Register answers carry up to 50 claims each; the door timeout lesson (brief p95 77s) says the
# point read is far cheaper, but a cold engine still earns more than a point-read default.
ENGINE_TIMEOUT = float(os.environ.get("CARD_ENGINE_TIMEOUT") or "30")
# A card's own lifespan — the next card's post outlives any button the owner never got to
# press, so waiting past this is polling a socket nobody is going to answer on.
CARD_WAIT_HOURS = float(os.environ.get("CARD_WAIT_HOURS") or "23")
# execute_repair's own timeout — separate from ENGINE_TIMEOUT (30s), which every quick
# point-read here shares. The door's POST blocks on the engine's synchronous /sync (global
# lock, full re-embed of every reread note); a 212-note group is not a point read. 600s is
# the door's own upstream ceiling headroom (DOOR_TIMEOUT=130s per hop, but /sync's own cost
# scales with note count, not request count) — long enough that a real repair's sync finishes
# under it rather than the card's http client giving up first and turning a slow-but-working
# merge into a fabricated failure (F2, 2026-09-22).
CARD_REPAIR_TIMEOUT = float(os.environ.get("CARD_REPAIR_TIMEOUT") or "600")


def _live_fetch(path: str, project: str) -> dict[str, Any]:
    # DrudgeClient has no public raw-POST door; _retry is the one that speaks JSON both ways.
    # project is always sent, even "" — the engine's own filter treats an explicit empty
    # string as "unassigned documents only", not "no filter" (measured 2026-09-22).
    return DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0)._retry("POST", path, {"project": project})


def _live_active_projects(active_days: int) -> list[str]:
    """The door's own GET /projects?active_days=N — DB-backed, unlike the engine's plain
    /projects (no activity filter), so this asks the door, not DrudgeClient's engine URL."""
    url = f"{_door_url()}/projects?active_days={active_days}"
    with urllib.request.urlopen(url, timeout=ENGINE_TIMEOUT) as r:
        payload = json.loads(r.read().decode("utf-8"))
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
    with urllib.request.urlopen(url, timeout=ENGINE_TIMEOUT) as r:
        payload = json.loads(r.read().decode("utf-8"))
    if payload.get("maybe_truncated"):
        raise OSError(
            f"/events?event={event_name}&since_hours={since_hours} maybe_truncated=true — "
            "a clipped judged-history window cannot ground 판정 suppression"
        )
    return payload["entries"]


def _live_past_verdicts(since_hours: int) -> list[PastVerdictPair]:
    """Join card_proposal and card_verdict events by (card_ts, idx) — a card_verdict event
    alone carries no note or evidence, only the button press (knowns: card_ts·idx·choice).
    Proposals are read over a wider window than verdicts: a press can land up to
    CARD_WAIT_HOURS after its card posted, so a verdict at hour 167 of the 168h window
    would otherwise be joined against a proposal that already fell outside it. A
    card_verdict with no matching card_proposal, or a matching one missing note/evidence/
    choice/timestamp, is a malformed row — F5/ROP: that is a visible failure (ValueError
    naming the row), never a silently skipped one, because a dropped row here is exactly a
    suppression pair going missing without anyone knowing."""
    proposal_window = since_hours + math.ceil(CARD_WAIT_HOURS)
    proposals_by_key: dict[tuple[Any, Any], dict[str, Any]] = {}
    for entry in _live_events("card_proposal", proposal_window):
        attrs = entry.get("attributes") or {}
        key = (attrs.get("card_ts"), attrs.get("idx"))
        if key[0] is not None and key[1] is not None:
            proposals_by_key[key] = attrs
    out: list[PastVerdictPair] = []
    for entry in _live_events("card_verdict", since_hours):
        attrs = entry.get("attributes") or {}
        key = (attrs.get("card_ts"), attrs.get("idx"))
        proposal = proposals_by_key.get(key)
        if proposal is None:
            raise ValueError(f"card_verdict {key!r} has no matching card_proposal event: {attrs!r}")
        evidence = proposal.get("evidence") or []
        if not evidence:
            raise ValueError(f"card_proposal {key!r} was recorded with no evidence: {proposal!r}")
        first = evidence[0]
        try:
            out.append(
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
    return out


def _live_handover(session: str, at: str, paths: list[str]) -> dict:
    return DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).handover(session, at, paths)


def _live_consumption(session: str, kind: str, paths: list[str]) -> dict:
    # Row-level, not session-level: a button judges the note its row cited, and only that one.
    # judge=OWNER: a button press is the owner's hand, and the client carries the owner token
    # the engine demands for that word.
    at = datetime.now(UTC).isoformat()
    if kind == "used":
        return DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).consumption(
            session, at, used=paths, judge=OWNER
        )
    return DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).consumption(
        session, at, contested=paths, judge=OWNER
    )


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
    with urllib.request.urlopen(url, timeout=ENGINE_TIMEOUT) as r:
        payload = json.loads(r.read().decode("utf-8"))
    return [
        PastApproved(session=item["session"], note=item["note"], at=item["at"])
        for item in payload["approved"]
    ]


def _live_record(event: str, fields: dict) -> None:
    import event_log

    event_log.append_event("slack-card", event, "ok", **fields)


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
    with urllib.request.urlopen(url, timeout=ENGINE_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _door_failure(subject: str, code: int, body: bytes) -> RepairFailed | RepairUnanswered:
    """Classify the door's HTTP-error body: counts reported → RepairFailed, anything else
    (unreadable, or JSON without both count keys) → RepairUnanswered — the rows may already
    be committed, so an unknown count must never be written as 0."""
    try:
        failed_payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return RepairUnanswered(subject=subject, reason=f"door answered {code}")
    if not isinstance(failed_payload, dict):
        return RepairUnanswered(subject=subject, reason=f"door answered {code}")
    deleted = failed_payload.get("deleted_rows")
    reread = failed_payload.get("reread_notes")
    if deleted is None or reread is None:
        reason = f"door answered {code}"
        error = failed_payload.get("error")
        if isinstance(error, str) and error:
            reason = f"{reason}: {error}"
        return RepairUnanswered(subject=subject, reason=reason)
    sync = failed_payload.get("sync")
    reason = (
        sync["error"] if isinstance(sync, dict) and sync.get("error") is not None else f"door answered {code}"
    )
    return RepairFailed(
        subject=subject,
        deleted_rows=int(deleted),
        reread_notes=int(reread),
        reason=str(reason),
        owner_held=failed_payload.get("owner_held") or [],
    )


def _live_execute_repair(subject: str) -> RepairDone | RepairFailed | RepairUnanswered:
    """POST the door's merge. The door commits DELETE+UPDATE before its sync, so a timeout
    or a count-less body may already have deleted the rows — a failure without reported
    counts is RepairUnanswered, never a fabricated 0 (F2). Never raised: a slow or failed
    merge must not end the card's whole run over one button."""
    claim = {"subject": subject, "judge": OWNER}
    req = urllib.request.Request(
        f"{_door_url()}/repairs/split-subjects",
        data=json.dumps(claim).encode(),
        headers={"content-type": "application/json", **owner_headers(claim)},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=CARD_REPAIR_TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except OSError:
            body = b""
        return _door_failure(subject, e.code, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return RepairUnanswered(subject=subject, reason=f"door unreachable: {e}")
    return RepairDone(
        subject=subject,
        deleted_rows=payload["deleted_rows"],
        reread_notes=payload["reread_notes"],
        remaining_variants=payload.get("remaining_variants"),
        owner_held=payload.get("owner_held") or [],
    )


def _live_merged_yesterday() -> int | None:
    """Sum of yesterday's subject_merged deleted_rows, or None when nothing merged — the head
    line's optional clause. Read straight from the engine's /events, the same route
    _live_past_verdicts already reads."""
    entries = _live_events("subject_merged", 24)
    if not entries:
        return None
    return sum(int((e.get("attributes") or {}).get("deleted_rows") or 0) for e in entries)


def _door_url() -> str:
    return os.environ["BORING_DOOR_URL"].rstrip("/")


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
