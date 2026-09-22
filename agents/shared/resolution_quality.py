#!/usr/bin/env python3
"""Resolution contract checks for distilled session notes.

This module is intentionally pure: it does not call the LLM, read the vault, or
write markers. It answers one question before a note is remembered: is this note
specific enough for the requested resolution level?
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

ALLOWED_RESOLUTIONS = {"compact", "standard", "evidence", "forensic"}
ALLOWED_CLAIM_KINDS = {"fact", "decision", "assumption", "risk", "blocked", "goal", "term", "next"}

# A section "signal" (heading word) only counts once real prose sits under it — a bare
# "## Decision" with nothing beneath is not a decision section. SSOT for that content-length
# floor, env-overridable. Kept at 1 (any non-whitespace char) so this only catches the
# genuinely-empty case and does not tighten grading beyond that (see TASK C1: over-tightening
# risked flipping already-passing notes to red).
SECTION_MIN_CONTENT_CHARS = int(os.environ.get("BORING_SECTION_MIN_CONTENT_CHARS", "1"))

_ATX_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")

SECTION_SIGNALS = {
    "problem": ("background", "problem", "context", "배경", "문제", "背景", "問題"),
    "as_is": ("as-is", "as is", "before", "current state", "현재", "이전 상태", "現状", "以前"),
    "to_be": ("to-be", "to be", "after", "target state", "목표", "목표 상태", "目標", "あるべき姿"),
    "decision": ("decision", "decided", "결정", "선택", "決定", "判断"),
    "evidence": (
        "evidence",
        "basis",
        "command",
        "verified",
        "근거",
        "명령",
        "검증",
        "根拠",
        "コマンド",
        "検証",
    ),
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
	      |\b(?:make|cargo|python3?|pytest|ruff|mypy|uv|npm|pnpm|bun|node|swift-format|swiftlint|swift|xcodebuild|git|gh|docker|ollama|pre-commit)\b
	      |\b[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){1,}\b
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


def normalize_resolution(resolution: str | None, default: str = "standard") -> str:
    fallback = default if default in ALLOWED_RESOLUTIONS else "standard"
    value = (resolution or "standard").strip().lower()
    if value not in ALLOWED_RESOLUTIONS:
        return fallback
    return value


def verify_note_resolution(
    note: dict[str, Any],
    transcript: str = "",
    resolution: str | None = None,
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
    elif not body_survives_storage_normalize(body):
        missing.append("body:storage-normalize-empty")

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
    required_tokens = _required_evidence_tokens(int(rule["min_evidence_tokens"]), seen)
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


def resolution_prompt_contract(resolution: str | None) -> str:
    level = normalize_resolution(resolution)
    rule = RESOLUTION_RULES[level]
    sections = ", ".join(rule["sections"])
    kinds = ", ".join(rule["claim_kinds"]) or "any relevant claim kind"
    return (
        f"RESOLUTION CONTRACT: {level} — {RESOLUTION_DESCRIPTIONS[level]}.\n"
        f"- Required body signals: {sections}.\n"
        f"- Minimum claims: {rule['min_claims']}.\n"
        f"- Required claim kinds: {kinds}.\n"
        f"- Required claim kinds are hard gates: emit at least one claim for each kind in [{kinds}], "
        'using the exact JSON value (for example "kind":"decision"), never a synonym.\n'
        f"- Minimum preserved evidence tokens: {rule['min_evidence_tokens']}.\n"
        "- Preserve concrete evidence from the transcript: PR numbers, ticket ids, model names, "
        "commands, durations, counts, statuses, before/after values. Do not invent missing evidence.\n"
    )


def _claims(note: dict[str, Any]) -> list[dict[str, Any]]:
    claims = note.get("claims") or []
    return [c for c in claims if isinstance(c, dict)]


def _has_section_signal(body: str, section: str) -> bool:
    lower = _prose_only(body).lower()
    return any(signal.lower() in lower for signal in SECTION_SIGNALS[section])


def _prose_only(body: str) -> str:
    """`body` with content-free ATX headings dropped.

    A heading with nothing beneath it (before the next heading, or EOF) only NAMES a
    section — it does not make the section exist. Without this, "## Decision" alone would
    satisfy the decision-section signal even though nothing was ever decided. Headings that
    DO have content, and any prose that sits outside heading structure, are left untouched —
    including a signal word that happens to appear inside real prose elsewhere in the body.
    That leniency is deliberate: tightening it further risks flipping already-passing notes to
    red, which is a worse regression than the coincidental match it would prevent.
    """
    lines = body.split("\n")
    keep = [True] * len(lines)
    for i, line in enumerate(lines):
        if not _ATX_HEADING_RE.match(line):
            continue
        content_len = 0
        for other in lines[i + 1 :]:
            if _ATX_HEADING_RE.match(other):
                break
            content_len += len(other.strip())
        if content_len < SECTION_MIN_CONTENT_CHARS:
            keep[i] = False
    return "\n".join(line for line, k in zip(lines, keep) if k)


# --- storage-precondition check -------------------------------------------------------------
# drudge/src/vault/remember.rs::normalize_body is the SSOT for what gets written to the vault:
# it decodes a small set of JSON-escaped characters, then repeatedly strips a *trailing* ATX
# heading that has no content beneath it (drudge::vault::remember::strip_trailing_empty_heading).
# A body that is nothing but empty headings collapses to "" there and the write gate rejects it
# with "missing argument: body" — after the resolution gate already said the note was fine.
#
# The functions below PREDICT that outcome; they do not replace it and must never be used to
# rewrite the body actually sent to `remember` (only drudge normalizes for storage — see
# remember.rs:74-79). Duplicating the transform as a second writer would reintroduce exactly the
# drift this file exists to close. This is a precondition check only: "would storage reject this",
# answered before the HTTP round-trip, not "here is the normalized body to send".
_BODY_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)
_BODY_ESCAPE_DECODE = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "`": "`",
    "#": "#",
    '"': '"',
    "*": "*",
    "_": "_",
    "[": "[",
    "]": "]",
    "(": "(",
    ")": ")",
    "\\": "\\",
}


def _decode_body_escapes(body: str) -> str:
    """Mirror drudge's normalize_body escape decoding (remember.rs ~80-103), predict-only."""
    return _BODY_ESCAPE_RE.sub(lambda m: _BODY_ESCAPE_DECODE.get(m.group(1), "\\" + m.group(1)), body)


def _strip_trailing_empty_headings(body: str) -> str:
    """Mirror drudge's strip_trailing_empty_heading (remember.rs:55-70), predict-only."""
    lines = body.split("\n")
    while lines:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and _ATX_HEADING_RE.match(lines[-1].lstrip()):
            lines.pop()
            continue
        break
    return "\n".join(lines)


def body_survives_storage_normalize(body: str) -> bool:
    """True if drudge's normalize_body would keep non-empty content for this body.

    Predicts (does not perform) the write-gate normalization owned by
    drudge/src/vault/remember.rs::normalize_body, so a note doomed to collapse into "" at
    storage can be caught before `remember` is ever called, instead of dying inside the MCP
    call with "missing argument: body".
    """
    decoded = _decode_body_escapes(body).strip()
    return bool(_strip_trailing_empty_headings(decoded).strip())


def _search_text(title: str, body: str, claims: list[dict[str, Any]]) -> str:
    parts = [title, body]
    for claim in claims:
        parts.extend(str(claim.get(field) or "") for field in ("subject", "predicate", "value"))
    return "\n".join(parts)


def _required_evidence_tokens(configured_min: int, seen: tuple[str, ...]) -> int:
    if not seen:
        return configured_min
    return min(configured_min, len(seen))


def _evidence_tokens(text: str) -> tuple[str, ...]:
    tokens = []
    seen = set()
    for m in EVIDENCE_TOKEN_RE.finditer(text):
        token = re.sub(r"\s+", "", m.group(0).lower())
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tuple(tokens)
