#!/usr/bin/env python3
"""The morning card's suppression, event-field shaping, and button-press parsing.

suppressed drops a resolved candidate whose (note, evidence) pair already got a judged
verdict in the last 7 days or was shown but never judged in the last 3 days;
proposal_event_fields/verdict_event_fields/handover_paths shape what the card tells the
engine's event log; confirm_past partitions past 「해」 approvals against today's registers;
parse_action turns a Slack block_actions payload into a ButtonVerdict or a Rejected value,
never an exception."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from card_types import (
    CHOICES,
    NO_CURRENT_CLAIM,
    ButtonVerdict,
    Confirmation,
    PastApproved,
    PastCardHistory,
    Proposal,
    Rejected,
)

#: The suppression window, hours — 7 days, matching the contract's "지난 7일" rule.
SUPPRESS_WINDOW_HOURS = 168
#: The rest window, hours — 사흘: a proposal shown but never judged rests this long before
#: the card may propose it again, so a card that got no button does not repeat every morning.
REST_HOURS = 72


def suppressed(
    candidates: list[Proposal],
    past: PastCardHistory,
    now: datetime | None = None,
) -> tuple[list[Proposal], list[Proposal]]:
    """Split resolved candidates into (kept, dropped): a candidate is dropped when its own
    note and its first evidence's (note, line) match a pair that already got a 해/빼 verdict
    within the last 7 days, or a shown-but-never-judged pair from the last REST_HOURS — an
    unanswered proposal rests, then may come back. 미뤄 wins over both rules: it suppresses
    nothing, and a key carrying any judged verdict — 미뤄 포함 — is never rested, because a
    verdict answers the pair and rest applies only to the never-judged. A pair older than
    its window does not drop either: the owner may see the same bottleneck again once
    enough time has passed for it to be worth asking again. A `past` entry whose `at` does
    not parse is not skipped: a judged-history row this function cannot place in time is
    exactly the case the contract calls a wrong card, not a quiet gap — it raises, same as
    an unresolvable pair upstream in card.py's own read."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=SUPPRESS_WINDOW_HOURS)
    rest_cutoff = now - timedelta(hours=REST_HOURS)
    judged = {(v.note, v.evidence_note, v.evidence_line) for v in past.judged}
    recent: set[tuple[str, str, int]] = set()
    for verdict in past.judged:
        if verdict.choice not in ("do", "drop"):
            continue
        at = datetime.fromisoformat(verdict.at)
        if at < cutoff:
            continue
        recent.add((verdict.note, verdict.evidence_note, verdict.evidence_line))
    rested: set[tuple[str, str, int]] = set()
    for pair in past.unanswered:
        if (pair.note, pair.evidence_note, pair.evidence_line) in judged:
            continue
        at = datetime.fromisoformat(pair.at)
        if at < rest_cutoff:
            continue
        rested.add((pair.note, pair.evidence_note, pair.evidence_line))
    kept: list[Proposal] = []
    dropped: list[Proposal] = []
    for candidate in candidates:
        if not candidate.evidence:
            kept.append(candidate)
            continue
        key = (candidate.note, candidate.evidence[0].note, candidate.evidence[0].line)
        (dropped if key in recent or key in rested else kept).append(candidate)
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
        "evidence": [e.model_dump(exclude={"superseded_by"}) for e in proposal.evidence],
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


def is_absence(reason: str) -> bool:
    """404 establishes absence; 5xx·불통 leave the question open."""
    return reason == NO_CURRENT_CLAIM


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


def parse_action(
    payload: dict,
    *,
    owner_id: str | None,
    n_total: int,
    at: str | None = None,
) -> ButtonVerdict | Rejected:
    """A block_actions payload in, one verdict out — or Rejected with the reason. Nothing here
    raises: a weird button is a fact about the world, not a crash. When an owner is configured,
    nobody else's press counts. `n_total` bounds idx across all three card lanes — repair rows,
    then advice rows, then the review rows, one shared index space (card.py's record_verdict
    splits on it)."""

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
    if idx < 0 or idx >= n_total:
        return Rejected(reason=f"no proposal {idx}")
    user = (payload.get("user") or {}).get("id") or ""
    if not user:
        return Rejected(reason="no user")
    if owner_id is not None and user != owner_id:
        return Rejected(reason=f"user {user} is not the owner")
    return ButtonVerdict(idx=idx, choice=choice, user=user, at=at or datetime.now(UTC).isoformat())
