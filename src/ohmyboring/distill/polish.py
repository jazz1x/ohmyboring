"""Polish a note body so it reads — and keep the original whenever the rewrite loses a fact or
reads no better. The model only proposes; code decides (langgraph-practice agreed-by-three)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ohmyboring.distill import readability
from ohmyboring.distill.prompts.polish import build_polish_prompt

_FACT_PATTERNS = (
    re.compile(r"wiki-\d{4}"),
    re.compile(r"`[^`\n]+`"),
    re.compile(r"https?://[^\s)>\]]+"),
    re.compile(r"#\d+"),
    re.compile(r"\d[\d.,:%/-]*\d|\d{2,}"),
)


# 1500 — the same value as the fixed chunk size (ingest/chunk.py fixed_chunks size=1500). Deliberately
# a constant of its own: distill must not import ingest, and both must move together by decision.
MAX_POLISHED_CHARS = 1500


@dataclass(frozen=True)
class Polished:
    body: str


@dataclass(frozen=True)
class Kept:
    reason: str


@dataclass(frozen=True)
class LostFacts(Kept):
    """The rewrite dropped fact(s) the original carried — tell the model what fell out and retry."""


@dataclass(frozen=True)
class MissedShape(Kept):
    """The rewrite misses the owner-approved markdown shape — retry once with the shape named."""


@dataclass(frozen=True)
class NoBetter(Kept):
    """The rewrite trips at least as many readability signals as the original."""


@dataclass(frozen=True)
class NoBody(Kept):
    """The model returned nothing usable (no body, empty, or not a string)."""


@dataclass(frozen=True)
class TooLong(Kept):
    """The rewrite is over MAX_POLISHED_CHARS and longer than the original (wiki-2476)."""


@dataclass(frozen=True)
class AlreadyReadable(Kept):
    """The original already reads — the model is never asked."""


# Which rejections earn the single retry edge in the graph: facts lost or shape missed, nothing else
# (wiki-2475·2476). The graph routes on this tuple, never on the reason string.
RETRYABLE_KEPT = (LostFacts, MissedShape)

PolishResult = Polished | Kept


# Linter rule codes (PLR0913, B008, E402, C901) mean nothing to the reader; the owner allowed
# dropping them (2026-09-30), so they are not facts a rewrite must keep.
_LINT_CODE = re.compile(r"\b[A-Z]{1,4}\d{3,4}\b")


def facts(text: str) -> set[str]:
    """The strings a rewrite must carry over verbatim: wiki ids, `code` spans, URLs, #refs, and
    multi-digit numbers (a single digit is too often a list marker to count)."""
    plain = _LINT_CODE.sub("", text)
    return {match for pattern in _FACT_PATTERNS for match in pattern.findall(plain)}


def shape_problems(body: str) -> list[str]:
    """What the approved markdown shape needs and `body` lacks: plain summary lines before the
    first heading, at least two `## ` sections, and list items (or table rows) under them."""
    lines = [line for line in body.splitlines() if line.strip()]
    first_heading = next((i for i, line in enumerate(lines) if line.startswith("## ")), None)
    problems = []
    if first_heading is None or first_heading == 0:
        problems.append("no summary before the first section")
    if sum(line.startswith("## ") for line in lines) < 2:
        problems.append("fewer than two ## sections")
    if not any(re.match(r"\s*(-|\d+\.)\s|\s*\|.*\|\s*$", line) for line in lines):
        problems.append("no list items or table rows")
    return problems


def judge(original: str, rewritten: object) -> PolishResult:
    """Whether `rewritten` may replace `original`: it must be text, stay under the length guard,
    carry every fact, and trip fewer body readability signals — otherwise the original stays, as
    a tagged Kept naming why (the model only proposes; this function decides)."""
    if not isinstance(rewritten, str) or not rewritten.strip():
        return NoBody(reason="the model returned no body")
    if len(rewritten) > MAX_POLISHED_CHARS and len(rewritten) > len(original):
        return TooLong(
            reason=f"rewrite is {len(rewritten)} chars — over {MAX_POLISHED_CHARS} and longer than the original"
        )
    missing = facts(original) - facts(rewritten)
    if missing:
        return LostFacts(reason=f"rewrite lost {len(missing)} fact(s): {', '.join(sorted(missing)[:5])}")
    before, after = readability.body_signals(original), readability.body_signals(rewritten)
    if len(after) >= len(before):
        return NoBetter(reason=f"rewrite reads no better ({before or 'clean'} → {after or 'clean'})")
    shape = shape_problems(rewritten)
    if shape:
        return MissedShape(reason=f"rewrite misses the markdown shape: {', '.join(shape)}")
    return Polished(body=rewritten.strip() + "\n")


def polish(
    body: str, note_lang: str, call_llm: Callable[[str], Any], retry_reason: str | None = None
) -> PolishResult:
    """One polish attempt. A body that already reads is left alone without calling the model;
    `retry_reason` carries the first rejection into the prompt (the lost-fact list verbatim)."""
    if not readability.body_signals(body):
        return AlreadyReadable(reason="already readable")
    answer = call_llm(build_polish_prompt(body, note_lang, retry_reason=retry_reason))
    return judge(body, answer.get("body") if isinstance(answer, dict) else None)
