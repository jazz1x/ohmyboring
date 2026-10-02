"""읽기 그림자 (E4-1) — 문이 엔진 답을 바이트 그대로 돌려준 뒤, 같은 요청을 파이썬 읽기
길(pg.py)에 「읽기 전용으로」 태워 받은 답을 엔진 답과 칸별로 대조해 read_shadow 사건
한 줄을 만든다. 응답은 이미 간 뒤라 그림자는 답을 바꾸지도, 기다리게 하지도 않는다.

대조는 소비자가 읽는 칸을 따라간다 — 레지스터는 answer·sources(HTTP)와 거기에
items·limit_applied·total_matching 를 얹은 MCP 다섯 칸, /recurrences 는 rows·days·
max_distance·min_days_apart, /context 는 다섯 섹션+language, /status 는 sources 와
빈 경로 문구(답 자체는 생성이라 대조 안 함 — answer_compared=False 로 남긴다).

어긋남마다 사유 한 줄을 붙여 갈래를 낸다 — 어휘는 remember 그림자와 같다:
  (가) python 결함 — row 집합·칸 값이 갈라진 것 (같은 SQL 의도에서 나온 차이니 파이썬 쪽 결함으로 본다)
  (나) 엔진이 틀렸거나 의도와 다른 차이 — ORDER BY 가 타이다음을 정하지 않아 생기는 plan-dependent 순서
  (다) 모름 — 엔진 답을 못 읽음·파이썬 계산 고장·엔진·파이썬 사이에 쓰기가 낀 레이스(한 번 더 질의해
      엔진과 맞으면 레이스로 접는다)

status 는 ok|mismatch|error — 거절 대조(양쪽 다 인자 거절)가 같으면 ok, 다륾면 (가) 다.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ohmyboring.result import Either, Err, Ok

EVENT_NAME = "read_shadow"

#: /status 생성 답 대신 대조하는 결정론 칸 — 이 문구로 시작하면 빈 경로다.
_STATUS_EMPTY_PREFIX = "No recent records or claims found for project '"

_DISTANCE_TOL = 1e-6


@dataclass(frozen=True)
class PyFailure:
    """파이썬 길의 실패 — rejection 이면 엔진 거절과 대조 가능(문구까지 같아야 한다)."""

    message: str
    rejection: bool = False


@dataclass(frozen=True)
class ShadowRequest:
    """그림자 한 번의 입력 — 문 핸들러가 응답을 본 뒤 싣는다.

    query/rerun 은 문이 싣는 읽기 전용 계산 (같은 인자로 다시 답하는 두 번째 closure —
    rerun 은 row 집합 어긋남의 레이스 판정에만 쓴다)."""

    surface: str  # decisions|risks|next_actions|stalled|recurrences|context|status
    transport: str  # http|mcp
    arguments: dict
    engine_status: int
    engine_body: bytes
    query: Callable[[], Either[dict, PyFailure]]
    rerun: Callable[[], Either[dict, PyFailure]] | None = None


@dataclass(frozen=True)
class ShadowEvent:
    status: str  # ok|mismatch|error
    surface: str
    transport: str
    fields: tuple[str, ...] = ()
    reason: str | None = None
    python_defect: int = 0
    engine_diff: int = 0
    unknown: int = 0
    engine_rows: int = 0
    python_rows: int = 0
    answer_compared: bool = True
    elapsed_total_s: float = 0.0


@dataclass(frozen=True)
class _EngineRejection:
    """엔진이 인자를 거절한 답 — {"error": …} / JSON-RPC error. 대조 가능."""

    message: str


@dataclass(frozen=True)
class _EngineUnreadable:
    """엔진 답을 대조할 수 없는 모양 — 4xx 비 JSON·isError·본문 파싱 불가."""

    reason: str


#: ── 엔진 답 꺼내기 ──────────────────────────────────────────────────────────
def _loads_dict(raw: Any) -> dict | _EngineUnreadable:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return _EngineUnreadable("engine answer not JSON")
    if not isinstance(data, dict):
        return _EngineUnreadable("engine answer not an object")
    return data


def _rpc_error(error: Any) -> _EngineRejection | _EngineUnreadable:
    message = error.get("message") if isinstance(error, dict) else None
    if isinstance(message, str):
        return _EngineRejection(message)
    return _EngineUnreadable("JSON-RPC error without message")


def _mcp_result_payload(result: dict) -> dict | _EngineUnreadable:
    if result.get("isError"):
        return _EngineUnreadable("tool answered isError")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        text = content[0].get("text")
        if isinstance(text, str):
            parsed = _loads_dict(text)
            if isinstance(parsed, _EngineUnreadable):
                return _EngineUnreadable("tool text not JSON")
            return parsed
    return _EngineUnreadable("MCP result without structuredContent")


def _extract_mcp(data: dict) -> dict | _EngineRejection | _EngineUnreadable:
    if "error" in data:
        return _rpc_error(data["error"])
    result = data.get("result")
    if not isinstance(result, dict):
        return _EngineUnreadable("MCP answer without result")
    return _mcp_result_payload(result)


def _extract_http(data: dict, status: int) -> dict | _EngineRejection | _EngineUnreadable:
    if 200 <= status < 300:
        return data
    message = data.get("error")
    if isinstance(message, str):
        return _EngineRejection(message)
    return _EngineUnreadable(f"engine answered {status} non-JSON error")


def _extract_engine(request: ShadowRequest) -> dict | _EngineRejection | _EngineUnreadable:
    data = _loads_dict(request.engine_body)
    if isinstance(data, _EngineUnreadable):
        return data
    if request.transport == "mcp":
        return _extract_mcp(data)
    return _extract_http(data, request.engine_status)


#: ── 레지스터 answer 텍스트 파싱 (data-parity.py 가 파싱하는 모양과 같게) ──────
_ANSWER_HEADER_RE = re.compile(r"^Showing (\d+) of (\d+) matching claims \(limit_applied=(true|false)\)\.$")
_ANSWER_ROW_RE = re.compile(r"^\* (.*) — (.*): (.*) \(kind=(.*), confidence=(.*)\)$")


def _parse_register_answer(answer: Any) -> tuple[list[tuple[str, ...]], int, bool] | None:
    """render_register 텍스트를 (행 키, total_matching, limit_applied) 로 푼다.

    빈 경로 문구(No decisions recorded yet. 등)는 None — 빈 행으로 친다."""
    if not isinstance(answer, str):
        return None
    lines = answer.split("\n")
    header = _ANSWER_HEADER_RE.match(lines[0]) if lines else None
    if header is None:
        return None
    rows: list[tuple[str, ...]] = []
    for line in lines[1:]:
        row = _ANSWER_ROW_RE.match(line)
        if row is None:
            return None
        rows.append(row.groups())
    return rows, int(header.group(2)), header.group(3) == "true"


#: ── 어긋남 한 줄 모음 ───────────────────────────────────────────────────────
@dataclass
class _Verdict:
    """한 표면의 대조 끝 — 어긋난 칸 이름과 사유 한 줄들 (갈래별 카운트 포함).

    diverging=False 인 사유는 판정에 영향 없는 「모름」 계열 한 줄 (레이스 재확인) — 카운트와
    사유에는 남되, 사건 status 는 올리지 않는다 (remember 그림자의 graph-unchecked 와 같다)."""

    fields: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    python_defect: int = 0
    engine_diff: int = 0
    unknown: int = 0
    diverging: int = 0

    def add(self, field_name: str, reason: str, branch: str, *, diverging: bool = True) -> None:
        if diverging:
            if field_name not in self.fields:
                self.fields.append(field_name)
            self.diverging += 1
        self.reasons.append(reason)
        match branch:
            case "가":
                self.python_defect += 1
            case "나":
                self.engine_diff += 1
            case _:
                self.unknown += 1

    @property
    def diverged(self) -> bool:
        return self.diverging > 0


def _counts_branch(reasons: list[str]) -> tuple[int, int, int]:
    """사유 줄에 (가)/(나)/(다) 태그가 몇 개인지 — 시험이 갈래 카운트를 단언할 때 쓴다."""
    가 = 나 = 다 = 0
    for reason in reasons:
        if "(가)" in reason:
            가 += 1
        elif "(나)" in reason:
            나 += 1
        elif "(다)" in reason:
            다 += 1
    return 가, 나, 다


#: ── 순서·집합 대조의 공통 규칙 ──────────────────────────────────────────────
def _sequence_verdict(
    verdict: _Verdict,
    field_name: str,
    engine_seq: list,
    python_seq: list,
    *,
    branch_rerun_ok: Callable[[], bool],
) -> None:
    """같은 ORDER BY 의도의 두 순열 — 같으면 끝, 집합이 같고 순서만 갈리면 (나) plan-dependent
    타이, 집합이 갈리면 (가) (한 번 더 질의해 엔진과 맞으면 레이스 (다) 로 접는다)."""
    if engine_seq == python_seq:
        return
    engine_counter = Counter(repr(item) for item in engine_seq)
    python_counter = Counter(repr(item) for item in python_seq)
    if engine_counter == python_counter:
        verdict.add(field_name, f"{field_name} order tie-plan (나)", "나")
        return
    missing = sum((engine_counter - python_counter).values())
    extra = sum((python_counter - engine_counter).values())
    if branch_rerun_ok():
        verdict.add(
            field_name,
            f"{field_name} race (다) engine-differed-between-reads — rerun matched",
            "다",
            diverging=False,
        )
        return
    verdict.add(field_name, f"{field_name} rows missing={missing} extra={extra} (가)", "가")


def _scalar_verdict(verdict: _Verdict, field_name: str, engine: Any, python: Any) -> None:
    if engine != python:
        verdict.add(field_name, f"field {field_name} (가)", "가")


#: ── 표멸 대조 ───────────────────────────────────────────────────────────────
_REGISTER_ITEM_KEY = ("subject", "predicate", "value", "kind", "confidence", "valid_from", "project")
_CONTEXT_ITEM_KEY = ("subject", "predicate", "value", "kind", "confidence")
_CONTEXT_SECTIONS = ("decisions", "risks", "facts", "glossary", "next_actions")


def _item_key(item: Any, keys: tuple[str, ...]) -> tuple | None:
    if not isinstance(item, dict):
        return None
    return tuple(item.get(key) for key in keys)


def _compare_register(verdict, engine: dict, python: dict, request: ShadowRequest) -> None:
    mcp = request.transport == "mcp"

    def rerun_ok() -> bool:
        return _rerun_matches(request, engine)

    engine_answer = engine.get("answer")
    python_answer = python.get("answer")
    if engine_answer != python_answer:
        parsed_e = _parse_register_answer(engine_answer)
        parsed_p = _parse_register_answer(python_answer)
        if parsed_e is not None and parsed_p is not None:
            rows_e, total_e, limited_e = parsed_e
            rows_p, total_p, limited_p = parsed_p
            _sequence_verdict(verdict, "rows", rows_e, rows_p, branch_rerun_ok=rerun_ok)
            if total_e != total_p:
                verdict.add("total_matching", "field total_matching (가)", "가")
            if limited_e != limited_p:
                verdict.add("limit_applied", "field limit_applied (가)", "가")
            if not verdict.diverged and engine_answer != python_answer:
                verdict.add("answer", "field answer render (가)", "가")
        else:
            if rerun_ok():
                verdict.add(
                    "rows",
                    "rows race (다) engine-differed-between-reads — rerun matched",
                    "다",
                    diverging=False,
                )
            else:
                verdict.add("answer", "field answer (가)", "가")
    _sequence_verdict(
        verdict,
        "sources",
        _as_str_list(engine.get("sources")),
        _as_str_list(python.get("sources")),
        branch_rerun_ok=rerun_ok,
    )
    if mcp:
        engine_items = [_item_key(i, _REGISTER_ITEM_KEY) for i in _as_list(engine.get("items"))]
        python_items = [_item_key(i, _REGISTER_ITEM_KEY) for i in _as_list(python.get("items"))]
        if None in engine_items or None in python_items:
            verdict.add("items", "field items shape (가)", "가")
        else:
            _sequence_verdict(verdict, "items", engine_items, python_items, branch_rerun_ok=rerun_ok)
        _scalar_verdict(verdict, "limit_applied", engine.get("limit_applied"), python.get("limit_applied"))
        _scalar_verdict(verdict, "total_matching", engine.get("total_matching"), python.get("total_matching"))


def _compare_context(verdict, engine: dict, python: dict, request: ShadowRequest) -> None:
    for name in _CONTEXT_SECTIONS:
        engine_items = [_item_key(i, _CONTEXT_ITEM_KEY) for i in _as_list(engine.get(name))]
        python_items = [_item_key(i, _CONTEXT_ITEM_KEY) for i in _as_list(python.get(name))]
        _sequence_verdict(
            verdict, name, engine_items, python_items, branch_rerun_ok=lambda: _rerun_matches(request, engine)
        )
    _scalar_verdict(verdict, "language", engine.get("language"), python.get("language"))


def _recurrence_group_key(row: Any) -> tuple | None:
    if not isinstance(row, dict):
        return None
    newer = row.get("newer")
    if not isinstance(newer, dict):
        return None
    return (
        newer.get("source_path"),
        newer.get("subject"),
        newer.get("predicate"),
        newer.get("valid_from"),
    )


def _claimref_key(ref: Any) -> tuple | None:
    if not isinstance(ref, dict):
        return None
    return (ref.get("source_path"), ref.get("valid_from"))


def _compare_recurrences(verdict, engine: dict, python: dict, request: ShadowRequest) -> None:
    for scalar in ("days", "min_days_apart"):
        _scalar_verdict(verdict, scalar, engine.get(scalar), python.get(scalar))
    engine_max = engine.get("max_distance")
    python_max = python.get("max_distance")
    if not _float_close(engine_max, python_max):
        verdict.add("max_distance", "field max_distance (가)", "가")
    engine_rows = _as_list(engine.get("rows"))
    python_rows = _as_list(python.get("rows"))
    engine_keys = [_recurrence_group_key(row) for row in engine_rows]
    python_keys = [_recurrence_group_key(row) for row in python_rows]
    if None in engine_keys or None in python_keys:
        verdict.add("rows", "field rows shape (가)", "가")
        return

    def rerun_ok() -> bool:
        return _rerun_matches(request, engine)

    if sorted(map(repr, engine_keys)) != sorted(map(repr, python_keys)):
        if rerun_ok():
            verdict.add(
                "rows",
                "rows race (다) engine-differed-between-reads — rerun matched",
                "다",
                diverging=False,
            )
            return
        engine_counter = Counter(repr(key) for key in engine_keys)
        python_counter = Counter(repr(key) for key in python_keys)
        missing = sum((engine_counter - python_counter).values())
        extra = sum((python_counter - engine_counter).values())
        verdict.add("rows", f"rows missing={missing} extra={extra} (가)", "가")
        return
    if engine_keys != python_keys:
        verdict.add("rows", "rows order tie-plan (나)", "나")
    python_by_key = {repr(key): row for key, row in zip(python_keys, python_rows)}
    for key, engine_row in zip(engine_keys, engine_rows):
        python_row = python_by_key.get(repr(key))
        if python_row is None:
            continue
        _compare_recurrence_group(verdict, engine_row, python_row)


def _compare_recurrence_group(verdict, engine_row: Any, python_row: Any) -> None:
    if not isinstance(engine_row, dict) or not isinstance(python_row, dict):
        return
    engine_older = [_claimref_key(ref) for ref in _as_list(engine_row.get("older"))]
    python_older = [_claimref_key(ref) for ref in _as_list(python_row.get("older"))]
    if sorted(map(repr, engine_older)) != sorted(map(repr, python_older)):
        verdict.add("older", "older rows (가)", "가")
    elif engine_older != python_older:
        verdict.add("older", "older order tie-plan (나)", "나")
    if not _float_close(engine_row.get("distance"), python_row.get("distance")):
        verdict.add("distance", "field distance (가)", "가")
    _scalar_verdict(verdict, "days_apart", engine_row.get("days_apart"), python_row.get("days_apart"))
    _scalar_verdict(verdict, "label_only", engine_row.get("label_only"), python_row.get("label_only"))


def _compare_status(verdict, engine: dict, python: dict, request: ShadowRequest) -> None:
    engine_sources = _as_str_list(engine.get("sources"))
    python_sources = _as_str_list(python.get("sources"))
    _sequence_verdict(
        verdict,
        "sources",
        engine_sources,
        python_sources,
        branch_rerun_ok=lambda: _rerun_matches(request, engine),
    )
    engine_answer = engine.get("answer")
    python_answer = python.get("answer")
    engine_empty = isinstance(engine_answer, str) and engine_answer.startswith(_STATUS_EMPTY_PREFIX)
    python_empty = isinstance(python_answer, str) and python_answer.startswith(_STATUS_EMPTY_PREFIX)
    if engine_empty or python_empty:
        if engine_answer != python_answer:
            if _rerun_matches(request, engine):
                verdict.add(
                    "answer",
                    "answer race (다) engine-differed-between-reads — rerun matched",
                    "다",
                    diverging=False,
                )
            else:
                verdict.add("answer", "field answer empty-path (가)", "가")
    elif verdict.diverged:
        pass  # sources 가 갈라진 것 — 답 대조는 sources 가 맞을 때만 의미 있다
    # 양쪽 다 비어 있지 않으면 답은 생성이라 대조 안 함 — answer_compared=False 로 남긴다


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_str_list(value: Any) -> list[str]:
    return [item for item in _as_list(value) if isinstance(item, str)]


def _float_close(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return a == b
    return abs(float(a) - float(b)) <= _DISTANCE_TOL


def _rerun_matches(request: ShadowRequest, engine_payload: dict) -> bool:
    """row 집합 어긋남의 재확인 — 같은 인자로 한 번 더 읽어 엔진 답과 같아지면 레이스."""
    if request.rerun is None:
        return False
    match request.rerun():
        case Err(_):
            return False
        case Ok(payload):
            return not _diverges(request, engine_payload, payload, allow_rerun=False)


def _diverges(request: ShadowRequest, engine: dict, python: dict, *, allow_rerun: bool) -> bool:
    verdict = _Verdict()
    _compare_payload(verdict, request, engine, python, allow_rerun=allow_rerun)
    return verdict.diverged


def _compare_payload(
    verdict: _Verdict, request: ShadowRequest, engine: dict, python: dict, *, allow_rerun: bool
) -> None:
    if not allow_rerun:
        request = ShadowRequest(**{**request.__dict__, "rerun": None})
    match request.surface:
        case "decisions" | "risks" | "next_actions" | "stalled":
            _compare_register(verdict, engine, python, request)
        case "recurrences":
            _compare_recurrences(verdict, engine, python, request)
        case "context":
            _compare_context(verdict, engine, python, request)
        case "status":
            _compare_status(verdict, engine, python, request)


def _row_count(surface: str, payload: dict) -> int:
    if surface in ("decisions", "risks", "next_actions", "stalled"):
        items = payload.get("items")
        if isinstance(items, list):
            return len(items)
        parsed = _parse_register_answer(payload.get("answer"))
        return len(parsed[0]) if parsed else 0
    if surface == "recurrences":
        return len(_as_list(payload.get("rows")))
    if surface == "context":
        return sum(len(_as_list(payload.get(name))) for name in _CONTEXT_SECTIONS)
    return len(_as_str_list(payload.get("sources")))


#: ── 몸통 ────────────────────────────────────────────────────────────────────
def run_shadow(request: ShadowRequest) -> ShadowEvent:
    """그림자 한 번 — 엔진 답을 꺼내 파이썬 답과 대조해 사건 한 줄을 만든다.

    실패는 전부 사건 값으로 (문 핸들러가 try_append_event 로 기록) — 응답은 이미 간 뒤라
    어떤 예외도 클라이언트에 닿지 않는다."""
    started = time.monotonic()

    def finish(event: ShadowEvent) -> ShadowEvent:
        return ShadowEvent(**{**event.__dict__, "elapsed_total_s": round(time.monotonic() - started, 3)})

    engine = _extract_engine(request)
    if isinstance(engine, _EngineUnreadable):
        return finish(
            ShadowEvent(
                "error",
                request.surface,
                request.transport,
                reason=f"engine unreadable (다): {engine.reason}",
                unknown=1,
            )
        )
    match request.query():
        case Err(failure):
            return finish(_python_failure_event(request, engine, failure))
        case Ok(python_payload):
            pass
    if isinstance(engine, _EngineRejection):
        return finish(
            ShadowEvent(
                "mismatch",
                request.surface,
                request.transport,
                fields=("decision",),
                reason=f"engine rejected python answered (가): {engine.message!r}",
                python_defect=1,
            )
        )
    verdict = _Verdict()
    _compare_payload(verdict, request, engine, python_payload, allow_rerun=True)
    answer_compared = not (
        request.surface == "status" and not verdict.diverged and not _is_status_empty(engine.get("answer"))
    )
    return finish(
        ShadowEvent(
            "mismatch" if verdict.diverged else "ok",
            request.surface,
            request.transport,
            fields=tuple(verdict.fields),
            reason="; ".join(verdict.reasons) or None,
            python_defect=verdict.python_defect,
            engine_diff=verdict.engine_diff,
            unknown=verdict.unknown,
            engine_rows=_row_count(request.surface, engine),
            python_rows=_row_count(request.surface, python_payload),
            answer_compared=answer_compared,
        )
    )


def _is_status_empty(answer: Any) -> bool:
    return isinstance(answer, str) and answer.startswith(_STATUS_EMPTY_PREFIX)


def _python_failure_event(request: ShadowRequest, engine: Any, failure: PyFailure) -> ShadowEvent:
    """파이썬 길이 거절·고장했을 때 — 거절끼리는 문구 대조, 고장은 (다) error."""
    if isinstance(engine, _EngineRejection) and failure.rejection:
        if engine.message == failure.message:
            return ShadowEvent(
                "ok",
                request.surface,
                request.transport,
                reason="both rejected identically",
                engine_rows=0,
                python_rows=0,
            )
        return ShadowEvent(
            "mismatch",
            request.surface,
            request.transport,
            fields=("decision",),
            reason=f"rejection engine={engine.message!r} python={failure.message!r} (가)",
            python_defect=1,
        )
    if failure.rejection:
        return ShadowEvent(
            "mismatch",
            request.surface,
            request.transport,
            fields=("decision",),
            reason=f"python rejected engine-accepted {failure.message!r} (가)",
            python_defect=1,
        )
    return ShadowEvent(
        "error",
        request.surface,
        request.transport,
        reason=f"python-error (다): {failure.message}",
        unknown=1,
    )


def event_payload(event: ShadowEvent) -> dict[str, Any]:
    """adapters/events 기록용 본문 — 값 원문은 전부 빠지고 칸 이름·개수·사유만 싣는다."""
    payload: dict[str, Any] = {
        "transport": event.transport,
        "fields": list(event.fields),
        "python_defect": event.python_defect,
        "engine_diff": event.engine_diff,
        "unknown": event.unknown,
        "engine_rows": event.engine_rows,
        "python_rows": event.python_rows,
        "answer_compared": event.answer_compared,
        "elapsed_total_s": event.elapsed_total_s,
    }
    if event.reason is not None:
        payload["reason"] = event.reason
    return payload
