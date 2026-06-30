#!/usr/bin/env python3
"""Resolution contract checks for distilled session notes.

This module is intentionally pure: it does not call the LLM, read the vault, or
write markers. It answers one question before a note is remembered: is this note
specific enough for the requested resolution level?
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional


ALLOWED_RESOLUTIONS = {"compact", "standard", "evidence", "forensic"}
ALLOWED_CLAIM_KINDS = {"fact", "decision", "assumption", "risk", "blocked", "goal", "term", "next"}

SECTION_SIGNALS = {
    "problem": ("background", "problem", "context", "배경", "문제", "背景", "問題"),
    "as_is": ("as-is", "as is", "before", "current state", "현재", "이전 상태", "現状", "以前"),
    "to_be": ("to-be", "to be", "after", "target state", "목표", "목표 상태", "目標", "あるべき姿"),
    "decision": ("decision", "decided", "결정", "선택", "決定", "判断"),
    "evidence": ("evidence", "basis", "command", "verified", "근거", "명령", "검증", "根拠", "コマンド", "検証"),
    "result": ("result", "outcome", "결과", "상태", "結果", "解決", "状態"),
    "next": ("next", "remaining", "follow-up", "다음", "남은 일", "次", "残件", "残作業"),
    "timeline": ("timeline", "sequence", "타임라인", "시점", "タイムライン", "時系列"),
    "root_cause": ("root cause", "cause", "원인", "근본원인", "原因", "根本原因"),
    "regression": ("regression", "fixture", "repro", "회귀", "재현", "回帰", "再現", "フィクスチャ"),
}

RESOLUTION_RULES = {
    "compact": {
        "min_claims": 1,
        "sections": ("problem", "result"),
        "claim_kinds": (),
        "min_evidence_tokens": 0,
    },
    "standard": {
        "min_claims": 2,
        "sections": ("problem", "decision", "result"),
        "claim_kinds": ("decision",),
        "min_evidence_tokens": 1,
    },
    "evidence": {
        "min_claims": 4,
        "sections": ("problem", "as_is", "to_be", "decision", "evidence", "result", "next"),
        "claim_kinds": ("decision", "fact"),
        "min_evidence_tokens": 2,
    },
    "forensic": {
        "min_claims": 6,
        "sections": (
            "problem",
            "as_is",
            "to_be",
            "timeline",
            "root_cause",
            "decision",
            "evidence",
            "result",
            "regression",
            "next",
        ),
        "claim_kinds": ("decision", "fact", "risk", "next"),
        "min_evidence_tokens": 3,
    },
}

RESOLUTION_DESCRIPTIONS = {
    "compact": "short note for tiny or mostly conversational sessions",
    "standard": "normal work note with decision and result detail",
    "evidence": "release/bug/verification note with as-is, to-be, evidence, numbers, and next actions",
    "forensic": "incident/regression note with timeline, root cause, rejected risk, fixture, and next actions",
}

EVIDENCE_TOKEN_RE = re.compile(
    r"""
    (?:
      \bPR\s*\#?\d+\b
      |\B\#\d+\b
      |\b[A-Z]{2,}-\d+\b
      |\b[a-z][a-z0-9._/-]*:[0-9][a-z0-9._/-]*\b
      |\b\d+(?:h|m|s)(?:\d+(?:h|m|s))*\b
      |\b\d+(?:\.\d+)?(?:ms|s|m|h|d|kb|mb|gb|%|개|건|회|초|분|시간)?\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class ResolutionReport:
    resolution: str
    ok: bool
    missing: tuple[str, ...]
    claim_count: int
    evidence_tokens_seen: tuple[str, ...]
    evidence_tokens_kept: tuple[str, ...]


def normalize_resolution(resolution: Optional[str], default: str = "standard") -> str:
    fallback = default if default in ALLOWED_RESOLUTIONS else "standard"
    value = (resolution or "standard").strip().lower()
    if value not in ALLOWED_RESOLUTIONS:
        return fallback
    return value


def verify_note_resolution(
    note: dict[str, Any],
    transcript: str = "",
    resolution: Optional[str] = None,
) -> ResolutionReport:
    level = normalize_resolution(resolution)
    rule = RESOLUTION_RULES[level]
    body = str(note.get("body") or "")
    title = str(note.get("title") or "")
    claims = _claims(note)
    body_and_claims = _search_text(title, body, claims)

    missing: list[str] = []
    if not title.strip():
        missing.append("title")
    if not body.strip():
        missing.append("body")

    for section in rule["sections"]:
        if not _has_section_signal(body, section):
            missing.append(f"section:{section}")

    if len(claims) < int(rule["min_claims"]):
        missing.append(f"claims:min:{rule['min_claims']}")

    kinds = {str(c.get("kind") or "fact").strip().lower() for c in claims}
    for kind in rule["claim_kinds"]:
        if kind not in kinds:
            missing.append(f"claim-kind:{kind}")

    for c in claims:
        kind = str(c.get("kind") or "fact").strip().lower()
        if kind not in ALLOWED_CLAIM_KINDS:
            missing.append(f"claim-kind-invalid:{kind}")
        if not str(c.get("subject") or "").strip():
            missing.append("claim-field:subject")
        if not str(c.get("predicate") or "").strip():
            missing.append("claim-field:predicate")
        if not str(c.get("value") or "").strip():
            missing.append("claim-field:value")

    seen = _evidence_tokens(transcript)
    kept = tuple(t for t in seen if t in _evidence_tokens(body_and_claims))
    required_tokens = int(rule["min_evidence_tokens"])
    if len(kept) < required_tokens:
        missing.append(f"evidence-tokens:min:{required_tokens}")

    return ResolutionReport(
        resolution=level,
        ok=not missing,
        missing=tuple(dict.fromkeys(missing)),
        claim_count=len(claims),
        evidence_tokens_seen=seen,
        evidence_tokens_kept=kept,
    )


def resolution_prompt_contract(resolution: Optional[str]) -> str:
    level = normalize_resolution(resolution)
    rule = RESOLUTION_RULES[level]
    sections = ", ".join(rule["sections"])
    kinds = ", ".join(rule["claim_kinds"]) or "any relevant claim kind"
    return (
        f"RESOLUTION CONTRACT: {level} — {RESOLUTION_DESCRIPTIONS[level]}.\n"
        f"- Required body signals: {sections}.\n"
        f"- Minimum claims: {rule['min_claims']}.\n"
        f"- Required claim kinds: {kinds}.\n"
        f"- Minimum preserved evidence tokens: {rule['min_evidence_tokens']}.\n"
        "- Preserve concrete evidence from the transcript: PR numbers, ticket ids, model names, "
        "commands, durations, counts, statuses, before/after values. Do not invent missing evidence.\n"
    )


def _claims(note: dict[str, Any]) -> list[dict[str, Any]]:
    claims = note.get("claims") or []
    return [c for c in claims if isinstance(c, dict)]


def _has_section_signal(body: str, section: str) -> bool:
    lower = body.lower()
    return any(signal.lower() in lower for signal in SECTION_SIGNALS[section])


def _search_text(title: str, body: str, claims: list[dict[str, Any]]) -> str:
    parts = [title, body]
    for claim in claims:
        parts.extend(str(claim.get(field) or "") for field in ("subject", "predicate", "value"))
    return "\n".join(parts)


def _evidence_tokens(text: str) -> tuple[str, ...]:
    tokens = []
    seen = set()
    for m in EVIDENCE_TOKEN_RE.finditer(text):
        token = re.sub(r"\s+", "", m.group(0).lower())
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tuple(tokens)
