"""사건 표 — store.rs log_event · recent_events 의 칸 대열과 SQL.

커서를 받아 쓰고 커밋은 부르는 쪽(run.py)이 한 번 한다. 엔진은 사건마다 자동 커밋이라 중간
실패면 반쯤 쓰지만 여기는 한 요청 = 한 트랜잭션이다(의도한 차이). log_event 는 store.rs
그대로 — 먼저 통째를 가림(redact_json_value — 문자열 값만, 키는 손 안 댐)은 뒤 칸
대열(otel 오버라이드·심각도 대체표·text_field 트림)을 옛 순서 그대로 뺀다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from ohmyboring.search.redact import redact_json_value

#: store.rs:2608 log_event — 칸 순서는 엔진 INSERT 그대로(id·observed_at 은 표 기본값).
_INSERT_SQL = (
    "INSERT INTO event_log ("
    " time_unix_nano, severity_text, severity_number, service_name,"
    " component, event_name, status, trace_id, span_id, run_id, session_id,"
    " workflow, workflow_node, workflow_outcome, body, attributes, resource"
    ") VALUES ("
    " %(time_unix_nano)s, %(severity_text)s, %(severity_number)s, %(service_name)s,"
    " %(component)s, %(event_name)s, %(status)s, %(trace_id)s, %(span_id)s, %(run_id)s,"
    " %(session_id)s, %(workflow)s, %(workflow_node)s, %(workflow_outcome)s,"
    " %(body)s, %(attributes)s, %(resource)s);"
)

#: store.rs:2645 recent_events — WHERE 칸이 NULL 이면 필터 없음, 윈도는 make_interval(hours).
_SELECT_SQL = (
    "SELECT id, observed_at, time_unix_nano, severity_text, severity_number,"
    "       service_name, component, event_name, status, trace_id, span_id,"
    "       run_id, session_id, workflow, workflow_node, workflow_outcome,"
    "       body, attributes, resource"
    " FROM event_log"
    " WHERE (%(component)s::text IS NULL OR component = %(component)s)"
    "   AND (%(event_name)s::text IS NULL OR event_name = %(event_name)s)"
    "   AND (%(status)s::text IS NULL OR status = %(status)s)"
    "   AND (%(run_id)s::text IS NULL OR run_id = %(run_id)s)"
    "   AND (%(workflow)s::text IS NULL OR workflow = %(workflow)s)"
    "   AND (%(since_hours)s::int IS NULL OR observed_at >= now() - make_interval(hours => %(since_hours)s))"
    " ORDER BY observed_at DESC, id DESC"
    " LIMIT %(limit)s;"
)

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1
_I32_MAX = 2**31 - 1


@dataclass(frozen=True)
class EventRow:
    """event_log 한 행 — recent_events 의 SELECT 칸 순서."""

    id: int
    observed_at: datetime
    time_unix_nano: int | None
    severity_text: str
    severity_number: int
    service_name: str
    component: str
    event_name: str
    status: str
    trace_id: str | None
    span_id: str | None
    run_id: str | None
    session_id: str | None
    workflow: str | None
    workflow_node: str | None
    workflow_outcome: str | None
    body: Any
    attributes: Any
    resource: Any


def _text_field(event: Any, key: str) -> str | None:
    """store.rs text_field — 문자열이면 트림, 빈 칸은 NULL."""
    if not isinstance(event, dict):
        return None
    raw = event.get(key)
    if not isinstance(raw, str):
        return None
    trimmed = raw.strip()
    return trimmed or None


def severity_text_for_status(status: str) -> str:
    """store.rs severity_text_for_status — status 를 소문자로 접어 심각도 문자로."""
    match status.lower():
        case "failed" | "failure" | "error":
            return "ERROR"
        case "warn" | "warning":
            return "WARN"
        case "debug":
            return "DEBUG"
        case "trace":
            return "TRACE"
        case _:
            return "INFO"


def severity_number_for_text(severity_text: str) -> int:
    """store.rs severity_number_for_text — 심각도 문자를 대문자로 접어 수로."""
    match severity_text.upper():
        case "TRACE":
            return 1
        case "DEBUG":
            return 5
        case "WARN":
            return 13
        case "ERROR":
            return 17
        case "FATAL":
            return 21
        case _:
            return 9


def _otel_i64(otel: dict[str, Any], key: str) -> int | None:
    """serde Value::as_i64 — 정수 JSON 수만(소수·불·문자열은 None), i64 범위만."""
    raw = otel.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if raw < _I64_MIN or raw > _I64_MAX:
        return None
    return raw


def _otel_str(otel: dict[str, Any] | None, key: str) -> str | None:
    """otel 의 문자열 칸 — 문자열이면 그대로(트림 안 함), 아니면 None."""
    raw = otel.get(key) if otel else None
    return raw if isinstance(raw, str) else None


def _otel_or(otel: dict[str, Any] | None, key: str, default: Any) -> Any:
    """otel 에 칸이 있으면 그 값(종류 무관 — null 도 값), 없으면 기본값."""
    if otel is not None and key in otel:
        return otel[key]
    return default


def _event_name(event: Any, otel: dict[str, Any] | None) -> str:
    """store.rs — otel.event_name 이 문자열이면 트림 없이 그대로, 아니면 트림된 event 칸."""
    return _otel_str(otel, "event_name") or _text_field(event, "event") or ""


def _severity(otel: dict[str, Any] | None, status: str) -> tuple[str, int]:
    """store.rs — otel.severity_text 가 문자열이면 그대로, 아니면 status 대체표에서.
    수는 otel.severity_number 가 i32 에 들어가는 정수일 때만 그 값, 아니면 문자 대체표."""
    raw = _otel_str(otel, "severity_text")
    text = raw if raw is not None else severity_text_for_status(status)
    number = severity_number_for_text(text)
    otel_number = _otel_i64(otel, "severity_number") if otel else None
    if otel_number is not None and -_I32_MAX - 1 <= otel_number <= _I32_MAX:
        number = otel_number
    return text, number


def _service_name(resource: Any, component: str) -> str:
    """store.rs — resource.attributes.service.name 이 문자열이면 그대로, 아니면 component."""
    if isinstance(resource, dict):
        attrs = resource.get("attributes")
        if isinstance(attrs, dict):
            name = attrs.get("service.name")
            if isinstance(name, str):
                return name
    return component


def log_event(cur: Any, event: Any) -> None:
    """store.rs log_event — 통째 가림 → otel 오버라이드 → 심각도 대체표 → 17 칸 적기.

    id·observed_at 은 표 기본값이라 칸에 없다. otel 은 객체여야 쓴다(아니면 전부 기본값 —
    store.rs 의 and_then(Value::as_object) 와 같다)."""
    event = redact_json_value(event)
    otel = event.get("otel") if isinstance(event, dict) else None
    if not isinstance(otel, dict):
        otel = None
    component = _text_field(event, "component") or ""
    status = _text_field(event, "status") or ""
    event_name = _event_name(event, otel)
    severity_text, severity_number = _severity(otel, status)
    time_unix_nano = _otel_i64(otel, "time_unix_nano") if otel else None
    body = _otel_or(otel, "body", {"event.name": event_name, "status": status})
    attributes = _otel_or(otel, "attributes", event)
    resource = _otel_or(
        otel, "resource", {"attributes": {"service.name": component, "service.namespace": "oh-my-boring"}}
    )
    # jsonb 칸은 dict 그대로면 cannot adapt — Jsonb 로 감싸 psycopg 3 의 직렬화에 맡긴다.
    cur.execute(
        _INSERT_SQL,
        {
            "time_unix_nano": time_unix_nano,
            "severity_text": severity_text,
            "severity_number": severity_number,
            "service_name": _service_name(resource, component),
            "component": component,
            "event_name": event_name,
            "status": status,
            "trace_id": _otel_str(otel, "trace_id"),
            "span_id": _otel_str(otel, "span_id"),
            "run_id": _text_field(event, "run_id"),
            "session_id": _text_field(event, "session_id"),
            "workflow": _text_field(event, "workflow"),
            "workflow_node": _text_field(event, "workflow_node"),
            "workflow_outcome": _text_field(event, "workflow_outcome"),
            "body": Jsonb(body),
            "attributes": Jsonb(attributes),
            "resource": Jsonb(resource),
        },
    )


def recent_events(cur: Any, query: Any) -> list[EventRow]:
    """store.rs recent_events — 필터 칸이 None 이면 $n IS NULL 로 빠진다(파라미터 그대로 NULL)."""
    cur.execute(
        _SELECT_SQL,
        {
            "limit": query.limit,
            "component": query.component,
            "event_name": query.event_name,
            "status": query.status,
            "run_id": query.run_id,
            "workflow": query.workflow,
            "since_hours": query.since_hours,
        },
    )
    return [EventRow(*row) for row in cur.fetchall()]


def rfc3339(value: datetime) -> str:
    """chrono DateTime<Utc>::to_rfc3339() — UTC 끝은 +00:00, 소수는 chrono 규약(0 이면 생략,
    1/1000 초 단위면 세 자리, 아니면 여섯 자리 — postgres 는 마이크로초라 여기까지다)."""
    stamp = value.astimezone(UTC)
    fraction = ""
    if stamp.microsecond:
        if stamp.microsecond % 1000 == 0:
            fraction = f".{stamp.microsecond // 1000:03d}"
        else:
            fraction = f".{stamp.microsecond:06d}"
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "+00:00"


def _sorted_json(value: Any) -> Any:
    """serde_json Value 의 Map(BTreeMap) — 중첩 객체의 키를 코드포인트 순으로 낸다."""
    if isinstance(value, dict):
        return {key: _sorted_json(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sorted_json(item) for item in value]
    return value


def entry_payload(row: EventRow) -> dict[str, Any]:
    """serve.rs EventLogEntry — 칸 순서가 응답 바이트다(구조체는 선언 순, 중첩 Value 는 키 정렬)."""
    observed_at = rfc3339(row.observed_at)
    otel = _sorted_json(
        {
            "observed_timestamp": observed_at,
            "time_unix_nano": row.time_unix_nano,
            "severity_text": row.severity_text,
            "severity_number": row.severity_number,
            "body": row.body,
            "attributes": row.attributes,
            "resource": row.resource,
            "trace_id": row.trace_id,
            "span_id": row.span_id,
            "event_name": row.event_name,
        }
    )
    return {
        "id": row.id,
        "observed_at": observed_at,
        "time_unix_nano": row.time_unix_nano,
        "severity_text": row.severity_text,
        "severity_number": row.severity_number,
        "service_name": row.service_name,
        "component": row.component,
        "event": row.event_name,
        "status": row.status,
        "trace_id": row.trace_id,
        "span_id": row.span_id,
        "run_id": row.run_id,
        "session_id": row.session_id,
        "workflow": row.workflow,
        "workflow_node": row.workflow_node,
        "workflow_outcome": row.workflow_outcome,
        "body": _sorted_json(row.body),
        "attributes": _sorted_json(row.attributes),
        "resource": _sorted_json(row.resource),
        "otel": otel,
    }


def entries_payload(rows: list[EventRow], limit_applied: int) -> dict[str, Any]:
    """http.rs handle_events 의 EventLogResp — maybe_truncated 은 건수가 클램프 한도에 닿았는지."""
    return {
        "entries": [entry_payload(row) for row in rows],
        "limit_applied": limit_applied,
        "maybe_truncated": len(rows) >= limit_applied,
    }
