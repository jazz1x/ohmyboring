#!/usr/bin/env python3
"""The secretary's brain, with no Slack in it.

A message comes in, an answer goes out. Everything in between is the engine — the same
`/search` the prompt hook uses, rendered the same way, so the owner reads the same material in
Slack that the agent reads in the terminal. There is no second brain here: the note snippets and
the claims behind them are the answer, not a summary of them.

Why the transport is not in this file: hermes was the secretary's face for three months, and the
face is the part that broke — a socket reconnect loop the engine never saw. Keeping the brain
free of Slack means it is tested without Slack, and the face can be swapped without touching
what it says.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NamedTuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import recall_core  # noqa: E402
from drudge_client import DrudgeClient  # noqa: E402

#: How many notes an answer carries. Three is what the prompt hook injects; a person reading in
#: Slack has less patience than an agent, not more.
MAX_HITS = int(os.environ.get("SECRETARY_MAX_HITS") or "3")
#: Claims per note, same knob as the hook.
CLAIMS_PER_HIT = int(os.environ.get("SECRETARY_CLAIMS_PER_HIT") or str(recall_core.CLAIMS_PER_HIT))
TIMEOUT = float(os.environ.get("SECRETARY_TIMEOUT") or "8")

#: The first line of every answer. The transport reads a thread parent back through the Slack API
#: to recognize its own answers without keeping any state, and this header — not the notes below
#: it, which move as the engine re-ranks — is what it matches on. Renaming this is a transport
#: change, not a wording change.
ANSWER_HEAD = "_기억에서 찾은 것 {n}개_"
ANSWER_HEAD_PREFIX = "_기억에서 찾은 것"
#: Circled numbers for the notes of one answer, so a thread reply can say "정정 2:" and mean the
#: second note on the list the reader saw, not the second note of the vault.
NUMERALS = "①②③④⑤"

#: What the secretary says when the engine has nothing, and when the engine is unreachable. The
#: two are different facts and the reader gets to tell them apart — "I found nothing" is an
#: answer, "I could not look" is not.
NOTHING_FOUND = "기억에 이 주제로 남은 게 없어요."
ENGINE_DOWN = "지금 기억을 못 열어요 — 엔진이 응답하지 않아요. (`make doctor`)"


class Answer(NamedTuple):
    """The message and the notes it carried. `hits` is exactly what reached the reader — not
    everything the search returned — so a 👍/👎 later lands on the notes the reader actually saw."""

    text: str
    hits: list[dict]


def strip_mention(text: str) -> str:
    """`<@U0123> 크론 잡 어디부터 봤더라` → `크론 잡 어디부터 봤더라`."""
    out = []
    for token in (text or "").split():
        if token.startswith("<@") and token.endswith(">"):
            continue
        out.append(token)
    return " ".join(out).strip()


def handed(hits: list[dict]) -> list[dict]:
    """The hits that actually reach the reader: the first MAX_HITS, minus any whose snippet
    renders to nothing. `render` and the ledger both go through this list, so what was shown
    and what feedback later judges are the same notes."""
    out = []
    for hit in (hits or [])[:MAX_HITS]:
        if recall_core.salient(hit.get("snippet")):
            out.append(hit)
    return out


def render(hits: list[dict]) -> str:
    """The same lines the prompt hook injects, minus the fence — a person is reading. The first
    line names the count and each note carries its circled number, so a later "정정 2:" in the
    thread points at exactly the note the reader saw second."""
    given = handed(hits)
    lines = [ANSWER_HEAD.format(n=len(given))]
    for i, hit in enumerate(given):
        snip = recall_core.salient(hit.get("snippet"))
        numeral = NUMERALS[i] if i < len(NUMERALS) else "·"
        lines.append(f"{numeral} *{recall_core.source_name(hit)}*{recall_core.consumption_note(hit)} {snip}")
        for line in recall_core.claim_lines(hit):
            lines.append("    " + line.strip())
    return "\n".join(lines)


def answer(question: str, search: Callable[..., list[dict]] | None = None) -> Answer:
    """One question in, one message out. `search` is injectable so the brain is testable
    without an engine; the default is the live client. `hits` is what the message carried,
    so the transport can ledger the answer and later attach a verdict — or a correction — to it."""
    q = strip_mention(question)
    if len(q) < 4:
        return Answer("무엇을 찾을까요? 한 문장으로 물어봐 주세요.", [])
    if search is None:
        client = DrudgeClient(timeout=TIMEOUT, retries=0)
        search = client.search
    try:
        hits = search(q, max_results=MAX_HITS, claims=CLAIMS_PER_HIT)
    except Exception as e:  # noqa: BLE001 — the reader must see "could not look", not a stack trace
        print(f"[secretary] search failed: {e}", file=sys.stderr)
        return Answer(ENGINE_DOWN, [])
    given = handed(hits or [])
    if not given:
        return Answer(NOTHING_FOUND, [])
    return Answer(render(given) + "\n\n_더 보려면 `recall`, 정한 것만 보려면 `claims` 를 터미널에서._", given)


_CORRECTION_RE = re.compile(r"^\s*정정(?:\s+(\d+))?\s*[:：]\s*(.*?)\s*$")


def parse_correction(text: str) -> tuple[int | None, str] | None:
    """`정정: X` → `(None, "X")` — the whole answer is wrong and X replaces all of it.
    `정정 2: X` → `(2, "X")` — only the numbered note is wrong. Anything else → None,
    an empty correction body included: not every sentence about "정정" is a correction."""
    m = _CORRECTION_RE.match(text or "")
    if not m:
        return None
    body = (m.group(2) or "").strip()
    if not body:
        return None
    return (int(m.group(1)) if m.group(1) else None), body


def _first_sentence(text: str, limit: int = 60) -> str:
    """The note's title: the first sentence, capped. No sentence end on the line → the line
    itself; past the cap → the cap — a title is never empty and never a wall of text."""
    head = text.strip().split("\n", 1)[0]
    cut = len(head)
    for sep in (".", "!", "?"):
        i = head.find(sep)
        if i != -1:
            cut = min(cut, i + 1)
    return head[:cut][:limit]


def correct(
    answer_key: str,
    question: str,
    handed_paths: list[str],
    text: str,
    remember: Callable[..., dict] | None = None,
) -> dict:
    """A thread reply of "정정: …" turned into a new note that replaces the answer's notes. With a
    number ("정정 2:") only that one note is superseded — 1-based, in the order the answer listed
    them; a number outside the answer is refused, not guessed. Without a number, the answer's one
    note is superseded; if it carried several, the owner is asked for a number instead.
    The owner's sentence is the note: no LLM is asked to reword it.
    The engine's response travels back untouched."""
    parsed = parse_correction(text)
    if parsed is None:
        return {"error": "not a correction"}
    number, body = parsed
    if number is None and len(handed_paths) > 1:
        return {"error": "number required", "count": len(handed_paths)}
    if number is not None and not 1 <= number <= len(handed_paths):
        return {"error": "no such number"}
    supersedes = [handed_paths[number - 1]] if number is not None else list(handed_paths)
    if remember is None:
        remember = DrudgeClient(timeout=TIMEOUT, retries=0).remember
    note = f"질문: {question}\n\n정정: {body}"
    try:
        return remember(_first_sentence(body), note, tags=["correction", "slack"], supersedes=supersedes)
    except Exception as e:  # noqa: BLE001 — a failed correction must not cost the transport a crash
        print(f"[secretary] correction on {answer_key} failed: {e}", file=sys.stderr)
        return {"error": str(e)}


def remember_handed(
    answer_key: str,
    question: str,
    hits: list[dict],
    handover: Callable[..., dict] | None = None,
) -> bool:
    """Hand the engine the list of note paths one answer carried, under the answer's own
    session name, so a later 👍/👎 only has to send the verdict. `question` stays in the
    signature to keep the transport's call shape; it is not used here. Empty hands mean
    nothing was handed over, so nothing is sent."""
    if not hits:
        return False
    if handover is None:
        handover = DrudgeClient(timeout=TIMEOUT, retries=0).handover
    paths = [hit["source_path"] for hit in hits]
    try:
        handover(answer_key, datetime.now(UTC).isoformat(), paths)
    except Exception as e:  # noqa: BLE001 — an unrecorded answer must not cost the transport a crash
        print(f"[secretary] handover failed: {e}", file=sys.stderr)
        return False
    return True


def feedback(
    answer_key: str,
    verdict: str,
    consumption: Callable[..., dict] | None = None,
    observed_at: str | None = None,
) -> dict:
    """A 👍/👎 on an answer, turned into the engine's verdict. The engine already recorded
    what the answer carried when it was handed over, so only the verdict travels now; a
    verdict on an answer the engine never heard of comes back with zero edges, which the
    transport reads as an unknown answer."""
    if verdict not in ("used", "contested"):
        raise ValueError(f"verdict must be 'used' or 'contested', got {verdict!r}")
    if consumption is None:
        consumption = DrudgeClient(timeout=TIMEOUT, retries=0).consumption
    if observed_at is None:
        observed_at = datetime.now(UTC).isoformat()
    try:
        resp = consumption(answer_key, observed_at, verdict=verdict)
    except Exception as e:  # noqa: BLE001 — a reaction must not cost the transport a crash
        print(f"[secretary] feedback failed: {e}", file=sys.stderr)
        return {"error": str(e)}
    if resp.get("used", 0) + resp.get("contested", 0) == 0:
        return {"unknown_answer": True, **resp}
    return resp
