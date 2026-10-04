#!/usr/bin/env python3
"""「맡길게요」의 판정 자리 — 버튼 누름 한 번에 모델이 노트 본문·근거 문장·제안 종류를
읽고 맞다/틀리다와 이유 한 줄을 내는 일을 준비한다.

build_judge_prompt shapes the one question a delegate press asks; parse_judgment is the
boundary that turns the completion into a Delegated value — never an exception, since a
model that cannot answer is a fact about the press, not a crash: 조용히 낱말 표지로
되돌아가는 변이(제안 표지를 그대로 도장 찍는 변이)를 막는 문지방이 parse 가 혼자 선다.
The two live reads (the vault note's own text, the proposal's 근거 문장 from the engine's
verdict_proposed 사건) live here too — card_live has the same reads, but card_live pulls
langchain through the advice seam's retriever and this module must stay importable inside
the hermes venv, so the hermes-safe copies live here rather than importing there.

The model call itself lives in the hermes plugin (agents/hermes/plugins/boring-card) —
hermes owns the host LLM facade (ctx.llm). 한 누름의 모델 호출 상한 1은 구조로 지켜진다:
이 모듈은 순수하고 효과도 순수한 결정표(card_press.effects)라, 호출은 플러그인의 판정
자리 딱 한 곳에서 한 번만 일어날 수 있다.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "src"))

import vault_note  # noqa: E402
from card_types import Delegated, DelegatedJudgment, DelegationFailed  # noqa: E402

from ohmyboring import config as omb_env  # noqa: E402

#: The note body that enters the prompt — a small local model's context outgrows a whole
#: note fast, and the judgment only needs the head of the claim's own text.
NOTE_TEXT_PROMPT_MAX = 2000

#: The delegated 이유 한 줄의 상한 — uptake_core.REASON_SENTENCE_MAX 와 같은 400. 사건 한 줄이
#: 카드 줄이 되는 길이라 근거 문장과 같은 캡을 쓴다.
DELEGATE_REASON_MAX = 400

#: How far back the proposal's 근거 사건 is read: the review lane's window (REVIEW_SINCE_HOURS
#: 24h) plus a card's pressable age (CARD_ANSWERABLE_HOURS 23h) — a proposal the press can
#: still answer is at most this old.
PROPOSAL_WINDOW_HOURS = float(os.environ.get("CARD_PROPOSAL_WINDOW_HOURS") or "48")

_EVENTS_TIMEOUT = float(os.environ.get("CARD_ENGINE_TIMEOUT") or "30")

_EVENTS_LIMIT = 1000

_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)

#: The claim each proposed kind makes — the question the model answers right/wrong about.
_KIND_CLAIM = {
    "used": "세션이 '이 노트를 작업에 썼다'고 주장했다",
    "contested": "세션이 '이 노트가 틀렸다'고 주장했다",
}


def build_judge_prompt(note_path: str, note_text: str, proposed_kind: str, evidence: str) -> str:
    """One delegate press's whole question: the claim, the sentence the session-end scorer
    caught it in, and the note's own text. The prompt is Korean, the same language
    card_advice builds its prompt in — the local model answers right/wrong about the claim,
    never picking an engine kind itself (that fold is parse_judgment's job)."""
    split = vault_note.split_frontmatter(note_text)
    body = (split[1] if split is not None else note_text).strip()
    if len(body) > NOTE_TEXT_PROMPT_MAX:
        body = f"{body[: NOTE_TEXT_PROMPT_MAX - 1]}… (앞부분만)"
    evidence_line = evidence.strip() if evidence.strip() else "(채점이 남긴 문장 없음)"
    return (
        f"검토할 주장: {_KIND_CLAIM[proposed_kind]}. 이 주장이 아래 근거 문장과 노트 본문에 맞는지 판정한다.\n\n"
        f"근거 문장(세션에서 실제로 나온 문장):\n{evidence_line}\n\n"
        f"노트 경로: {note_path}\n{body}\n\n"
        "주장이 맞으면 아래 JSON 하나만 출력해라:\n"
        '{"verdict": "right", "reason": "왜 맞는지 한 문장"}\n\n'
        "주장이 틀리면:\n"
        '{"verdict": "wrong", "reason": "왜 틀리는지 한 문장"}\n\n'
        "규칙:\n"
        "- 새로운 사실을 지어내지 마라. reason 은 노트 본문과 근거 문장에 근거해 써라.\n"
        f"- reason 은 한 문장, {DELEGATE_REASON_MAX}자 이내로 써라.\n"
        '- verdict 는 "right" 아니면 "wrong" 만 써라.\n'
    )


def parse_judgment(raw: str, proposed_kind: str) -> Delegated:
    """One model completion in, the delegated value out — never an exception. JSON이 아니거나
    verdict 어휘가 아니거나 이유가 없는 답은 전부 DelegationFailed: 모델이 못 답한 사실을
    사건으로 남길 뿐 판정을 찍지 않는다(조용히 낱말 표지로 되돌아가는 변이의 문지방)."""
    text = (raw or "").strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return DelegationFailed(reason="모델 답이 JSON 이 아니다")
    if not isinstance(data, dict):
        return DelegationFailed(reason="모델 답이 JSON 객체가 아니다")
    verdict = data.get("verdict")
    if verdict not in ("right", "wrong"):
        return DelegationFailed(reason=f"모델 판정을 알아듣지 못했다: {verdict!r}")
    reason = " ".join(str(data.get("reason") or "").split())
    if not reason:
        return DelegationFailed(reason="모델이 이유를 남기지 않았다")
    if len(reason) > DELEGATE_REASON_MAX:
        reason = f"{reason[: DELEGATE_REASON_MAX - 1]}…"
    # 맞다 = 제안 그대로, 틀리다 = 뒤집은 판정 — used↔contested 접기는 이 경계 한 곳.
    kind = proposed_kind if verdict == "right" else ("contested" if proposed_kind == "used" else "used")
    return DelegatedJudgment(kind=kind, reason=reason)


#: The vault path: the boring-agent container mounts it read-only at /vault (boring-memory's
#: own convention — agents/hermes/plugins/boring-memory/__init__.py), and BORING_VAULT_DIR
#: overrides it for host runs and tests, the same escape hatch card_live uses. This module's
#: only production caller is the hermes plugin, so the container default comes first.
_VAULT_ENV = "BORING_VAULT_DIR"
_DEFAULT_VAULT_DIR = "/vault"


def _vault_dir() -> str:
    return os.environ.get(_VAULT_ENV) or _DEFAULT_VAULT_DIR


def read_note_text(note_path: str) -> str | None:
    """The note's own text from the vault — card_live._live_read_note 의 hermes-venv 길.
    `note_path` looks like `/vault/wiki/wiki-NNNN.md`; missing on disk is a value (the
    press cannot be judged without the note), never an exception here."""
    relative = note_path.removeprefix("/vault/") if note_path.startswith("/vault/") else note_path.lstrip("/")
    path = os.path.join(_vault_dir(), relative)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _live_events(event_name: str, since_hours: float) -> list[dict]:
    """GET /events one name at a time — the same read card_live makes, through the door
    like every other consumer (E4-α; /events is in the door's proxy table now). A dead
    engine/door or a bad body is an empty list here: the caller turns the missing
    사건 into a DelegationFailed, so the press records the fact instead of crashing."""
    url = (
        f"{omb_env.door_url()}/events?event={urllib.parse.quote(event_name)}"
        f"&since_hours={since_hours}&limit={_EVENTS_LIMIT}"
    )
    try:
        with urllib.request.urlopen(url, timeout=_EVENTS_TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    return entries if isinstance(entries, list) else []


def proposal_evidence(sessions: list[str], note_path: str, kind: str) -> str | None:
    """The proposal's 근거 문장 — the `reason` the session-end scorer left on the
    verdict_proposed 사건 for one of this press's sessions, note, and kind. A grouped press
    carries every grouped session and any of their sentences is the group's evidence, so
    the newest row with a sentence wins; a row that exists with no sentence (옛 판정) is
    "" — the prompt then says honestly that no sentence was stored. None only when no
    matching row is there at all (aged out, or the 사건 read died): without the proposal
    the press cannot be judged."""
    wanted = set(sessions)
    rows: list[tuple[str, str]] = []
    for entry in _live_events("verdict_proposed", PROPOSAL_WINDOW_HOURS):
        if not isinstance(entry, dict):
            continue
        attrs = entry.get("attributes")
        if not isinstance(attrs, dict):
            continue
        observed_at = entry.get("observed_at")
        if attrs.get("note") != note_path or attrs.get("kind") != kind:
            continue
        if attrs.get("session_id") not in wanted or not isinstance(observed_at, str):
            continue
        reason = attrs.get("reason")
        rows.append((observed_at, reason if isinstance(reason, str) else ""))
    if not rows:
        return None
    rows.sort(key=lambda row: row[0], reverse=True)
    return next((reason for _, reason in rows if reason), "")
