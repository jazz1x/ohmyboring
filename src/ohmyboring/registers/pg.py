"""레지스터 읽기 질의 — drudge store.rs·ask.rs·serve.rs 의 해당 질의·렌더·인자 검사를 옮긴 것.

SQL 은 store.rs 를 그대로 따라 %(name)s 플레이스홀더로만 옮겼다 — 필터·순서·상수가
엔진과 다륾면 그림자(shadow.py)가 칸별 대조로 바로 잡아낸다. 쓰기 SQL 이 없는 읽기
전용 모듈 — 문의 그림자와 DOOR_REGISTER_READER=python 응답 길이 같이 쓴다.

인자 검사(register_limit 등)도 serve.rs 를 따라 400/-32602 의 문구까지 같게 해, 문이
직접 답을 낼 때 소비자가 보는 거절 모양이 엔진과 같다.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg

from ohmyboring.result import Either, Err, Ok, map_ok
from ohmyboring.search.pg import PgError, _canonical_confidence, _canonical_kind

#: ── 상수 (drudge store.rs·serve.rs·ask.rs 정본) ─────────────────────────────
REGISTER_LIMIT = 50
STALE_HORIZON_DAYS = 30
NOT_USER_MEMORY_RE = r"(^|/)(eval-|daily-brief-|weekly-brief-)[^/]*\.md$"
INFORMATIVE_VALUE_CHARS = 25
TAUTOLOGICAL_PREDICATES = (
    "^(incident|decision|status|state|상태|결정|next[-_ ]?(step|action)|action|다음 작업)$"
)
RECURRENCE_MAX_DISTANCE = 0.2
RECURRENCE_MIN_DAYS_APART = 3
RECURRENCES_DEFAULT_DAYS = 30
RECURRENCES_DEFAULT_LIMIT = 10
RECURRENCES_MAX_LIMIT = 50
CONTEXT_DEFAULT_MAX_ITEMS = 5
HTTP_CONTEXT_MAX_ITEMS = 20
MCP_CONTEXT_MAX_ITEMS = 50
STATUS_DOC_LIMIT = 15
STATUS_SINCE_HOURS = 720
STATUS_CLAIM_LIMIT = 10

EMPTY_DECISIONS = "No decisions recorded yet."
EMPTY_RISKS = "No risks, assumptions, or blockers recorded yet."
EMPTY_NEXT_ACTIONS = "No next actions or blockers recorded yet."
STALLED_EMPTY = "No stalled items older than {days} days."
STATUS_EMPTY = "No recent records or claims found for project '{project}'."

DECISION_KINDS = ("decision",)
RISK_KINDS = ("risk", "assumption", "blocked")
NEXT_ACTION_KINDS = ("next", "blocked")
FACT_KINDS = ("fact",)
GLOSSARY_KINDS = ("term",)
RECURRENCE_KINDS = ("risk", "blocked")

#: Postgres `~*` 와 같은 대소문자 무시 매치 — SQL 에 넘기는 패턴을 파이썬 타이 판정에도 쓴다.
_TAUTOLOGICAL_RE = re.compile(TAUTOLOGICAL_PREDICATES, re.IGNORECASE)

REGISTER_SURFACES = ("decisions", "risks", "next_actions", "stalled", "recurrences", "context", "status")
HTTP_PATHS = {
    "/decisions": "decisions",
    "/risks": "risks",
    "/next_actions": "next_actions",
    "/stalled": "stalled",
    "/recurrences": "recurrences",
    "/context": "context",
    "/status": "status",
}
MCP_TOOLS = {
    "decisions": "decisions",
    "risks": "risks",
    "next_actions": "next_actions",
    "stalled": "stalled",
    "recurrences": "recurrences",
    "context": "context",
    "project_status": "status",
}

#: ── SQL (store.rs 정본 — 최근접 문서 제외 레지스터 전부) ─────────────────────
_RECENT_REGISTER_ROWS_SQL = (
    "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence, c.valid_from, d.project,"
    " COUNT(*) OVER () AS total"
    " FROM claim c"
    " JOIN document d ON d.source_path = c.source_path"
    " WHERE c.superseded_at IS NULL"
    " AND (%(project)s::text IS NULL OR d.project = %(project)s)"
    " AND (%(kinds)s::text[] IS NULL OR c.kind = ANY(%(kinds)s))"
    " AND NOT (d.origin = ANY(%(exclude_origins)s))"
    " AND d.source_path !~ %(not_user_memory)s"
    " ORDER BY (CASE WHEN length(c.value) < %(informative)s THEN 1 ELSE 0 END)"
    " + (CASE WHEN c.predicate ~* %(tautological)s THEN 1 ELSE 0 END),"
    " c.valid_from DESC"
    " LIMIT %(limit)s"
)

_STALLED_REGISTER_ROWS_SQL = (
    "WITH ranked AS ("
    " SELECT c.subject, c.predicate, c.value, c.kind, c.confidence, c.valid_from, d.project,"
    " ROW_NUMBER() OVER (PARTITION BY c.source_path ORDER BY c.valid_from ASC) AS per_doc"
    " FROM claim c"
    " JOIN document d ON d.source_path = c.source_path"
    " WHERE c.superseded_at IS NULL"
    " AND c.valid_from < (NOW() - INTERVAL '1 day' * (%(older_than_days)s::bigint))"
    " AND c.valid_from >= (NOW() - INTERVAL '1 day' * (%(horizon)s::bigint))"
    " AND (%(project)s::text IS NULL OR d.project = %(project)s)"
    " AND (%(kinds)s::text[] IS NULL OR c.kind = ANY(%(kinds)s))"
    " AND NOT (d.origin = ANY(%(exclude_origins)s))"
    " AND d.source_path !~ %(not_user_memory)s"
    " )"
    " SELECT subject, predicate, value, kind, confidence, valid_from, project,"
    " COUNT(*) OVER () AS total"
    " FROM ranked"
    " WHERE per_doc = 1"
    " ORDER BY valid_from ASC"
    " LIMIT %(limit)s"
)

_RECURRENCES_SQL = (
    "SELECT a.source_path, a.subject, a.predicate, a.value, a.kind, a.valid_from,"
    " b.source_path, b.subject, b.predicate, b.value, b.kind, b.valid_from,"
    " (a.embedding <=> b.embedding)::float4 AS distance,"
    " (a.valid_from::date - b.valid_from::date)::bigint AS days_apart,"
    " (a.predicate ~* %(tautological)s) AS label_only"
    " FROM claim a"
    " JOIN claim b ON b.source_path <> a.source_path"
    " JOIN document da ON da.source_path = a.source_path"
    " JOIN document db ON db.source_path = b.source_path"
    " WHERE a.superseded_at IS NULL"
    " AND b.superseded_at IS NULL"
    " AND a.kind IN ('risk', 'blocked')"
    " AND b.kind IN ('risk', 'blocked')"
    " AND a.embedding IS NOT NULL"
    " AND b.embedding IS NOT NULL"
    " AND length(a.value) >= %(informative)s"
    " AND length(b.value) >= %(informative)s"
    " AND a.valid_from >= (NOW() - INTERVAL '1 day' * (%(days)s::bigint))"
    " AND (a.valid_from::date - b.valid_from::date) >= (%(min_days_apart)s::bigint)"
    " AND (a.embedding <=> b.embedding) <= %(max_distance)s::real"
    " AND (%(project)s::text IS NULL OR da.project = %(project)s)"
    " AND (%(project)s::text IS NULL OR db.project = %(project)s)"
    " ORDER BY distance ASC, a.valid_from DESC"
)

_RECENT_CLAIMS_SQL = (
    "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence FROM claim c"
    " JOIN document d ON d.source_path = c.source_path"
    " WHERE c.superseded_at IS NULL"
    " AND (%(project)s::text IS NULL OR d.project = %(project)s)"
    " AND (%(kinds)s::text[] IS NULL OR c.kind = ANY(%(kinds)s))"
    " AND NOT (d.origin = ANY(%(exclude_origins)s))"
    " AND d.source_path !~ %(not_user_memory)s"
    " ORDER BY (CASE WHEN length(c.value) < %(informative)s THEN 1 ELSE 0 END)"
    " + (CASE WHEN c.predicate ~* %(tautological)s THEN 1 ELSE 0 END),"
    " c.valid_from DESC"
    " LIMIT %(limit)s"
)

_STATUS_DOCS_SQL = (
    "SELECT d.source_path, d.project, d.tags,"
    " string_agg(c.content, E'\\n' ORDER BY c.chunk_idx) AS content"
    " FROM document d"
    " JOIN chunk c ON c.source_path = d.source_path"
    " WHERE NOT (d.origin = ANY(%(exclude_origins)s))"
    " AND d.updated_at >= now() - make_interval(hours => %(since_hours)s)"
    " AND (%(project)s::text IS NULL OR d.project = %(project)s)"
    " AND d.source_path !~ %(not_user_memory)s"
    " GROUP BY d.source_path, d.project, d.tags, d.updated_at"
    " ORDER BY d.updated_at DESC"
    " LIMIT %(limit)s"
)

_STATUS_CLAIMS_SQL = (
    "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence"
    " FROM claim c"
    " JOIN document d ON d.source_path = c.source_path"
    " WHERE c.superseded_at IS NULL AND c.embedding IS NOT NULL"
    " AND c.stale_at IS NULL"
    " AND NOT (d.origin = ANY(%(exclude_origins)s))"
    " AND (%(project)s::text IS NULL OR d.project = %(project)s)"
    " AND d.source_path !~ %(not_user_memory)s"
    " ORDER BY c.embedding <=> %(vec)s::vector"
    " LIMIT %(limit)s"
)


#: ── 행 모양 ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RegisterRow:
    """store.rs RegisterRow — kind·confidence 은 Claim::kind()/confidence()로 정규화.

    valid_from 의 기본은 에포치 — /context·/status 처럼 시각 칸을 안 내는 표면만 그대로 쓴다."""

    subject: str
    predicate: str
    value: str
    kind: str
    confidence: str
    valid_from: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))
    project: str = ""

    @property
    def node_id(self) -> str:
        return f"claim:{self.subject}:{self.predicate}"

    def as_item(self) -> dict[str, Any]:
        """MCP register_json items[] 칸 — 엔진 serde 필드 순서 그대로."""
        return {
            "node_id": self.node_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "kind": _canonical_kind(self.kind, self.value),
            "confidence": _canonical_confidence(self.confidence),
            "valid_from": rfc3339(self.valid_from),
            "project": self.project,
        }

    def as_context_item(self) -> dict[str, Any]:
        """ask.rs ContextItem — valid_from·node_id 없이 다섯 칸."""
        return {
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "kind": _canonical_kind(self.kind, self.value),
            "confidence": _canonical_confidence(self.confidence),
        }

    def rank_key(self) -> int:
        """ORDER BY 첫 단의 강등 랭크 — 타이 판정의 한 축 (길이 강등 + 술어 동어 반복)."""
        demoted = 1 if len(self.value) < INFORMATIVE_VALUE_CHARS else 0
        tautological = 1 if _regex_ci(TAUTOLOGICAL_PREDICATES, self.predicate) else 0
        return demoted + tautological


@dataclass(frozen=True)
class RegisterRows:
    rows: tuple[RegisterRow, ...]
    total_matching: int


@dataclass(frozen=True)
class ClaimRef:
    """store.rs ClaimRef — recurrence 한 쪽 끝. kind 은 원시 칸(정규화 없음)."""

    source_path: str
    subject: str
    predicate: str
    value: str
    kind: str
    valid_from: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "kind": self.kind,
            "valid_from": rfc3339(self.valid_from),
        }


@dataclass(frozen=True)
class Recurrence:
    newer: ClaimRef
    older: tuple[ClaimRef, ...]
    distance: float
    days_apart: int
    label_only: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "newer": self.newer.as_dict(),
            "older": [c.as_dict() for c in self.older],
            "distance": _f32_for_json(self.distance),
            "days_apart": self.days_apart,
            "label_only": self.label_only,
        }


@dataclass(frozen=True)
class StatusDoc:
    source_path: str
    project: str
    tags: list[str]
    content: str


@dataclass(frozen=True)
class StatusMaterial:
    """/status 의 결정론 재료 — 답 자체는 엔진은 LLM 생성, 파이썬 길은 digest 렌더."""

    docs: tuple[StatusDoc, ...]
    claims: tuple[RegisterRow, ...] = field(default_factory=tuple)


def _regex_ci(pattern: str, value: str) -> bool:
    return bool(_TAUTOLOGICAL_RE.search(value))


def rfc3339(dt: datetime) -> str:
    """chrono DateTime::to_rfc3339(AutoSi) — store.rs serialize_valid_from 의 출력 모양.

    timestamptz 는 마이크로초라 초 아래가 0 이면 생략, 밀리초 단위면 셋, 아니면 여섯 자리.
    """
    dt = dt.astimezone(UTC)
    micros = dt.microsecond
    if micros == 0:
        frac = ""
    elif micros % 1000 == 0:
        frac = f".{micros // 1000:03d}"
    else:
        frac = f".{micros:06d}"
    return f"{dt:%Y-%m-%dT%H:%M:%S}{frac}Z"


def _f32_shortest(value: float) -> str:
    """serde_json(ryu) 의 f32 최단 소수 출력 — 1..9 유효 자릿수를 f32 라운드트립으로 찾는다."""
    packed = struct.pack("!f", value)
    for prec in range(1, 10):
        candidate = format(value, f".{prec}g")
        try:
            if struct.pack("!f", float(candidate)) == packed:
                return candidate
        except (ValueError, OverflowError):
            continue
    return format(value, ".9g")


def _f32_for_json(value: float) -> float:
    """distance 를 엔진 JSON 의 숫자 모양(f32 최단 소수)에 가깝게 실수로 되돌린다."""
    return float(_f32_shortest(value))


def _exec(conn: psycopg.Connection, sql: str, params: dict[str, Any]) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _attempt(fn: Callable[[], Any]) -> Either[Any, PgError]:
    try:
        return Ok(fn())
    except psycopg.Error as e:
        return Err(PgError(str(e)))


@dataclass(frozen=True)
class RegisterQuery:
    """레지스터 본질문의 인자 한 벌 — stalled 만 older_than_days 를 더 쓴다."""

    limit: int
    project: str | None
    kinds: tuple[str, ...] | None
    exclude_origins: tuple[str, ...]
    older_than_days: int | None = None


def _register_params(query: RegisterQuery) -> dict[str, Any]:
    return {
        "limit": query.limit,
        "project": query.project,
        "kinds": list(query.kinds) if query.kinds is not None else None,
        "exclude_origins": list(query.exclude_origins),
        "not_user_memory": NOT_USER_MEMORY_RE,
        "informative": INFORMATIVE_VALUE_CHARS,
        "tautological": TAUTOLOGICAL_PREDICATES,
    }


def _to_register_row(row: tuple) -> RegisterRow:
    return RegisterRow(
        subject=row[0],
        predicate=row[1],
        value=row[2],
        kind=row[3],
        confidence=row[4],
        valid_from=row[5],
        project=row[6],
    )


def _register_rows_from(rows: list[tuple]) -> RegisterRows:
    total = rows[0][7] if rows else 0
    return RegisterRows(rows=tuple(_to_register_row(row) for row in rows), total_matching=int(total))


def recent_register_rows(conn: psycopg.Connection, query: RegisterQuery) -> Either[RegisterRows, PgError]:
    """store.rs:1728 recent_register_rows — 레지스터 다섯이 같이 쓰는 본질문."""
    params = _register_params(query)
    match _attempt(lambda: _exec(conn, _RECENT_REGISTER_ROWS_SQL, params)):
        case Err(e):
            return Err(e)
        case Ok(rows):
            return Ok(_register_rows_from(rows))


def stalled_register_rows(conn: psycopg.Connection, query: RegisterQuery) -> Either[RegisterRows, PgError]:
    """store.rs:1855 stalled_register_rows — 문서당 한 행, older_than_days ≤ age < 30d."""
    params = _register_params(query)
    params["older_than_days"] = query.older_than_days
    params["horizon"] = STALE_HORIZON_DAYS
    match _attempt(lambda: _exec(conn, _STALLED_REGISTER_ROWS_SQL, params)):
        case Err(e):
            return Err(e)
        case Ok(rows):
            return Ok(_register_rows_from(rows))


def recurrence_rows(
    conn: psycopg.Connection, days: int, project: str | None, limit: int
) -> Either[list[Recurrence], PgError]:
    """store.rs:1914 recurrences — 짝은 SQL 이, 그룹화(최신 걸 그룹 키·첫 짝 거리·older 시간순)는 러스트 따라 옮김."""
    params = {
        "days": days,
        "min_days_apart": RECURRENCE_MIN_DAYS_APART,
        "informative": INFORMATIVE_VALUE_CHARS,
        "max_distance": RECURRENCE_MAX_DISTANCE,
        "project": project,
        "tautological": TAUTOLOGICAL_PREDICATES,
    }
    match _attempt(lambda: _exec(conn, _RECURRENCES_SQL, params)):
        case Err(e):
            return Err(e)
        case Ok(rows):
            pass
    out: list[Recurrence] = []
    index: dict[tuple[str, str, str, datetime], int] = {}
    for row in rows:
        newer = ClaimRef(row[0], row[1], row[2], row[3], row[4], row[5])
        older = ClaimRef(row[6], row[7], row[8], row[9], row[10], row[11])
        key = (newer.source_path, newer.subject, newer.predicate, newer.valid_from)
        if key in index:
            out[index[key]] = Recurrence(
                newer=out[index[key]].newer,
                older=out[index[key]].older + (older,),
                distance=out[index[key]].distance,
                days_apart=out[index[key]].days_apart,
                label_only=out[index[key]].label_only,
            )
        else:
            index[key] = len(out)
            out.append(
                Recurrence(
                    newer=newer,
                    older=(older,),
                    distance=float(row[12]),
                    days_apart=int(row[13]),
                    label_only=bool(row[14]),
                )
            )
    grouped = [
        Recurrence(
            r.newer,
            tuple(sorted(r.older, key=lambda c: c.valid_from)),
            r.distance,
            r.days_apart,
            r.label_only,
        )
        for r in out
    ]
    if limit >= 0:
        grouped = grouped[:limit]
    return Ok(grouped)


def recent_claims(
    conn: psycopg.Connection,
    limit: int,
    project: str | None,
    kinds: tuple[str, ...] | None,
    exclude_origins: Iterable[str],
) -> Either[tuple[RegisterRow, ...], PgError]:
    """store.rs:1602 recent_claims — /context 다섯 칸의 재료 (유효시각 없이 다섯 칸만 낸다)."""
    query = RegisterQuery(limit, project, kinds, tuple(exclude_origins))
    params = _register_params(query)
    match _attempt(lambda: _exec(conn, _RECENT_CLAIMS_SQL, params)):
        case Err(e):
            return Err(e)
        case Ok(rows):
            pass
    return Ok(
        tuple(
            RegisterRow(
                subject=row[0],
                predicate=row[1],
                value=row[2],
                kind=row[3],
                confidence=row[4],
            )
            for row in rows
        )
    )


def status_docs(
    conn: psycopg.Connection, project: str, exclude_origins: Iterable[str] = ()
) -> Either[tuple[StatusDoc, ...], PgError]:
    """store.rs:1040 recent_docs 의 since_hours=Some(720) 갈래 — /status 의 근 30일 문서."""
    params = {
        "exclude_origins": list(exclude_origins),
        "since_hours": STATUS_SINCE_HOURS,
        "project": project,
        "not_user_memory": NOT_USER_MEMORY_RE,
        "limit": STATUS_DOC_LIMIT,
    }
    match _attempt(lambda: _exec(conn, _STATUS_DOCS_SQL, params)):
        case Err(e):
            return Err(e)
        case Ok(rows):
            pass
    return Ok(
        tuple(
            StatusDoc(
                source_path=row[0],
                project=row[1],
                tags=list(row[2] or []),
                content=row[3] or "",
            )
            for row in rows
        )
    )


def status_claims(
    conn: psycopg.Connection, project: str, vec: list[float]
) -> Either[tuple[RegisterRow, ...], PgError]:
    """store.rs:2036 current_claims 의 /status 갈래 — 프로젝트명 임베딩의 최근접 현행 claim 열 개."""
    params = {
        "vec": _vector_literal(vec),
        "limit": STATUS_CLAIM_LIMIT,
        "exclude_origins": [],
        "project": project,
        "not_user_memory": NOT_USER_MEMORY_RE,
    }
    match _attempt(lambda: _exec(conn, _STATUS_CLAIMS_SQL, params)):
        case Err(e):
            return Err(e)
        case Ok(rows):
            pass
    return Ok(
        tuple(
            RegisterRow(
                subject=row[0],
                predicate=row[1],
                value=row[2],
                kind=row[3],
                confidence=row[4],
            )
            for row in rows
        )
    )


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


#: ── 렌더 (ask.rs render_register·register_sources·register_json) ────────────
def render_register(rows: tuple[RegisterRow, ...], limit_applied: bool, total_matching: int) -> str:
    """ask.rs:792 render_register — answer 텍스트는 data-parity.py 가 파싱하는 계약 칸."""
    lines = [
        f"Showing {len(rows)} of {total_matching} matching claims (limit_applied={str(limit_applied).lower()})."
    ]
    for row in rows:
        kind = _canonical_kind(row.kind, row.value)
        confidence = _canonical_confidence(row.confidence)
        lines.append(f"* {row.subject} — {row.predicate}: {row.value} (kind={kind}, confidence={confidence})")
    return "\n".join(lines)


def register_sources(rows: Iterable[RegisterRow]) -> list[str]:
    """ask.rs:813 register_sources — 정렬·중복 제거한 subject 목록 (경로가 아니다)."""
    return sorted({row.subject for row in rows})


@dataclass(frozen=True)
class RegisterOut:
    """ask.rs RegisterOut — MCP register_json 다섯 칸 + HTTP 의 answer·sources 둘."""

    answer: str
    sources: list[str]
    items: tuple[RegisterRow, ...]
    limit_applied: bool
    total_matching: int

    def http_payload(self) -> dict[str, Any]:
        return {"answer": self.answer, "sources": self.sources}

    def mcp_payload(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "sources": self.sources,
            "items": [row.as_item() for row in self.items],
            "limit_applied": self.limit_applied,
            "total_matching": self.total_matching,
        }


def _register_out(rows: RegisterRows, empty_answer: str) -> RegisterOut:
    if not rows.rows:
        return RegisterOut(empty_answer, [], (), False, 0)
    limit_applied = len(rows.rows) < rows.total_matching
    return RegisterOut(
        answer=render_register(rows.rows, limit_applied, rows.total_matching),
        sources=register_sources(rows.rows),
        items=rows.rows,
        limit_applied=limit_applied,
        total_matching=rows.total_matching,
    )


def _simple_builder(
    kinds: tuple[str, ...], empty_answer: str
) -> Callable[[psycopg.Connection, str | None, Iterable[str], int], Either[RegisterOut, PgError]]:
    """decisions·risks·next_actions 가 같이 쓰는 recent_register_rows 한 바퀴 — 종류·빈 문구만 다르다."""

    def build(
        conn: psycopg.Connection, project: str | None, exclude_origins: Iterable[str], limit: int
    ) -> Either[RegisterOut, PgError]:
        query = RegisterQuery(limit, project, kinds, tuple(exclude_origins))
        match recent_register_rows(conn, query):
            case Err(e):
                return Err(e)
            case Ok(rows):
                return Ok(_register_out(rows, empty_answer))

    return build


decision_register = _simple_builder(DECISION_KINDS, EMPTY_DECISIONS)
risk_register = _simple_builder(RISK_KINDS, EMPTY_RISKS)
next_action_register = _simple_builder(NEXT_ACTION_KINDS, EMPTY_NEXT_ACTIONS)


def stalled_register(
    conn: psycopg.Connection,
    project: str | None,
    exclude_origins: Iterable[str],
    older_than_days: int,
    limit: int,
) -> Either[RegisterOut, PgError]:
    query = RegisterQuery(limit, project, NEXT_ACTION_KINDS, tuple(exclude_origins), older_than_days)
    match stalled_register_rows(conn, query):
        case Err(e):
            return Err(e)
        case Ok(rows):
            return Ok(_register_out(rows, STALLED_EMPTY.format(days=older_than_days)))


def recurrences_payload(
    conn: psycopg.Connection, days: int, project: str | None, limit: int
) -> Either[dict, PgError]:
    """serve.rs RecurrencesResp — rows·days·max_distance·min_days_apart 네 칸."""
    match recurrence_rows(conn, days, project, limit):
        case Err(e):
            return Err(e)
        case Ok(rows):
            pass
    return Ok(
        {
            "rows": [row.as_dict() for row in rows],
            "days": days,
            "max_distance": _f32_for_json(RECURRENCE_MAX_DISTANCE),
            "min_days_apart": RECURRENCE_MIN_DAYS_APART,
        }
    )


def context_payload(
    conn: psycopg.Connection,
    project: str | None,
    exclude_origins: Iterable[str],
    max_items: int,
    lang: str,
) -> Either[dict, PgError]:
    """ask.rs ContextCard — decisions·risks·facts·glossary·next_actions·language 여섯 칸.

    한 칸이라도 못 읽으면 Err — 엔진도 다섯 질의를 다 성공해야 답을 낸다."""
    sections: dict[str, tuple[RegisterRow, ...]] = {}
    for name, kinds in (
        ("decisions", DECISION_KINDS),
        ("risks", RISK_KINDS),
        ("facts", FACT_KINDS),
        ("glossary", GLOSSARY_KINDS),
        ("next_actions", NEXT_ACTION_KINDS),
    ):
        match recent_claims(conn, max_items, project, kinds, exclude_origins):
            case Err(e):
                return Err(e)
            case Ok(rows):
                sections[name] = rows
    return Ok(
        {
            "decisions": [row.as_context_item() for row in sections["decisions"]],
            "risks": [row.as_context_item() for row in sections["risks"]],
            "facts": [row.as_context_item() for row in sections["facts"]],
            "glossary": [row.as_context_item() for row in sections["glossary"]],
            "next_actions": [row.as_context_item() for row in sections["next_actions"]],
            "language": lang,
        }
    )


def _defang(text: str) -> str:
    """ask.rs defang — 줄 머리의 '#' 를 빈 칸 하나로 묶어 마크다운 주입을 늦춘다."""
    out: list[str] = []
    for line in text.splitlines():
        out.append(f" {line}" if line.startswith("#") else line)
    return "\n".join(out) + ("\n" if text else "")


def render_status_answer(material: StatusMaterial, lang: str) -> str:
    """파이썬 /status 길의 답 — LLM 이 없으니 결정론 digest 를 낸다 (칸은 엔진과 같다).

    비었을 때의 문구는 엔진과 바이트 같게 해 빈 경로가 스위치를 타도 안 바뀐다. 비어 있지
    않으면 문서·claim 재료를 그대로 편집 없이 배열한 것 — 생성 답과 다름은 (나) 계열로
    그림자가 계속 잰다."""
    if not material.docs and not material.claims:
        return ""
    parts: list[str] = ["# Recent work records (last 30 days)", ""]
    for i, doc in enumerate(material.docs):
        parts.append(f"## [{i}] {doc.source_path}")
        parts.append(_defang(doc.content).rstrip("\n"))
        parts.append("")
    if material.claims:
        parts.append("# Current project facts")
        parts.append("")
        for claim in material.claims:
            kind = _canonical_kind(claim.kind, claim.value)
            confidence = _canonical_confidence(claim.confidence)
            parts.append(f"- [{kind}|{confidence}] {claim.subject} {claim.predicate} {claim.value}")
        parts.append("")
    if lang == "ko":
        parts.append("(한국어 요약은 생성 모델 경로에서만 나온다 — 이 답은 읽기 재료 그대로의 digest 이다.)")
    return "\n".join(parts).rstrip("\n")


def status_payload(material: StatusMaterial, project: str, lang: str) -> dict[str, Any]:
    """serve.rs AskResp 모양 — answer·sources 두 칸. sources 는 문서 경로의 근접 순."""
    if not material.docs and not material.claims:
        return {"answer": STATUS_EMPTY.format(project=project), "sources": []}
    return {
        "answer": render_status_answer(material, lang),
        "sources": [doc.source_path for doc in material.docs],
    }


#: ── 인자 검사·정규화 (serve.rs·mcp.rs — 문구·강제·기본값까지 수송 단별로 같게) ──
@dataclass(frozen=True)
class Rejected:
    """인자 검사 거절 — message 는 엔진 400/-32602 본문과 바이트 같아야 한다."""

    message: str


_UNPROCESSABLE = Rejected("unprocessable entity")


def parse_register_limit(raw: Any) -> Either[int, Rejected]:
    """serve.rs:1063 register_limit — 양 수송 단 같음: 정수 1..=50 만, 조용한 클램프 없음."""
    if raw is None:
        return Ok(REGISTER_LIMIT)
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= REGISTER_LIMIT:
        return Err(Rejected(f"limit must be an integer in 1..={REGISTER_LIMIT}"))
    return Ok(raw)


def _http_optional_str(body: dict, key: str) -> Either[str | None, Rejected]:
    """HTTP 의 Option<String> — 있는데 문자열 아니면 422 계열."""
    if key not in body or body[key] is None:
        return Ok(None)
    value = body[key]
    if not isinstance(value, str):
        return Err(_UNPROCESSABLE)
    return Ok(value)


def _http_u32(body: dict, key: str, default: int) -> Either[int, Rejected]:
    """HTTP 의 Option<u32>/usize — 부재면 기본값, 정수 아니면 422 계열."""
    if key not in body or body[key] is None:
        return Ok(default)
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 0xFFFF_FFFF:
        return Err(_UNPROCESSABLE)
    return Ok(value)


def _mcp_u32(args: dict, key: str, default: int, what: str) -> Either[int, Rejected]:
    """mcp.rs 의 as_u64→u32 — 쓰레기 값(None·문자·실수·음수)은 기본값, u32 넘으면 -32602."""
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return Ok(default)
    if value > 0xFFFF_FFFF:
        return Err(Rejected(f"{what} is too large"))
    return Ok(value)


def _mcp_project(args: dict) -> str | None:
    """mcp.rs 다섯 레지스터 — 문자열이면 trim, 빈 문자열은 Some("") 그대로."""
    value = args.get("project")
    return value.strip() if isinstance(value, str) else None


def _http_register_args(body: dict, *, stalled: bool) -> Either[dict, Rejected]:
    match _http_optional_str(body, "project"):
        case Err(e):
            return Err(e)
        case Ok(project):
            pass
    out: dict[str, Any] = {"project": project}
    if stalled:
        match _http_u32(body, "older_than_days", 7):
            case Err(e):
                return Err(e)
            case Ok(days):
                out["older_than_days"] = days
    return map_ok(parse_register_limit(body.get("limit")), lambda limit: {**out, "limit": limit})


def _http_recurrences_args(body: dict) -> Either[dict, Rejected]:
    match _http_optional_str(body, "project"):
        case Err(e):
            return Err(e)
        case Ok(project):
            pass
    match _http_u32(body, "days", RECURRENCES_DEFAULT_DAYS):
        case Err(e):
            return Err(e)
        case Ok(days):
            pass
    match _http_u32(body, "limit", RECURRENCES_DEFAULT_LIMIT):
        case Err(e):
            return Err(e)
        case Ok(limit):
            pass
    return Ok({"project": project, "days": recurrences_days(days), "limit": recurrences_limit(limit)})


def _http_context_args(body: dict) -> Either[dict, Rejected]:
    match _http_optional_str(body, "project"):
        case Err(e):
            return Err(e)
        case Ok(project):
            pass
    match _http_u32(body, "max_items", CONTEXT_DEFAULT_MAX_ITEMS):
        case Err(e):
            return Err(e)
        case Ok(max_items):
            pass
    raw_origins = body.get("exclude_origins", [])
    if not isinstance(raw_origins, list) or not all(isinstance(o, str) for o in raw_origins):
        return Err(_UNPROCESSABLE)
    return Ok(
        {
            "project": project,
            "max_items": min(max(max_items, 1), HTTP_CONTEXT_MAX_ITEMS),
            "exclude_origins": list(raw_origins),
        }
    )


def _http_status_args(body: dict) -> Either[dict, Rejected]:
    project = body.get("project")
    if not isinstance(project, str):
        return Err(_UNPROCESSABLE)
    return Ok({"project": project})


_HTTP_ARG_PARSERS = {
    "decisions": lambda body: _http_register_args(body, stalled=False),
    "risks": lambda body: _http_register_args(body, stalled=False),
    "next_actions": lambda body: _http_register_args(body, stalled=False),
    "stalled": lambda body: _http_register_args(body, stalled=True),
    "recurrences": _http_recurrences_args,
    "context": _http_context_args,
    "status": _http_status_args,
}


def http_args(surface: str, body: Any) -> Either[dict, Rejected]:
    """HTTP 본문(JSON 객체)을 정규 인자로 — serde 422 계열은 _UNPROCESSABLE."""
    if not isinstance(body, dict):
        return Err(_UNPROCESSABLE)
    parser = _HTTP_ARG_PARSERS.get(surface)
    if parser is None:
        return Err(_UNPROCESSABLE)
    return parser(body)


def _mcp_register_args(args: dict, *, stalled: bool) -> Either[dict, Rejected]:
    out: dict[str, Any] = {"project": _mcp_project(args)}
    if stalled:
        match _mcp_u32(args, "older_than_days", 7, "older_than_days"):
            case Err(e):
                return Err(e)
            case Ok(days):
                out["older_than_days"] = days
    return map_ok(parse_register_limit(args.get("limit")), lambda limit: {**out, "limit": limit})


def _mcp_recurrences_args(args: dict) -> Either[dict, Rejected]:
    match _mcp_u32(args, "days", RECURRENCES_DEFAULT_DAYS, "days"):
        case Err(e):
            return Err(e)
        case Ok(days):
            pass
    match _mcp_u32(args, "limit", RECURRENCES_DEFAULT_LIMIT, "limit"):
        case Err(e):
            return Err(e)
        case Ok(limit):
            pass
    return Ok(
        {
            "project": _mcp_project(args),
            "days": recurrences_days(days),
            "limit": recurrences_limit(limit),
        }
    )


def _mcp_context_args(args: dict) -> Either[dict, Rejected]:
    raw = args.get("max_items")
    max_items = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None
    value = MCP_CONTEXT_MAX_ITEMS if max_items is None else max_items
    project = args.get("project")
    return Ok(
        {
            "project": project.strip() if isinstance(project, str) and project.strip() else None,
            "max_items": min(max(value, 1), MCP_CONTEXT_MAX_ITEMS),
            "exclude_origins": [],
        }
    )


def _mcp_status_args(args: dict) -> Either[dict, Rejected]:
    project = args.get("project")
    if not isinstance(project, str) or not project.strip():
        return Err(Rejected("missing argument: project"))
    return Ok({"project": project.strip()})


_MCP_ARG_PARSERS = {
    "decisions": lambda args: _mcp_register_args(args, stalled=False),
    "risks": lambda args: _mcp_register_args(args, stalled=False),
    "next_actions": lambda args: _mcp_register_args(args, stalled=False),
    "stalled": lambda args: _mcp_register_args(args, stalled=True),
    "recurrences": _mcp_recurrences_args,
    "context": _mcp_context_args,
    "status": _mcp_status_args,
}


def mcp_args(surface: str, arguments: Any) -> Either[dict, Rejected]:
    """MCP arguments 를 정규 인자로 — mcp.rs 의 as_str/as_u64 강제 규칙 그대로."""
    parser = _MCP_ARG_PARSERS.get(surface)
    if parser is None:
        return Err(_UNPROCESSABLE)
    return parser(arguments if isinstance(arguments, dict) else {})


def recurrences_days(days: int) -> int:
    return RECURRENCES_DEFAULT_DAYS if days < 0 else days


def recurrences_limit(limit: int) -> int:
    """serve.rs:1054 recurrences_limit — 기본 10, 조용히 1..=50 클램프."""
    if limit < 0:
        return RECURRENCES_DEFAULT_LIMIT
    return min(max(limit, 1), RECURRENCES_MAX_LIMIT)


@dataclass(frozen=True)
class AnswerCtx:
    """문이 싣는 응답 맥락 — 원천 배제 정책·노트 언어·임베딩·수송 단 한 벌."""

    policy_origins: tuple[str, ...]
    lang: str
    embed: Callable[[str], Either[list[float], str]]
    transport: str


_SIMPLE_BUILDERS = {
    "decisions": decision_register,
    "risks": risk_register,
    "next_actions": next_action_register,
}


def _answer_register(conn: psycopg.Connection, surface: str, args: dict, ctx: AnswerCtx) -> Either[dict, str]:
    if surface == "stalled":
        result = stalled_register(
            conn, args["project"], ctx.policy_origins, args["older_than_days"], args["limit"]
        )
    else:
        result = _SIMPLE_BUILDERS[surface](conn, args["project"], ctx.policy_origins, args["limit"])
    match result:
        case Err(e):
            return Err(str(e))
        case Ok(out):
            return Ok(out.mcp_payload() if ctx.transport == "mcp" else out.http_payload())


def _answer_payload(conn: psycopg.Connection, surface: str, args: dict, ctx: AnswerCtx) -> Either[dict, str]:
    """recurrences·context — payload 가 dict 그대로인 두 표면."""
    if surface == "recurrences":
        result = recurrences_payload(conn, args["days"], args["project"], args["limit"])
    else:
        result = context_payload(conn, args["project"], args["exclude_origins"], args["max_items"], ctx.lang)
    match result:
        case Err(e):
            return Err(str(e))
        case Ok(payload):
            return Ok(payload)


def _answer_status(conn: psycopg.Connection, args: dict, ctx: AnswerCtx) -> Either[dict, str]:
    match status_docs(conn, args["project"]):
        case Err(e):
            return Err(str(e))
        case Ok(docs):
            pass
    match ctx.embed(args["project"]):
        case Err(failure):
            return Err(f"embed: {failure}")
        case Ok(vec):
            pass
    match status_claims(conn, args["project"], vec):
        case Err(e):
            return Err(str(e))
        case Ok(claims):
            pass
    return Ok(status_payload(StatusMaterial(docs, claims), args["project"], ctx.lang))


_ANSWER_HANDLERS = {
    "decisions": _answer_register,
    "risks": _answer_register,
    "next_actions": _answer_register,
    "stalled": _answer_register,
    "recurrences": _answer_payload,
    "context": _answer_payload,
    "status": _answer_status,
}


def answer(conn: psycopg.Connection, surface: str, args: dict, ctx: AnswerCtx) -> Either[dict, str]:
    """정규 인자로 표면을 계산해 엔진 모양 payload 를 낸다 — 스위치 켬 응답 길과 그림자가 같이 쓴다.

    실패는 message 값으로 (호출자가 400/-32602·사건 reason 에 입힌다). /status 는 claim 을
    얻으려 프로젝트명을 임베딩한다 — 엔진이 매 요청 embed 를 하는 것과 같다."""
    handler = _ANSWER_HANDLERS.get(surface)
    if handler is None:
        return Err(f"unknown surface: {surface}")
    return handler(conn, surface, args, ctx)
