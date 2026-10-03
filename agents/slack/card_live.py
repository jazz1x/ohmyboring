#!/usr/bin/env python3
"""The morning card's live collaborators — every read-side seam that reaches the door, the
engine, or the host vault. Depends only on card_types (plus stdlib and the shared engine
clients) — never on card_registers/card_advice/card_verdicts/card_view/card — so card.py
can import this module for its default Collaborators without a cycle back.

The write-side effects (record/consumption/execute_repair) and the fold that applies a
card_press.effects list live in card_effects — a module that must stay importable inside
the hermes venv, which has no langchain. This module imports them back so existing
references (card.py's Collaborators, tests) keep working; `_door_url` and ENGINE_TIMEOUT
moved there with them.

`_live_past_verdicts`'s proposal window is card_press.CARD_ANSWERABLE_HOURS wider than its
verdict window; the hermes plugin refuses presses older than that, which is what keeps the
join whole."""

from __future__ import annotations

import glob
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from card_effects import (  # noqa: F401
    ENGINE_TIMEOUT,
    _door_url,
    _live_consumption,
    _live_execute_repair,
    _live_record,
)
from card_press import CARD_ANSWERABLE_HOURS
from card_types import (
    NO_CURRENT_CLAIM,
    REPAIR_JUDGE_VERDICTS,
    PastApproved,
    PastCardHistory,
    PastUnansweredPair,
    PastVerdictPair,
    ProposedVerdict,
    RepairJudgment,
    ResolvedNote,
    Unresolved,
)
from pydantic import ValidationError

from ohmyboring import config as omb_env
from ohmyboring.adapters.engine import DrudgeClient
from ohmyboring.result import Err, Ok

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "memory"))
from retriever import BoringRetriever  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import vault_note  # noqa: E402


def _live_fetch(path: str, project: str) -> dict[str, Any]:
    # project is always sent, even "" — the engine's own filter treats an explicit empty
    # string as "unassigned documents only", not "no filter" (measured 2026-09-22).
    # Err→예외는 카드 그래프가 예외를 계약으로 삼는 동안의 임시 경계(card.py 의 except 가 주인).
    match DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).request("POST", path, {"project": project}):
        case Ok(payload):
            return payload
        case Err(failure):
            raise OSError(str(failure))


def _door_json(url: str) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=ENGINE_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except OSError as e:
        raise OSError(f"{url}: {e}") from e


def _live_active_projects(active_days: int) -> list[str]:
    """The door's own GET /projects?active_days=N — DB-backed, unlike the engine's plain
    /projects (no activity filter), so this asks the door, not DrudgeClient's engine URL."""
    url = f"{_door_url()}/projects?active_days={active_days}"
    payload = _door_json(url)
    return [str(item["project"]) for item in payload["projects"]]


def _live_events(event_name: str, since_hours: int) -> list[dict[str, Any]]:
    """GET /events, one event name at a time. E4-α: /events 는 이제 문의 프록시 표에 있고
    (계약 스냅샷 재생성 — 옛 측정(2026-09-22)이 404 로 찍었던 그 경로다), 문은 표에 있는
    경로를 엔진에 바이트 그대로 넘기니 여기서도 omb_env.door_url() 하나로 읽는다. 문이
    없던 경로를 엔진에 붙이던 임시 직행은 사라진다. maybe_truncated is not a value to shrug
    at here: a clipped 7-day window would silently under-suppress (a do/drop that should
    have hidden a repeat candidate falls outside the page handed back), so it is raised,
    the same way an unreadable window is raised anywhere else in this file."""
    url = f"{omb_env.door_url()}/events?event={urllib.parse.quote(event_name)}&since_hours={since_hours}&limit=1000"
    payload = _door_json(url)
    if payload.get("maybe_truncated"):
        raise OSError(
            f"/events?event={event_name}&since_hours={since_hours} maybe_truncated=true — "
            "a clipped judged-history window cannot ground 판정 suppression"
        )
    return payload["entries"]


def _live_past_verdicts(since_hours: int) -> PastCardHistory:
    """Join card_proposal and card_verdict events by (card_ts, idx) — a card_verdict event
    alone carries no note or evidence, only the button press (knowns: card_ts·idx·choice).
    Proposals are read over a wider window than verdicts: a press can land up to
    CARD_ANSWERABLE_HOURS after its card posted, so a verdict at hour 167 of the 168h window
    would otherwise be joined against a proposal that already fell outside it. The same
    join also yields the shown-but-unanswered pairs: a card_proposal with no card_verdict
    in the window is a proposal the owner saw and never judged, returned with the
    proposal event's own observed_at so suppressed can rest it REST_HOURS. A card_verdict
    with no matching card_proposal, or a matching one missing note/evidence/choice/
    timestamp, is a malformed row — F5/ROP: that is a visible failure (ValueError naming
    the row), never a silently skipped one, because a dropped row here is exactly a
    suppression pair going missing without anyone knowing."""
    proposal_window = since_hours + math.ceil(CARD_ANSWERABLE_HOURS)
    proposals_by_key: dict[tuple[Any, Any], dict[str, Any]] = {}
    for entry in _live_events("card_proposal", proposal_window):
        attrs = entry.get("attributes") or {}
        key = (attrs.get("card_ts"), attrs.get("idx"))
        if key[0] is not None and key[1] is not None:
            proposals_by_key[key] = entry
    judged: list[PastVerdictPair] = []
    answered: set[tuple[Any, Any]] = set()
    for entry in _live_events("card_verdict", since_hours):
        attrs = entry.get("attributes") or {}
        key = (attrs.get("card_ts"), attrs.get("idx"))
        answered.add(key)
        proposal_entry = proposals_by_key.get(key)
        if proposal_entry is None:
            raise ValueError(f"card_verdict {key!r} has no matching card_proposal event: {attrs!r}")
        proposal = proposal_entry.get("attributes") or {}
        evidence = proposal.get("evidence") or []
        if not evidence:
            raise ValueError(f"card_proposal {key!r} was recorded with no evidence: {proposal!r}")
        first = evidence[0]
        try:
            judged.append(
                PastVerdictPair(
                    note=proposal["note"],
                    evidence_note=first["note"],
                    evidence_line=int(first["line"]),
                    choice=attrs["choice"],
                    at=entry["observed_at"],
                )
            )
        except (KeyError, TypeError, ValueError, ValidationError) as e:
            raise ValueError(f"malformed card_verdict/card_proposal pair {key!r}: {e}") from e
    unanswered: list[PastUnansweredPair] = []
    for key, proposal_entry in proposals_by_key.items():
        if key in answered:
            continue
        proposal = proposal_entry.get("attributes") or {}
        evidence = proposal.get("evidence") or []
        if not evidence:
            raise ValueError(f"card_proposal {key!r} was recorded with no evidence: {proposal!r}")
        first = evidence[0]
        try:
            unanswered.append(
                PastUnansweredPair(
                    note=proposal["note"],
                    evidence_note=first["note"],
                    evidence_line=int(first["line"]),
                    at=proposal_entry["observed_at"],
                )
            )
        except (KeyError, TypeError, ValueError, ValidationError) as e:
            raise ValueError(f"malformed card_proposal {key!r}: {e}") from e
    return PastCardHistory(judged=judged, unanswered=unanswered)


def _live_handover(session: str, at: str, paths: list[str]) -> dict:
    # Err→예외는 카드 그래프가 예외를 계약으로 삼는 동안의 임시 경계 — 실패한 카드는 게시되지 않는다.
    match DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).handover(session, at, paths):
        case Ok(resp):
            return resp
        case Err(failure):
            raise OSError(str(failure))


def _live_resolve(subject: str, register: str) -> ResolvedNote | Unresolved:
    """subject → current claim's note path through the door. A recurrence source is already
    a note path — it resolves to itself without a door round-trip. Everything else asks
    /claim-source; every failure mode is an Unresolved value: the resolve node drops that
    proposal and the card ships with whatever is left, even zero."""
    if subject.startswith("/"):
        return ResolvedNote(subject=subject, note=subject)
    url = f"{_door_url()}/claim-source?subject={urllib.parse.quote(subject)}"
    try:
        with urllib.request.urlopen(url, timeout=ENGINE_TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            reason = NO_CURRENT_CLAIM
        else:
            reason = f"claim-source answered {e.code}"
        return Unresolved(subject=subject, register=register, reason=reason)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return Unresolved(subject=subject, register=register, reason=f"claim-source unreachable: {e}")
    return ResolvedNote(subject=subject, note=payload["note"])


def _live_approved(since_hours: int) -> list[PastApproved]:
    url = f"{_door_url()}/approved?since_hours={since_hours}"
    payload = _door_json(url)
    return [
        PastApproved(session=item["session"], note=item["note"], at=item["at"])
        for item in payload["approved"]
    ]


#: how many split-subject groups the "오늘 할 일" lane shows — the door's own default too.
REPAIRS_LIMIT = 3

#: The repair lane's scan width — 문 한 번에 주는 상한(문 _MAX_REPAIR_LIMIT)만큼 받아 판정으로
#: 거른 뒤 REPAIRS_LIMIT 개를 앉힌다. 거른 뒤 3개가 안 차도 실패가 아니다.
REPAIRS_SCAN_LIMIT = 50

#: The repair-judgment window, hours — 한 달, 판정 세션이 정한 창. card_repair_judge 의
#: JUDGED_WINDOW_HOURS 와 같아야 한다: 판정을 쓰는 쪽(카드)과 판정을 걸러 넘기는 쪽(판정
#: 실행)이 같은 창을 봐야, 낡은 판정을 다시 내거나 두 번 쓰는 일이 없다.
REPAIR_JUDGED_WINDOW_HOURS = 24 * 30


def _live_repair_judgments() -> dict[tuple[str, tuple[str, ...]], RepairJudgment]:
    """repair_judged 사건의 최신 판정 하나씩 — (subject, 정렬된 variants) 가리키는 줄.
    같은 묶음의 판정이 창 안에 여러 개면 observed_at 최신 것 하나만 산다. 모양이 틀린 행은
    ValueError(F5/ROP), 조용히 걸러내지 않는다: 빠진 판정 하나가 generic 묶음이 오너 카드에
    오르는 문지방이다."""
    out: dict[tuple[str, tuple[str, ...]], RepairJudgment] = {}
    newest: dict[tuple[str, tuple[str, ...]], str] = {}
    for entry in _live_events("repair_judged", REPAIR_JUDGED_WINDOW_HOURS):
        attrs = entry.get("attributes") or {}
        subject = attrs.get("subject")
        variants = attrs.get("variants")
        verdict = attrs.get("verdict")
        reason = attrs.get("reason")
        observed_at = entry.get("observed_at")
        if (
            not isinstance(subject, str)
            or not subject
            or not isinstance(variants, list)
            or not variants
            or not all(isinstance(v, str) for v in variants)
            or verdict not in REPAIR_JUDGE_VERDICTS
            or not isinstance(reason, str)
            or not reason
            or not isinstance(observed_at, str)
        ):
            raise ValueError(f"malformed repair_judged row: {attrs!r}")
        key = (subject, tuple(sorted(variants)))
        if key not in newest or observed_at >= newest[key]:
            newest[key] = observed_at
            out[key] = RepairJudgment(
                subject=subject, variants=sorted(variants), verdict=verdict, reason=reason
            )
    return out


#: how many of the agent's session-end classifications the review lane shows.
REVIEW_LIMIT = 3

#: The held-pair (보류) window, hours — 이레, the same window as the advice lane's
#: SUPPRESS_WINDOW_HOURS (card_verdicts): a pair the owner held comes back only after the
#: window lets it go. card_live never imports card_verdicts, so the number lives here with
#: that pointer rather than a cross-import.
REVIEW_DEFER_WINDOW_HOURS = 168


def _deferred_review_pairs() -> set[tuple[str, str]]:
    """(note, kind) pairs the owner held (보류) within the 이레 window — read from the
    verdict_reviewed 사건 a hold leaves. Agree/flip/delegate rows are ignored here: only a
    hold suppresses (the delegate rows a successful judgment leaves are read by
    _delegated_reasons, which runs before this filter). A hold row missing the note or its
    kind is malformed, same as a malformed proposal: raise (F5/ROP), never skip — a dropped
    row here is exactly a held pair coming back unannounced."""
    out: set[tuple[str, str]] = set()
    for entry in _live_events("verdict_reviewed", REVIEW_DEFER_WINDOW_HOURS):
        attrs = entry.get("attributes") or {}
        if attrs.get("choice") != "defer":
            continue
        note, kind = attrs.get("note"), attrs.get("proposed_kind")
        if not isinstance(note, str) or not note or kind not in ("used", "contested"):
            raise ValueError(f"malformed verdict_reviewed hold row: {attrs!r}")
        out.add((note, kind))
    return out


def _delegated_reasons(since_hours: int) -> dict[tuple[str, str], tuple[str, str, str]]:
    """(note, proposed_kind) → (judged kind, 이유, observed_at) for every successful
    「맡길게요」 judgment — a verdict_reviewed delegate row that actually carries the model's
    kind and reason. The word-flag stamps b300c46 wrote (choice=delegate with no kind or
    reason) are not judgments: they are skipped, never mistaken for one. A failed delegation
    (error field, no kind) leaves no judgment either — the row comes back unanswered."""
    out: dict[tuple[str, str], tuple[str, str, str]] = {}
    for entry in _live_events("verdict_reviewed", since_hours):
        attrs = entry.get("attributes") or {}
        if attrs.get("choice") != "delegate":
            continue
        note, proposed_kind = attrs.get("note"), attrs.get("proposed_kind")
        kind, reason = attrs.get("kind"), attrs.get("reason")
        if (
            not isinstance(note, str)
            or not note
            or proposed_kind not in ("used", "contested")
            or kind not in ("used", "contested")
            or not isinstance(reason, str)
            or not reason
        ):
            continue
        observed_at = entry.get("observed_at")
        if not isinstance(observed_at, str):
            continue
        key = (note, proposed_kind)
        if key not in out or observed_at >= out[key][2]:
            out[key] = (kind, reason, observed_at)
    return out


def _group_reviews(rows: list[ProposedVerdict]) -> list[ProposedVerdict]:
    """같은 노트를 같은 판정으로 (작업만 다르게) 본 행을 한 줄로 묶는다 — 오너가 묶음을
    고를 때 판정은 묶인 세션 전부에 가야 하니, 묶음 전체가 한 단추에 실린다. 첫 목격
    (최신) 순서가 묶음 안에서도 그대로고, 이유는 묶음에서 첫 번째로 남은 근거 문장이다."""
    order: list[tuple[str, str]] = []
    groups: dict[tuple[str, str], list[ProposedVerdict]] = {}
    for row in rows:
        key = (row.note, row.kind)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    out = []
    for key in order:
        members = groups[key]
        first = members[0]
        if len(members) == 1:
            out.append(first)
            continue
        out.append(
            first.model_copy(
                update={
                    "sessions": [m.session_id for m in members],
                    "reason": next((m.reason for m in members if m.reason), ""),
                }
            )
        )
    return out


def _live_proposed(since_hours: int) -> list[ProposedVerdict]:
    """The review lane's rows: session-end verdict_proposed events, contested first, then
    newest first, the same (note, kind) proposed by several sessions folded into one row,
    a pair a successful 「맡길게요」 judged carrying the model's 이유 in place of the scorer's
    sentence (the kind line itself stays the session's own claim), pairs the owner held
    within the 이레 window left off, at most REVIEW_LIMIT. A row missing a field this value
    needs is malformed — raise (F5/ROP), never skip: a dropped row here is a judgment the
    owner was never shown."""
    parsed: list[ProposedVerdict] = []
    for entry in _live_events("verdict_proposed", since_hours):
        attrs = entry.get("attributes") or {}
        try:
            parsed.append(
                ProposedVerdict(
                    session_id=attrs["session_id"],
                    note=attrs["note"],
                    kind=attrs["kind"],
                    at=entry["observed_at"],
                    reason=attrs.get("reason") or "",
                )
            )
        except (KeyError, TypeError, ValidationError) as e:
            raise ValueError(f"malformed verdict_proposed row: {attrs!r}: {e}") from e
    parsed.sort(key=lambda p: p.at, reverse=True)  # newest first
    parsed.sort(key=lambda p: p.kind != "contested")  # stable: contested keeps the head
    # The same judgement can land twice (two SessionEnd hooks, 1ms apart — 2026-09-30).
    seen: set[tuple[str, str, str]] = set()
    unique = [p for p in parsed if (key := (p.session_id, p.note, p.kind)) not in seen and not seen.add(key)]
    grouped = _group_reviews(unique)
    if grouped:
        delegated = _delegated_reasons(since_hours)
        if delegated:
            # 맡긴 판정이 있는 짝은 이유 한 줄을 그 판정의 것으로 바꾼다 — 다음 카드에 보이는
            # 이유가 모델이 남긴 문장. 짝의 종류 줄은 세션이 말한 것을 그대로 말하는 문구라
            # (「말이 나왔어요」) 판정에 따라 바뀌지 않는다: 뒤집힌 종류는 엔진 간선에만 실린다.
            grouped = [
                r.model_copy(update={"reason": delegated[(r.note, r.kind)][1]})
                if (r.note, r.kind) in delegated
                else r
                for r in grouped
            ]
        deferred = _deferred_review_pairs()
        grouped = [r for r in grouped if (r.note, r.kind) not in deferred]
    shown = grouped[:REVIEW_LIMIT]
    works = _session_works() if shown else {}
    out = []
    for r in shown:
        update: dict = {"note_title": _note_title(r.note), "work": works.get(r.session_id, "")}
        if r.sessions:
            update["works"] = [works.get(s, "") for s in r.sessions]
        out.append(r.model_copy(update=update))
    return out


_FRONTMATTER_FIELD = re.compile(r"^(title|project|date|omb_session_id):[ \t]*(.*?)[ \t]*$", re.M)


def _frontmatter_fields(text: str) -> dict[str, str]:
    return {
        key: value.strip("'\"")
        for key, value in _FRONTMATTER_FIELD.findall(vault_note.frontmatter_text(text))
    }


def _note_title(note: str) -> str:
    text = _live_read_note(note)
    return _frontmatter_fields(text).get("title", "") if text is not None else ""


def _work_line(fields: dict[str, str]) -> str:
    date = fields.get("date", "")
    parts = (fields.get("project", ""), date[5:10] if len(date) >= 10 else date, fields.get("title", ""))
    return " · ".join(part for part in parts if part)


def _session_works() -> dict[str, str]:
    """omb_session_id → the work line of the note that session left, over the whole vault. A
    session with more than one note (21 of 1,650 on 2026-09-30) names its first by file name —
    the note it left first — so the card says the same thing on every run."""
    works: dict[str, str] = {}
    unreadable = 0
    for path in sorted(glob.glob(os.path.join(_vault_dir(), "wiki", "*.md"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
        except OSError:
            unreadable += 1
            continue
        fields = _frontmatter_fields(head)
        if (session := fields.get("omb_session_id")) and session not in works:
            works[session] = _work_line(fields)
    if unreadable:
        print(f"[card] {unreadable} vault note(s) unreadable — their sessions show by id", file=sys.stderr)
    return works


def _held_repair_subjects() -> set[str]:
    """Subjects the owner held or dropped on the repair lane within the 이레 window."""
    out: set[str] = set()
    for entry in _live_events("repair_reviewed", REVIEW_DEFER_WINDOW_HOURS):
        subject = (entry.get("attributes") or {}).get("subject")
        if not isinstance(subject, str) or not subject:
            raise ValueError(f"malformed repair_reviewed row: {entry!r}")
        out.add(subject)
    return out


def _live_repairs(limit: int = REPAIRS_LIMIT) -> dict[str, Any]:
    url = f"{_door_url()}/repairs/split-subjects?limit={limit}"
    return _door_json(url)


def _live_merged_yesterday() -> int | None:
    """Sum of yesterday's subject_merged deleted_rows, or None when nothing merged — the head
    line's optional clause. Read straight from the engine's /events, the same route
    _live_past_verdicts already reads."""
    entries = _live_events("subject_merged", 24)
    if not entries:
        return None
    return sum(int((e.get("attributes") or {}).get("deleted_rows") or 0) for e in entries)


def _live_search(subject: str) -> list[dict[str, Any]]:
    """A candidate's past record — the BoringRetriever over the door's /search with claims=3:
    the door's own recall of what the engine has decided about this subject before
    (wiki-1765 step 1), read through the LangChain seam the advice slot's retriever plugs
    into. Returned in the dict shape advise has always read: 'id', 'snippet', and the hit
    metadata (source_path, counts, claims) unpacked alongside."""
    retriever = BoringRetriever(base_url=_door_url(), max_results=3, claims=3)
    return [{"id": doc.id, "snippet": doc.page_content, **doc.metadata} for doc in retriever.invoke(subject)]


def _vault_dir() -> str:
    return os.path.expanduser(os.environ.get("BORING_VAULT_DIR") or "~/oh-my-boring/vault")


def _live_read_note(note: str) -> str | None:
    """A hit's own note text, read from the host vault (the card runs on the host, not in a
    container). `note` looks like `/vault/wiki/wiki-NNNN.md`; missing on disk is a value —
    that evidence simply cannot verify — never an exception here."""
    relative = note.removeprefix("/vault/") if note.startswith("/vault/") else note.lstrip("/")
    path = os.path.join(_vault_dir(), relative)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None
