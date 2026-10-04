"""회상 그림자 (E4-2) — 문이 엔진 recall 답을 그대로 돌려준 뒤, 같은 요청을 파이썬 회상 길에
읽기 전용으로 태워 엔진 텍스트와 글자 단위로 대조해 recall_shadow 사건 한 줄을 만든다.

대조 대상은 문이 번호 노트 블록을 붙이기 전의 엔진 텍스트다. 사건에는 질의 원문을 싣지 않는다 —
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

from ohmyboring.recall.answer import Failed, Recalled, Rejected, WikiPool
from ohmyboring.result import Either, Err, Ok

EVENT_NAME = "recall_shadow"
_DIFF_CHARS = 80

PythonAnswer = Either[Recalled, Rejected | Failed]


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


def _compare_text(engine: str, python: Recalled) -> ShadowEvent:
    engine_lines = engine.split("\n")
    python_lines = python.text.split("\n")
    same = engine == python.text
    tie = not same and python.pool is not None and _tie_only(engine, python.pool)
    status, reason = (
        ("ok", None) if same else ("tie", "tie-only difference") if tie else ("mismatch", "text differs")
    )
    return ShadowEvent(
        status,
        python.path,
        len(engine_lines),
        len(python_lines),
        first_diff=None if same else _first_diff(engine_lines, python_lines),
        reason=reason,
        skipped=python.skipped,
    )


def compare(engine_status: int, engine_body: bytes, python: PythonAnswer) -> ShadowEvent:
    match (_engine_side(engine_status, engine_body), python):
        case (_EngineUnreadable(reason), _):
            return ShadowEvent("error", reason=f"engine unreadable: {reason}")
        case (_, Err(Failed(detail))):
            return ShadowEvent("error", reason=f"python failed: {detail}")
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
            return _compare_text(text, recalled)


def run_shadow(engine_status: int, engine_body: bytes, python: Callable[[], PythonAnswer]) -> ShadowEvent:
    return compare(engine_status, engine_body, python())


def event_payload(event: ShadowEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
    return payload
