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
import sys
from datetime import datetime, timezone
from typing import Callable, NamedTuple, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import recall_core  # noqa: E402
import uptake_core  # noqa: E402
from drudge_client import DrudgeClient  # noqa: E402

#: How many notes an answer carries. Three is what the prompt hook injects; a person reading in
#: Slack has less patience than an agent, not more.
MAX_HITS = int(os.environ.get("SECRETARY_MAX_HITS") or "3")
#: Claims per note, same knob as the hook.
CLAIMS_PER_HIT = int(os.environ.get("SECRETARY_CLAIMS_PER_HIT") or str(recall_core.CLAIMS_PER_HIT))
TIMEOUT = float(os.environ.get("SECRETARY_TIMEOUT") or "8")

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
    """The same lines the prompt hook injects, minus the fence — a person is reading."""
    lines = []
    for hit in handed(hits):
        snip = recall_core.salient(hit.get("snippet"))
        lines.append(f"• *{recall_core.source_name(hit)}*{recall_core.consumption_note(hit)} {snip}")
        for line in recall_core.claim_lines(hit):
            lines.append("    " + line.strip())
    return "\n".join(lines)


def answer(question: str, search: Optional[Callable[..., list[dict]]] = None) -> Answer:
    """One question in, one message out. `search` is injectable so the brain is testable
    without an engine; the default is the live client. `hits` is what the message carried,
    so the transport can ledger the answer and later attach a verdict to it."""
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
    body = render(hits or [])
    if not body:
        return Answer(NOTHING_FOUND, [])
    return Answer(body + "\n\n_더 보려면 `recall`, 정한 것만 보려면 `claims` 를 터미널에서._", given)


def remember_handed(answer_key: str, question: str, hits: list[dict]) -> bool:
    """Ledger one answer as its own session, so a later 👍/👎 can find the notes it carried.
    `answer_key` is the transport's opaque name for the answer (e.g. a Slack message ref);
    empty hands mean nothing was handed over, so nothing is written."""
    if not hits:
        return False
    record = uptake_core.injection_record(answer_key, question, hits, len(hits))
    return uptake_core.append_record(record)


def feedback(
    answer_key: str,
    verdict: str,
    consumption: Optional[Callable[..., dict]] = None,
    observed_at: Optional[str] = None,
) -> dict:
    """A 👍/👎 on an answer, turned into engine edges. Terminal sessions get their edges
    inferred from the transcript at SessionEnd; a Slack answer has no transcript, so the
    owner's reaction is the explicit verdict instead. The note paths the answer carried
    become `used` or `contested` on the graph, where the next answer's `(reused N×)` count
    reads them."""
    if verdict not in ("used", "contested"):
        raise ValueError(f"verdict must be 'used' or 'contested', got {verdict!r}")
    records = uptake_core.load_records(answer_key)
    if not records:
        return {"unknown_answer": True}
    paths = [
        uptake_core.note_path(hit)
        for record in records
        for hit in record.get("hits") or []
    ]
    if consumption is None:
        consumption = DrudgeClient(timeout=TIMEOUT, retries=0).consumption
    if observed_at is None:
        observed_at = datetime.now(timezone.utc).isoformat()
    try:
        return consumption(
            answer_key,
            observed_at,
            used=paths if verdict == "used" else [],
            contested=paths if verdict == "contested" else [],
        )
    except Exception as e:  # noqa: BLE001 — a reaction must not cost the transport a crash
        print(f"[secretary] feedback failed: {e}", file=sys.stderr)
        return {"error": str(e)}
