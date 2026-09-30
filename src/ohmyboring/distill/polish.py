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


@dataclass(frozen=True)
class Polished:
    body: str


@dataclass(frozen=True)
class Kept:
    reason: str


PolishResult = Polished | Kept


def facts(text: str) -> set[str]:
    """The strings a rewrite must carry over verbatim: wiki ids, `code` spans, URLs, #refs, and
    multi-digit numbers (a single digit is too often a list marker to count)."""
    return {match for pattern in _FACT_PATTERNS for match in pattern.findall(text)}


def judge(original: str, rewritten: object) -> PolishResult:
    """Whether `rewritten` may replace `original`: it must be text, carry every fact, and trip
    fewer body readability signals — otherwise the original stays, with the reason."""
    if not isinstance(rewritten, str) or not rewritten.strip():
        return Kept(reason="the model returned no body")
    missing = facts(original) - facts(rewritten)
    if missing:
        return Kept(reason=f"rewrite lost {len(missing)} fact(s): {', '.join(sorted(missing)[:5])}")
    before, after = readability.body_signals(original), readability.body_signals(rewritten)
    if len(after) >= len(before):
        return Kept(reason=f"rewrite reads no better ({before or 'clean'} → {after or 'clean'})")
    return Polished(body=rewritten.strip() + "\n")


def polish(body: str, note_lang: str, call_llm: Callable[[str], Any]) -> PolishResult:
    """One polish attempt. A body that already reads is left alone without calling the model."""
    if not readability.body_signals(body):
        return Kept(reason="already readable")
    answer = call_llm(build_polish_prompt(body, note_lang))
    return judge(body, answer.get("body") if isinstance(answer, dict) else None)
