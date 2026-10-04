"""해상도 검증 — 빠진 주장 종류와 증거 토큰을 채운 뒤 보고서를 낸다."""

from __future__ import annotations

from ohmyboring.distill.nodes import claims, evidence
from ohmyboring.distill.resolution import verify_note_resolution


def verified(note, state):
    note = claims.ensure_required_claim_kinds(note, state["resolution"], state["repo"])
    note = evidence.ensure_required_evidence_tokens(note, state["text"], state["resolution"])
    report = verify_note_resolution(
        {"title": note["title"], "body": note["body"], "claims": note["claims"]},
        transcript=state["text"],
        resolution=state["resolution"],
    )
    return note, report


def verify(state):
    note, report = verified(state["note"], state)
    return {"note": note, "report": report, "verified": report.ok}


def repair_verify(state):
    note, report = verified(state["repaired_note"], state)
    return {"repaired_note": note, "repaired_report": report, "repaired_verified": report.ok}
