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
    ResolvedNote,
    Unresolved,
)
from drudge_client import DrudgeClient
from pydantic import ValidationError

# Register answers carry up to 50 claims each; the door timeout lesson (brief p95 77s) says the
# point read is far cheaper, but a cold engine still earns more than a point-read default.
ENGINE_TIMEOUT = float(os.environ.get("CARD_ENGINE_TIMEOUT") or "30")
# A card's own lifespan — the next card's post outlives any button the owner never got to
# press, so waiting past this is polling a socket nobody is going to answer on.
CARD_WAIT_HOURS = float(os.environ.get("CARD_WAIT_HOURS") or "23")


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
    at = datetime.now(UTC).isoformat()
    if kind == "used":
        return DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).consumption(session, at, used=paths)
    return DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).consumption(session, at, contested=paths)


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


def _door_url() -> str:
    return os.environ["BORING_DOOR_URL"].rstrip("/")


def _live_search(subject: str) -> list[dict[str, Any]]:
    """A candidate's past record — `/search` with claims, the door's own recall of what the
    engine has decided about this subject before (wiki-1765 step 1)."""
    return DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).search(subject, max_results=3, claims=3)


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
