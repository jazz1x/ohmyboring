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
from typing import Callable, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import recall_core  # noqa: E402
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


def strip_mention(text: str) -> str:
    """`<@U0123> 크론 잡 어디부터 봤더라` → `크론 잡 어디부터 봤더라`."""
    out = []
    for token in (text or "").split():
        if token.startswith("<@") and token.endswith(">"):
            continue
        out.append(token)
    return " ".join(out).strip()


def render(hits: list[dict]) -> str:
    """The same lines the prompt hook injects, minus the fence — a person is reading."""
    lines = []
    for hit in hits[:MAX_HITS]:
        snip = recall_core.salient(hit.get("snippet"))
        if not snip:
            continue
        lines.append(f"• *{recall_core.source_name(hit)}*{recall_core.consumption_note(hit)} {snip}")
        for line in recall_core.claim_lines(hit):
            lines.append("    " + line.strip())
    return "\n".join(lines)


def answer(question: str, search: Optional[Callable[..., list[dict]]] = None) -> str:
    """One question in, one message out. `search` is injectable so the brain is testable
    without an engine; the default is the live client."""
    q = strip_mention(question)
    if len(q) < 4:
        return "무엇을 찾을까요? 한 문장으로 물어봐 주세요."
    if search is None:
        client = DrudgeClient(timeout=TIMEOUT, retries=0)
        search = client.search
    try:
        hits = search(q, max_results=MAX_HITS, claims=CLAIMS_PER_HIT)
    except Exception as e:  # noqa: BLE001 — the reader must see "could not look", not a stack trace
        print(f"[secretary] search failed: {e}", file=sys.stderr)
        return ENGINE_DOWN
    body = render(hits or [])
    if not body:
        return NOTHING_FOUND
    return body + "\n\n_더 보려면 `recall`, 정한 것만 보려면 `claims` 를 터미널에서._"
