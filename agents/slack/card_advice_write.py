#!/usr/bin/env python3
"""The pre-card advice step — the morning card's advice is written here, ahead of the card.

The 08:00 card run makes no model call: this program (cron `advice-write`, before the card, the
same slot as card_repair_judge.py) runs card.build_advice_graph, which picks the day's proposals
the way the card's advise node used to, rewrites each once for a first-time reader, and leaves
one `card_advice_written` 사건 per proposal. The card only reads them back (written_proposals).

The rewrite has one goal (the owner understands it on first read) and one line (no inside
talk), and code decides whether it may replace the original: a rewrite that still trips the
checker after a second pass, loses a number, or does not parse leaves the original on the 사건
with the reason in `revise`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "src"))

import card_advice  # noqa: E402
import card_types  # noqa: E402
from card_types import MIN_ADVICE_CHARS, MIN_BOTTLENECK_CHARS, Proposal  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from ohmyboring.distill import polish  # noqa: E402

#: The 사건 the card reads.
EVENT = "card_advice_written"

#: How far back the card looks for it — a day is picked by the row's own `day`, this only
#: bounds the read.
WINDOW_HOURS = 48

#: One rewrite, plus one more if the checker still flags something.
REWRITE_PASSES = 2

_KST = timezone(timedelta(hours=9))

Revise = Literal["rewritten", "kept_original", "lost_facts", "unusable"]

REVISE_HEAD = "아래 두 문장을 다시 쓴다."

# ASCII-only edges: a Korean particle glued to a name ("advise_stats를") must still match.
_L = r"(?<![A-Za-z0-9_\-])"
_R = r"(?![A-Za-z0-9_\-])"
_PATTERNS = (
    re.compile(rf"{_L}wiki-\d{{3,}}"),
    re.compile(r"(?:^|[\s(\"'])(?:/[\w.\-]+){2,}"),
    re.compile(rf"{_L}[\w\-]+(?:/[\w.\-]+)+\.[A-Za-z]{{1,5}}{_R}"),
    re.compile(rf"{_L}[\w\-]+\.(?:py|rs|md|json|toml|sh|ts|yml|yaml){_R}"),
    re.compile(rf"(?<![0-9A-Za-z])(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{{7,40}}{_R}"),
    re.compile(rf"{_L}[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+{_R}"),
    re.compile(rf"{_L}[a-z][a-z0-9]*(?:-[a-z0-9]+)+{_R}"),
    re.compile(rf"{_L}(?:[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z]+[A-Z][A-Za-z0-9]*){_R}"),
    re.compile(r"`[^`\n]+`"),
)


def today_kst() -> str:
    return datetime.now(_KST).date().isoformat()


def internal_names(text: str) -> list[str]:
    """What in `text` is inside talk to a first-time reader: wiki numbers, file paths, commit
    hashes, snake/kebab/camel identifiers, backtick spans. Deterministic — the model never
    judges its own line."""
    found: list[str] = []
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            token = match.group(0).strip(" \t\n(\"'")
            if token and token not in found:
                found.append(token)
    return found


@dataclass(frozen=True)
class Revised:
    proposal: Proposal
    revise: Revise


def is_revise_prompt(prompt: str) -> bool:
    return prompt.startswith(REVISE_HEAD)


def build_revise_prompt(bottleneck: str, advice: str, lang: str, flagged: list[str]) -> str:
    """One goal, one line. After a flagged pass the same prompt carries what was left."""
    lines = [
        REVISE_HEAD,
        "목표: 처음 읽는 사람이 한 번에 이해한다.",
        "선: 내부 이름·영어 식별자·wiki 번호·파일 경로·커밋 해시를 쓰지 않는다.",
    ]
    if flagged:
        lines.append(f"앞서 고친 글에 이게 남았다: {', '.join(flagged)}")
    lines += [
        "",
        f"병목: {bottleneck}",
        f"오늘 할 것: {advice}",
        "",
        '아래 JSON 하나만 출력하라: {"bottleneck": "...", "advice": "..."}',
        card_advice.ADVICE_LANG_INSTRUCTION.get(lang, card_advice.ADVICE_LANG_INSTRUCTION["en"]),
    ]
    return "\n".join(lines)


def parse_revision(raw: str) -> tuple[str, str] | None:
    """The two sentences out of one completion, or None when it is not that."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    bottleneck, advice = data.get("bottleneck"), data.get("advice")
    if not isinstance(bottleneck, str) or not isinstance(advice, str):
        return None
    bottleneck, advice = " ".join(bottleneck.split()), " ".join(advice.split())
    if len(bottleneck) < MIN_BOTTLENECK_CHARS or len(advice) < MIN_ADVICE_CHARS:
        return None
    return bottleneck, advice


def _facts_to_keep(original: str) -> set[str]:
    """polish.facts of the original with the inside talk taken out first: a wiki number, a path
    or a hash is what the line removes on purpose, and the digits inside it go with it — that
    is the rewrite doing its job, not losing a fact."""
    plain = original
    for token in internal_names(original):
        plain = plain.replace(token, " ")
    return polish.facts(plain)


def revise(proposal: Proposal, invoke: Callable[[str], str], lang: str) -> Revised:
    """Rewrite the proposal's two sentences once (twice when the checker still flags the first
    rewrite). The original stays when a rewrite loses a number, cannot be parsed, or is still
    flagged after the second pass — the model proposes, this function decides."""
    original = f"{proposal.bottleneck}\n{proposal.advice}"
    keep = _facts_to_keep(original)
    bottleneck, advice = proposal.bottleneck, proposal.advice
    flagged: list[str] = []
    for _ in range(REWRITE_PASSES):
        parsed = parse_revision(invoke(build_revise_prompt(bottleneck, advice, lang, flagged)))
        if parsed is None:
            return Revised(proposal, "unusable")
        bottleneck, advice = parsed
        if keep - polish.facts(f"{bottleneck}\n{advice}"):
            return Revised(proposal, "lost_facts")
        flagged = internal_names(f"{bottleneck} {advice}")
        if not flagged:
            return Revised(
                proposal.model_copy(update={"bottleneck": bottleneck, "advice": advice}), "rewritten"
            )
    return Revised(proposal, "kept_original")


def event_fields(proposal: Proposal, *, day: str, run: str, rank: int, revise: Revise) -> dict[str, Any]:
    return {
        "prompt_version": card_types.ADVICE_WRITTEN_PROMPT_VERSION,
        "day": day,
        "run": run,
        "rank": rank,
        "revise": revise,
        "register": proposal.register_,
        "project": proposal.project,
        "subject": proposal.subject,
        "note": proposal.note,
        "bottleneck": proposal.bottleneck,
        "advice": proposal.advice,
        "evidence": [e.model_dump() for e in proposal.evidence],
    }


_PROPOSAL_KEYS = ("register", "project", "subject", "note", "bottleneck", "advice", "evidence")


def written_proposals(entries: list[dict[str, Any]], day: str) -> list[Proposal]:
    """The card's whole read of the stored advice: `day`'s rows of this prompt version, the
    newest run's only, in rank order. No rows is an empty section; a row that does not rebuild
    is malformed and raises — a card must not quietly drop advice it was handed."""
    rows = []
    for entry in entries:
        attrs = entry.get("attributes") or {}
        if (
            attrs.get("prompt_version") == card_types.ADVICE_WRITTEN_PROMPT_VERSION
            and attrs.get("day") == day
        ):
            rows.append(attrs)
    if not rows:
        return []
    try:
        newest = max(str(row["run"]) for row in rows)
        picked = sorted((row for row in rows if str(row["run"]) == newest), key=lambda row: row["rank"])
        return [Proposal.model_validate({key: row[key] for key in _PROPOSAL_KEYS}) for row in picked]
    except (KeyError, TypeError, ValidationError) as e:
        raise ValueError(f"malformed {EVENT} row: {e}") from e


def main() -> int:
    if not os.environ.get("BORING_DOOR_URL"):
        print("[advice-write] BORING_DOOR_URL must be set — 조언은 문을 통해 읽고 쓴다", file=sys.stderr)
        return 2
    import card

    try:
        state = card.build_advice_graph().invoke({})
    except (ValueError, OSError) as e:
        print(f"[advice-write] 조언 거부: {' '.join(str(e).split())}", file=sys.stderr)
        return 3
    tags = Counter(state["advice_revisions"])
    summary = " ".join(f"{tag}={n}" for tag, n in sorted(tags.items()))
    print(f"[advice-write] written={len(state['advice_revisions'])} {summary}".rstrip(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
