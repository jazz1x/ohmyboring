#!/usr/bin/env python3
"""The card's write-side effects — everything a button press can do to the outside world.

card_press.effects turns a press into a list of Effect values; this module is the one
interpreter that applies them, through the fold `run`, plus the three live functions the
fold calls (record/consumption/execute_repair). The hermes plugin
(agents/hermes/plugins/boring-card) is the one folder now — one decision table, one
interpreter, wherever a press lands.

This module must stay importable inside the hermes venv: it depends only on card_types
and the shared engine clients (plus stdlib), never on card_live — the read side pulls
langchain through the advice seam's retriever, and the hermes venv has no langchain.
ENGINE_TIMEOUT lives here rather than in card_live because the write side needs it too
and card_effects cannot import card_live."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", ".."))
import card_types
from card_types import RepairDone, RepairFailed, RepairUnanswered

from ohmyboring.adapters.engine import OWNER, ConsumptionMarks, DrudgeClient, owner_headers

# Register answers carry up to 50 claims each; the door timeout lesson (brief p95 77s) says the
# point read is far cheaper, but a cold engine still earns more than a point-read default.
ENGINE_TIMEOUT = float(os.environ.get("CARD_ENGINE_TIMEOUT") or "30")
# execute_repair's own timeout — separate from ENGINE_TIMEOUT (30s), which every quick
# point-read here shares. The door's POST blocks on the engine's synchronous /sync (global
# lock, full re-embed of every reread note); a 212-note group is not a point read. 600s is
# the door's own upstream ceiling headroom (DOOR_TIMEOUT=130s per hop, but /sync's own cost
# scales with note count, not request count) — long enough that a real repair's sync finishes
# under it rather than the card's http client giving up first and turning a slow-but-working
# merge into a fabricated failure (F2, 2026-09-22).
CARD_REPAIR_TIMEOUT = float(os.environ.get("CARD_REPAIR_TIMEOUT") or "600")


def run(
    effects: list[card_types.Effect],
    record: Callable[[str, dict], None],
    consumption: Callable[[str, str, list[str]], None],
    execute_repair: Callable[[str], RepairDone | RepairFailed | RepairUnanswered],
) -> list[RepairDone | RepairFailed | RepairUnanswered]:
    """The one interpreter: a card_press.effects list in, applied in order. Each Effect tag
    picks its collaborator; the list's order is the execution order, and the fold stops at
    the first failure — the same exception keeps propagating, annotated with the effect
    that failed (`card_failed_effect`) and how many effects behind it were skipped
    (`card_effects_skipped`) so a catcher can log one precise line. Returns the
    execute_repair results (a press carries at most one) — the only effects with a value
    worth handing back. An unknown tag raises: a decision table this small has no fourth
    kind, and a quiet skip here would be a press that half-happened."""
    repairs: list[RepairDone | RepairFailed | RepairUnanswered] = []
    for i, effect in enumerate(effects):
        try:
            if effect.effect == "record":
                record(effect.event, effect.fields)
            elif effect.effect == "consumption":
                consumption(effect.session, effect.kind, effect.paths)
            elif effect.effect == "execute_repair":
                repairs.append(execute_repair(effect.subject))
            else:
                raise ValueError(f"unknown effect tag {effect.effect!r}")
        except Exception as e:
            e.card_failed_effect = effect
            e.card_effects_skipped = len(effects) - i - 1
            raise
    return repairs


def _live_record(event: str, fields: dict) -> None:
    import event_log

    event_log.append_event("slack-card", event, "ok", **fields)


def _live_consumption(session: str, kind: str, paths: list[str]) -> dict:
    # Row-level, not session-level: a button judges the note its row cited, and only that one.
    # judge=OWNER: a button press is the owner's hand, and the client carries the owner token
    # the engine demands for that word.
    at = datetime.now(UTC).isoformat()
    if kind == "used":
        return DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).consumption(
            session, at, ConsumptionMarks(used=paths, judge=OWNER)
        )
    return DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).consumption(
        session, at, ConsumptionMarks(contested=paths, judge=OWNER)
    )


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


def _door_url() -> str:
    return os.environ["BORING_DOOR_URL"].rstrip("/")
