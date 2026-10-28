"""회상 그림자 (E4-2) — 문이 엔진 recall 답을 그대로 돌려준 뒤, 같은 요청을 파이썬 회상 길에
읽기 전용으로 태워 엔진 텍스트와 글자 단위로 대조해 recall_shadow 사건 한 줄을 만든다.

대조 대상은 문이 실제로 내보내는 refined 텍스트다 — 엔진 블록(번호 노트 블록 붙이기 전)에도 파이썬 쪽과
같은 refine(claims·제목·힌트·실패 꼬리)을 태워 견준다(compared="refined"). error 는 cause(embed|engine|python)를 단다. 사건에는 질의 원문을 싣지 않는다 —
길(wiki|vector)·두 쪽 줄 수·처음 갈린 줄(번호와 두 줄의 앞 80자)·사유·건너뛴 wiki 파일 수(skipped)뿐이다.
status 는 ok(글자까지 같음)·tie(점수 동점 때문에만 다름 — 엔진은 동점을 HashMap 순서로 가르고 파이썬은
경로순으로 고정한다)·mismatch·error.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any

from ohmyboring.recall import answer
from ohmyboring.recall.answer import Failed, Recalled, Rejected, WikiPool
from ohmyboring.result import Either, Err, Ok
from ohmyboring.search import pg

EVENT_NAME = "recall_shadow"
COMPARED = "refined"
CAUSE_EMBED, CAUSE_ENGINE, CAUSE_PYTHON = "embed", "engine", "python"
_DIFF_CHARS = 80

PythonAnswer = Either[Recalled, Rejected | Failed]
ClaimsFor = Callable[[list[str]], Either[dict[str, pg.RegisterRows], str]]


@dataclass(frozen=True)
class FirstDiff:
    line: int
    engine: str
    python: str


@dataclass(frozen=True)
class ShadowEvent:
    status: str  # ok|tie|mismatch|error
    path: str | None = None
    engine_lines: int = 0
    python_lines: int = 0
    first_diff: FirstDiff | None = None
    reason: str | None = None
    skipped: int = 0
    cause: str | None = None  # error 만 — embed|engine|python
    superseded_hit: bool = False  # 답에 실린 노트 중 대체된 노트가 있었나
    compared: str = COMPARED  # 무엇끼리 견줬나 — 옛 text 기준 숫자와 섞이지 않게


@dataclass(frozen=True)
class _EngineText:
    text: str


@dataclass(frozen=True)
class _EngineRejection:
    message: str


@dataclass(frozen=True)
class _EngineUnreadable:
    reason: str


def _engine_side(status: int, body: bytes) -> _EngineText | _EngineRejection | _EngineUnreadable:
    if not 200 <= status < 300:
        return _EngineUnreadable(f"engine answered {status}")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return _EngineUnreadable("engine answer not JSON")
    if not isinstance(data, dict):
        return _EngineUnreadable("engine answer not an object")
    if "error" in data:
        return _rpc_error(data["error"])
    return _result_text(data.get("result"))


def _rpc_error(error: Any) -> _EngineRejection | _EngineUnreadable:
    message = error.get("message") if isinstance(error, dict) else None
    if isinstance(message, str):
        return _EngineRejection(message)
    return _EngineUnreadable("JSON-RPC error without message")


def _result_text(result: Any) -> _EngineText | _EngineUnreadable:
    if not isinstance(result, dict) or result.get("isError"):
        return _EngineUnreadable("MCP result missing or isError")
    content = result.get("content")
    first = content[0] if isinstance(content, list) and content else None
    text = first.get("text") if isinstance(first, dict) else None
    if not isinstance(text, str):
        return _EngineUnreadable("MCP result without text content")
    return _EngineText(text)


def _first_diff(engine: list[str], python: list[str]) -> FirstDiff | None:
    pairs = enumerate(zip_longest(engine, python, fillvalue=""), start=1)
    return next((FirstDiff(n, e[:_DIFF_CHARS], p[:_DIFF_CHARS]) for n, (e, p) in pairs if e != p), None)


def _tie_only(engine: str, pool: WikiPool) -> bool:
    """엔진 답이 파이썬 열쇠로 가능한 답 중 하나인가 — 동점(열쇠가 정확히 같음)만 엔진 쪽 HashMap
    순서로 갈릴 수 있다. 멤버십은 점수 컷(k 번째 점수보다 높은 노트는 반드시 든다)이고, 든 노트들의
    순서 열쇠는 줄어들지 않아야 하며, 줄 글자는 노트별로 파이썬의 줄과 같아야 한다."""
    by_line = {entry.line: entry for entry in pool.entries}
    chosen = [by_line.get(line) for line in engine.split("\n\n")]
    if any(entry is None for entry in chosen) or len(chosen) != pool.size:
        return False
    entries = [entry for entry in chosen if entry is not None]
    if len({entry.path for entry in entries}) != pool.size:
        return False
    cut = pool.entries[pool.size - 1].score
    kept = {entry.path for entry in entries}
    if any(entry.score > cut and entry.path not in kept for entry in pool.entries):
        return False
    keys = [entry.key for entry in entries]
    return all(a <= b for a, b in zip(keys, keys[1:]))


def _cause_of(detail: str) -> str:
    return CAUSE_EMBED if "embed:" in detail else CAUSE_PYTHON


def _engine_refined(engine: str, python: Recalled, claims: dict[str, pg.RegisterRows]) -> str:
    """엔진 블록에 문이 내보낼 때와 같은 refine 을 태운다 — 경로는 파이썬 후보 블록에서 되찾는다."""
    context = python.context
    paths = dict(context.block_paths) if context is not None else {}
    blocks = [(paths.get(block, ""), block) for block in engine.split("\n\n")]
    superseded, titles, concepts = (
        (context.superseded, context.titles, context.concepts) if context is not None else ({}, {}, {})
    )
    return answer.refine_blocks(blocks, superseded, claims, titles, concepts) + answer.failed_tail(
        python.failed
    )


def _shared_entries_agree(engine_refined: str, python_refined: str) -> bool:
    """동점으로 엔진이 다른 노트를 골랐을 수 있다 — 양쪽에 다 든 노트(`- [이름]` 머리)는 refine 된 모양이 같아야 한다."""

    def by_head(text: str) -> dict[str, str]:
        return {entry.split("]")[0]: entry for entry in text.split("\n\n")}

    engine, python = by_head(engine_refined), by_head(python_refined)
    return all(engine[head] == python[head] for head in engine.keys() & python.keys())


def _compare_text(engine: str, python: Recalled, claims: dict[str, pg.RegisterRows]) -> ShadowEvent:
    engine_refined = _engine_refined(engine, python, claims)
    engine_lines = engine_refined.split("\n")
    python_lines = python.refined.split("\n")
    same_text = engine == python.text
    same = engine_refined == python.refined
    tie_text = not same_text and python.pool is not None and _tie_only(engine, python.pool)
    tie = tie_text and _shared_entries_agree(engine_refined, python.refined)
    status, reason = (
        ("ok", None)
        if same
        else ("tie", "tie-only difference")
        if tie
        else ("mismatch", "refined differs" if same_text or tie_text else "text differs")
    )
    return ShadowEvent(
        status,
        python.path,
        len(engine_lines),
        len(python_lines),
        first_diff=None if same else _first_diff(engine_lines, python_lines),
        reason=reason,
        skipped=python.skipped,
        superseded_hit=_superseded_hit(python),
    )


def _superseded_hit(python: Recalled) -> bool:
    context = python.context
    return context is not None and any(path in context.superseded for path in python.shown)


def _engine_paths(engine: str, python: Recalled) -> list[str]:
    paths = dict(python.context.block_paths) if python.context is not None else {}
    return sorted({paths[block] for block in engine.split("\n\n") if block in paths})


def _compare_with_claims(engine: str, python: Recalled, claims_for: ClaimsFor) -> ShadowEvent:
    paths = _engine_paths(engine, python)
    match claims_for(paths) if paths else Ok({}):
        case Err(detail):
            return ShadowEvent("error", reason=f"python claims lookup failed: {detail}", cause=CAUSE_PYTHON)
        case Ok(claims):
            return _compare_text(engine, python, claims)


def compare(
    engine_status: int, engine_body: bytes, python: PythonAnswer, claims_for: ClaimsFor
) -> ShadowEvent:
    match (_engine_side(engine_status, engine_body), python):
        case (_EngineUnreadable(reason), _):
            return ShadowEvent("error", reason=f"engine unreadable: {reason}", cause=CAUSE_ENGINE)
        case (_, Err(Failed(detail))):
            return ShadowEvent("error", reason=f"python failed: {detail}", cause=_cause_of(detail))
        case (_EngineRejection(message), Err(Rejected(python_message))):
            same = message == python_message
            return ShadowEvent(
                "ok" if same else "mismatch",
                reason="both rejected identically"
                if same
                else f"rejection engine={message!r} python={python_message!r}",
            )
        case (_EngineRejection(message), Ok(_)):
            return ShadowEvent("mismatch", reason=f"engine rejected {message!r}, python answered")
        case (_EngineText(_), Err(Rejected(python_message))):
            return ShadowEvent("mismatch", reason=f"python rejected {python_message!r}, engine answered")
        case (_EngineText(text), Ok(recalled)):
            return _compare_with_claims(text, recalled, claims_for)


def event_payload(event: ShadowEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "compared": event.compared,
        "superseded_hit": event.superseded_hit,
        "path": event.path,
        "engine_lines": event.engine_lines,
        "python_lines": event.python_lines,
        "skipped": event.skipped,
    }
    if event.first_diff is not None:
        payload["first_diff"] = {
            "line": event.first_diff.line,
            "engine": event.first_diff.engine,
            "python": event.first_diff.python,
        }
    if event.reason is not None:
        payload["reason"] = event.reason
    if event.cause is not None:
        payload["cause"] = event.cause
    return payload
