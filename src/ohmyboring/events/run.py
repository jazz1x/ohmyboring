"""사건 쓰기·읽기 한 번 — 요청 하나가 트랜잭션 하나다 (E4-6a, DOOR_EVENTS_OWNER=python).

엔진 http.rs handle_event_ingest·handle_events 와 mcp.rs mcp_events 의 순서를 옮긴다:
저장소 확인(500 · -32603) → 검증(400 · -32602) → 질의. 엔진과 달리 쓰기 전체가 한 커밋이라
중간 실패면 아무것도 안 남는다(엔진은 사걸마다 자동 커밋 — 의도한 차이). 문(door)만 부른다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg

from ohmyboring.events import parse as events_parse
from ohmyboring.events import pg as events_pg
from ohmyboring.result import Either, Err, Ok


@dataclass(frozen=True)
class Failed:
    """쓰기·읽기 실패 — 연결·SQL 이 거절한 것. 요청 모양 탓이 아니다(HTTP 500 · MCP -32603)."""

    message: str


#: serve.rs vector_disabled() — 저장소가 없을 때 엔진이 내는 문장 그대로(HTTP 500 · MCP -32603).
STORE_OFF_MESSAGE = (
    "BORING_VECTOR=off — this feature requires the vector backend (pgvector). "
    "Set BORING_VECTOR=on and start Postgres."
)


@dataclass(frozen=True)
class StoreOff:
    """쓸 저장소·읽을 저장소가 없다 — 검증을 지난 뒤에만 나온다(엔진과 같은 순서)."""

    message: str = STORE_OFF_MESSAGE


@dataclass(frozen=True)
class Deps:
    """문이 싣는 면 — connect 는 쓰기·읽기 가능한 연결 공장(None 이면 저장소 없음)."""

    connect: Callable[[], Any] | None


def _transact(deps: Deps, work: Callable[[Any], Any]) -> Either[Any, Failed | StoreOff]:
    """연결 → 커서 → work → 커밋 한 번. psycopg 가 던진 것은 여기서 Failed 값으로 접힌다."""
    if deps.connect is None:
        return Err(StoreOff())
    try:
        with deps.connect() as conn, conn.cursor() as cur:
            return Ok(work(cur))
    except psycopg.Error as e:
        return Err(Failed(str(e)))


def ingest(body: Any, deps: Deps) -> Either[dict[str, Any], events_parse.Rejected | Failed | StoreOff]:
    """POST /events — 저장소 확인(엔진 먼저) → 묶음 검사 → 한 트랜잭션으로 전부 적는다.

    빈 묶음은 표를 안 만진다 — 엔진은 사건 루프를 한 바퀴도 안 돌아 저장소 호출 없이
    {"accepted": 0} 만 낸다."""
    if deps.connect is None:
        return Err(StoreOff())
    match events_parse.event_batch(body):
        case Err(rejected):
            return Err(rejected)
        case Ok(events):
            pass
    if not events:
        return Ok({"accepted": 0})

    def work(cur: Any) -> dict[str, Any]:
        for event in events:
            events_pg.log_event(cur, event)
        return {"accepted": len(events)}

    return _transact(deps, work)


def read(query: str, deps: Deps) -> Either[dict[str, Any], events_parse.Rejected | Failed | StoreOff]:
    """GET /events — 쿼리 검사(음수 since_hours 400) → 저장소 확인 → 읽기. 엔진 순서 그대로."""
    match events_parse.parse_event_query(query):
        case Err(rejected):
            return Err(rejected)
        case Ok(parsed):
            pass
    if deps.connect is None:
        return Err(StoreOff())

    def work(cur: Any) -> dict[str, Any]:
        return events_pg.entries_payload(events_pg.recent_events(cur, parsed), parsed.limit)

    return _transact(deps, work)


def mcp_read(
    args: dict[str, Any], deps: Deps
) -> Either[dict[str, Any], events_parse.Rejected | Failed | StoreOff]:
    """MCP events — 저장소 확인(엔진 먼저) → 인자 검사(-32602) → 읽기. 실패는 `events: ` prefix."""
    if deps.connect is None:
        return Err(StoreOff())
    match events_parse.parse_mcp_args(args):
        case Err(rejected):
            return Err(rejected)
        case Ok(parsed):
            pass

    def work(cur: Any) -> dict[str, Any]:
        return {"entries": [events_pg.entry_payload(row) for row in events_pg.recent_events(cur, parsed)]}

    return _transact(deps, work)
