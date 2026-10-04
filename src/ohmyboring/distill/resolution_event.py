"""증류 결과를 `distill_resolution` 사건으로 적는다 — 호스트 훅과 그래프 노드가 같이 쓴다."""

from __future__ import annotations

import sys

from ohmyboring.adapters import events, workflow_contract
from ohmyboring.distill import polish


def _polish_event_fields(outcome):
    """polish ADT 를 사건 필드로 펼친다 — None 이면 polish 가 안 돈 길(repair_failed·give_up)이라 필드가 빠진다."""
    if outcome is None:
        return None, None
    if isinstance(outcome, polish.Polished):
        return "polished", None
    return "kept", outcome.reason


def log_resolution_event(  # noqa: PLR0913
    session_id, origin, repo, report, verifier_status, remember_status, polish_outcome=None
):
    ok = verifier_status in {"pass", "repaired"} and remember_status in {"remembered", "duplicate"}
    polish_status, polish_reason = _polish_event_fields(polish_outcome)
    try:
        events.append_event(
            "distill-session",
            "distill_resolution",
            "ok" if ok else "failed",
            session_id=session_id,
            origin=origin,
            repo=repo,
            resolution=report.resolution,
            verifier_status=verifier_status,
            missing_fields=list(report.missing),
            claim_count=report.claim_count,
            numbers_seen=len(report.evidence_tokens_seen),
            numbers_kept=len(report.evidence_tokens_kept),
            remember_status=remember_status,
            polish_status=polish_status,
            polish_reason=polish_reason,
            **workflow_contract.resolution_fields(verifier_status, remember_status),
        )
    except OSError as e:
        print(f"[distill-session] event log write failed: {e}", file=sys.stderr)


def log_skip_event(session_id, origin, repo, resolution, reason):
    events.try_append_event(
        "distill-session",
        "distill_resolution",
        "ok",
        session_id=session_id,
        origin=origin,
        repo=repo,
        resolution=resolution,
        verifier_status="skipped",
        missing_fields=[],
        claim_count=0,
        numbers_seen=0,
        numbers_kept=0,
        remember_status="skipped",
        reason=reason,
        **workflow_contract.skip_fields(),
    )
