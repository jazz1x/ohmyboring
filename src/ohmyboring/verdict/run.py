"""판정 쓰기 한 번 — 요청 하나가 트랜잭션 하나다 (E4-5, DOOR_VERDICT_WRITER=python).

엔진 http.rs handle_consumption·handle_handover 와 mcp.rs mcp_verdict 의 순서를 옮긴다: 자격 →
(비오너의 supersedes 가 오너 노트를 겨누면 버리고 사건 한 줄) → 판정 간선 → supersedes+봉인.
엔진과 달리 쓰기 전체가 한 커밋이라 중간에 실패하면 아무것도 안 남는다. 거절 사건은 트랜잭션
밖 싱크(append_event)로 나가 롤백에 안 딸려 간다 — 엔진도 쓰기 전에 먼저 적는다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import psycopg

from ohmyboring.remember.writer import _OWNER_COMPONENT, OWNER_REFUSED_EVENT
from ohmyboring.result import Either, Err, Ok, map_ok
from ohmyboring.search import pg as search_pg
from ohmyboring.verdict import pg as verdict_pg
from ohmyboring.verdict.parse import (
    OWNER_TOKEN_NEEDED,
    Basis,
    Consumption,
    Handed,
    Handover,
    Listed,
    Rejected,
    Verdict,
    VerdictCall,
)

#: 거절 사건의 문 이름 — owner.rs refused_supersedes(…, "consumption").
REFUSED_DOOR = "consumption"


@dataclass(frozen=True)
class Failed:
    """쓰기 실패 — 연결·SQL 이 거절한 것. 요청 모양 탓이 아니다(HTTP 500 · MCP -32603)."""

    message: str


#: serve.rs vector_disabled() — 저장소가 없을 때 엔진이 내는 문장 그대로(HTTP 500 · MCP -32603).
STORE_OFF_MESSAGE = (
    "BORING_VECTOR=off — this feature requires the vector backend (pgvector). "
    "Set BORING_VECTOR=on and start Postgres."
)


@dataclass(frozen=True)
class StoreOff:
    """쓸 저장소가 없다 — 검증·자격을 지난 뒤에만 나온다(엔진과 같은 순서)."""

    message: str = STORE_OFF_MESSAGE


@dataclass(frozen=True)
class Deps:
    """문이 싣는 면 — connect 는 쓰기 가능한 연결 공장(None 이면 저장소 없음), append_event 는 사건 싱크(시험은 수집기)."""

    connect: Callable[[], Any] | None
    append_event: Callable[..., Any]
    is_owner: bool
    now: Callable[[], str]


@dataclass(frozen=True)
class ConsumptionResp:
    """엔진 ConsumptionResp — 칸 순서가 응답 바이트다."""

    session: str
    used: int
    contested: int
    supersedes: int
    unknown: int
    refused: int

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _transact(deps: Deps, work: Callable[[Any], Any]) -> Either[Any, Failed | StoreOff]:
    """연결 → 커서 → work → 커밋 한 번. psycopg 가 던진 것은 여기서 Failed 값으로 접힌다."""
    if deps.connect is None:
        return Err(StoreOff())
    try:
        with deps.connect() as conn, conn.cursor() as cur:
            return Ok(work(cur))
    except psycopg.Error as e:
        return Err(Failed(str(e)))


def _lists(cur: Any, basis: Basis, session_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    match basis:
        case Listed(used, contested):
            return used, contested
        case Handed(verdict):
            return _by_verdict(verdict, verdict_pg.handed_paths(cur, session_id))


def _by_verdict(verdict: Verdict, handed: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    match verdict:
        case Verdict.USED:
            return handed, ()
        case Verdict.CONTESTED:
            return (), handed


def _kept_pairs(cur: Any, req: Consumption, deps: Deps) -> tuple[tuple[tuple[str, str], ...], int]:
    """비오너의 supersedes 중 오너가 쓴 옛 노트를 겨눈 쌍은 버린다 — (남은 쌍, 버린 쌍 수)."""
    refused: list[str] = (
        [] if deps.is_owner else verdict_pg.owner_authored(cur, tuple(old for _, old in req.supersedes))
    )
    if refused:
        deps.append_event(_OWNER_COMPONENT, OWNER_REFUSED_EVENT, "warn", door=REFUSED_DOOR, targets=refused)
    kept = tuple(pair for pair in req.supersedes if pair[1] not in refused)
    return kept, len(req.supersedes) - len(kept)


def _consume(cur: Any, req: Consumption, deps: Deps) -> ConsumptionResp:
    kept, refused = _kept_pairs(cur, req, deps)
    used, contested = _lists(cur, req.basis, req.session_id)
    report = verdict_pg.record_consumption(
        cur, req.session_id, req.observed_at, verdict_pg.Verdicts(used, contested, kept, req.judge)
    )
    return ConsumptionResp(
        session=f"session:{req.session_id}",
        used=report.used,
        contested=report.contested,
        supersedes=report.supersedes,
        unknown=report.unknown,
        refused=refused,
    )


def consume(req: Consumption, deps: Deps) -> Either[dict[str, Any], Rejected | Failed | StoreOff]:
    """POST /consumption — judge=owner 는 오너 토큰이 있어야 한다(없으면 400, 쓰기 전)."""
    if req.judge == "owner" and not deps.is_owner:
        return Err(Rejected(OWNER_TOKEN_NEEDED))
    return map_ok(_transact(deps, lambda cur: _consume(cur, req, deps)), ConsumptionResp.payload)


def hand_over(req: Handover, deps: Deps) -> Either[dict[str, Any], Failed | StoreOff]:
    """POST /handover — session 노드 + 알려진 문서마다 handed 간선."""

    def work(cur: Any) -> dict[str, Any]:
        report = search_pg.write_handover(cur, req.session_id, req.observed_at, list(req.paths))
        return {"session": f"session:{req.session_id}", "handed": report.handed, "unknown": report.unknown}

    return _transact(deps, work)


def verdict(req: VerdictCall, deps: Deps) -> Either[dict[str, Any], Failed | StoreOff]:
    """MCP verdict — 건넨 문서 전부에 판정 하나, observed_at 은 지금, judge·supersedes 없음(refused 칸 없음)."""

    def work(cur: Any) -> dict[str, Any]:
        used, contested = _by_verdict(req.verdict, verdict_pg.handed_paths(cur, req.session_id))
        report = verdict_pg.record_consumption(
            cur, req.session_id, deps.now(), verdict_pg.Verdicts(used, contested, (), None)
        )
        return {
            "session": f"session:{req.session_id}",
            "used": report.used,
            "contested": report.contested,
            "supersedes": report.supersedes,
            "unknown": report.unknown,
        }

    return _transact(deps, work)
