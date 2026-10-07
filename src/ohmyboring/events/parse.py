"""사건 요청의 경계 — POST /events · GET /events · MCP events 를 한 번 좁힌다.

엔진 http.rs 의 event_batch(events 칸이 배열이면 그 배열이 묶음, 아니면 본문 통째가 한
사건 — 칸 종류는 안 본다)와 EventLogReq(쿼리 문자열의 limit·component·event·status·
run_id·workflow·since_hours)와 mcp.rs mcp_events 의 인자 규약(문자열은 트림하고 빈 칸은
무시, 비정수 limit 은 50, since_hours 는 음수 거절·비정수는 무시)을 그대로 옮긴 순수 값 —
여기를 지난 값은 아래에서 다시 검사하지 않는다. 거절은 `Rejected` 값(HTTP 400 · MCP
-32602)이고 예외가 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

from ohmyboring.result import Either, Err, Ok

#: http.rs EVENT_INGEST_MAX_BATCH · EVENT_LOG_MAX_LIMIT.
EVENT_INGEST_MAX_BATCH = 100
EVENT_LOG_MAX_LIMIT = 1000
DEFAULT_LIMIT = 50

#: axum Query<serde_urlencoded> 가 내는 400 문장 — 비정수 쿼리 칸(엔진은 serde 가 먼저 본다).
QUERY_DESERIALIZE = "Failed to deserialize query string: invalid digit found in string"


def _query_deserialize(raw: str) -> Rejected:
    """serde 가 i32/i64 를 못 읽을 때의 Display — 숫자 모양이면 invalid value, 아니면 invalid digit."""
    if raw.lstrip("-").isdigit():
        return Rejected(f"Failed to deserialize query string: invalid value: integer `{raw}`, expected i64")
    return Rejected(QUERY_DESERIALIZE)


_I32_MAX = 2_147_483_647


@dataclass(frozen=True)
class Rejected:
    """요청 거절 — 엔진 400(검증·쿼리 모양)과 같은 status, MCP 에서는 -32602."""

    message: str
    status: int = 400


@dataclass(frozen=True)
class EventQuery:
    """GET /events 의 좁힌 모양 — 칸이 None 이면 필터 없음(엔진의 $n IS NULL 과 같다)."""

    limit: int
    component: str | None
    event_name: str | None
    status: str | None
    run_id: str | None
    workflow: str | None
    since_hours: int | None


def event_batch(body: Any) -> Either[list[Any], Rejected]:
    """http.rs event_batch — events 칸이 배열이면 그 배열(상한 초과 400), 아니면 본문 하나.

    events 칸이 배열이 아닌 값(문자열·숫자·객체 포함)이어도 본문 통째가 한 사건이다.
    칸 종류는 점검하지 않는다 — 배열 안의 값도 그대로 넘긴다."""
    if isinstance(body, dict):
        items = body.get("events")
        if isinstance(items, list):
            if len(items) > EVENT_INGEST_MAX_BATCH:
                return Err(Rejected(f"events batch too large: max {EVENT_INGEST_MAX_BATCH}"))
            return Ok(list(items))
    return Ok([body])


def _query_fields(query: str) -> Either[dict[str, str], Rejected]:
    """쿼리 문자열 → 칸 dict(중복은 마지막 값). 비정수 limit·since_hours 는 엔진 serde 400."""
    fields = dict(parse_qsl(query, keep_blank_values=True))
    for key in ("limit", "since_hours"):
        if key in fields:
            raw = fields[key]
            try:
                value = int(raw)
            except ValueError:
                return Err(_query_deserialize(raw))
            if value > _I32_MAX or value < -_I32_MAX - 1:
                return Err(_query_deserialize(raw))
            fields[key] = value
    return Ok(fields)


def parse_event_query(query: str) -> Either[EventQuery, Rejected]:
    """GET /events 쿼리 — serde 파싱(비정수 400) → since_hours 검사(음수 400) → 클램프 순.

    limit 은 빠지면 50, 클램프 1..1000(엔진과 같이 검증보다 뒤에서 — 응답의 limit_applied).
    문자열 필터는 트림하지 않는다(엔진도 안 한다 — 빈 문자열은 빈 문자열과 딱 맞는다)."""
    match _query_fields(query):
        case Err(rejected):
            return Err(rejected)
        case Ok(fields):
            pass
    since_hours = fields.get("since_hours")
    if isinstance(since_hours, int) and since_hours < 0:
        return Err(Rejected("since_hours must be >= 0"))
    limit = fields.get("limit")
    if not isinstance(limit, int):
        limit = DEFAULT_LIMIT
    return Ok(
        EventQuery(
            limit=max(1, min(limit, EVENT_LOG_MAX_LIMIT)),
            component=fields.get("component"),
            event_name=fields.get("event"),
            status=fields.get("status"),
            run_id=fields.get("run_id"),
            workflow=fields.get("workflow"),
            since_hours=since_hours if isinstance(since_hours, int) else None,
        )
    )


@dataclass(frozen=True)
class McpEventsArgs:
    """MCP events 의 좁힌 모양 — mcp.rs: limit 은 비정수면 50, 문자열 칸은 트림·빈 칸 무시."""

    limit: int
    component: str | None
    event_name: str | None
    status: str | None
    run_id: str | None
    workflow: str | None
    since_hours: int | None


def _mcp_text(args: dict[str, Any], key: str) -> str | None:
    """mcp.rs 의 문자열 칸 규약 — 문자열이면 트림, 빈 칸은 None(필터 없음)."""
    raw = args.get(key)
    if not isinstance(raw, str):
        return None
    trimmed = raw.strip()
    return trimmed or None


def parse_mcp_args(args: dict[str, Any]) -> Either[McpEventsArgs, Rejected]:
    """MCP events 인자 — mcp.rs mcp_events: 비정수 limit 은 50, since_hours 는 음수·너무 크면
    -32602, 비정수 since_hours 는 무시(None)다(엔진 as_i64 가 못 읽으면 그냥 없는 칸)."""
    raw_limit = args.get("limit")
    limit = raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else DEFAULT_LIMIT
    since_hours: int | None = None
    raw_since = args.get("since_hours")
    if isinstance(raw_since, int) and not isinstance(raw_since, bool):
        if raw_since < 0:
            return Err(Rejected("since_hours must be >= 0"))
        if raw_since > _I32_MAX:
            return Err(Rejected("since_hours is too large"))
        since_hours = raw_since
    return Ok(
        McpEventsArgs(
            limit=max(1, min(limit, EVENT_LOG_MAX_LIMIT)),
            component=_mcp_text(args, "component"),
            event_name=_mcp_text(args, "event"),
            status=_mcp_text(args, "status"),
            run_id=_mcp_text(args, "run_id"),
            workflow=_mcp_text(args, "workflow"),
            since_hours=since_hours,
        )
    )
