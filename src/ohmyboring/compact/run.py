"""하루 1회 DB 정리 — drudge store.rs compact(:2702-2802) 의 파이썬 이식, 단계와 SQL 은 그대로.

순서: ① VACUUM ANALYZE 7개 표 ② 표마다 잘못 남은 _ccnew 색인을 regclass 이름으로 지우고
REINDEX TABLE CONCURRENTLY ③ query_log 90일 지나면 삭제 ④ gc_orphans(sync.run 의 것을 부른다)
⑤ supersedes 간선마다 옛 노트의 claim 봉인. 사건 compact_run 한 줄(단계별 ms·개수·status).

VACUUM·REINDEX CONCURRENTLY 는 트랜잭션 안에서 못 돈다 — 부르는 쪽이 autocommit 연결을 넘긴다.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any

from ohmyboring.remember.writer import _RETIRED_NOTE
from ohmyboring.result import Either, Err, Ok
from ohmyboring.sync.run import GcStats, gc_apply, gc_preview
from ohmyboring.sync.schedule import RUN_LOCK

COMPONENT = "door-compact"
EVENT = "compact_run"

TABLES = ("document", "chunk", "node", "edge", "claim", "query_log", "event_log")

#: store.rs:2753-2758 — TOAST 색인은 pg_toast 스키마라 relname 으로는 못 찾고 DROP 도 못 한다.
#: regclass 가 스키마 한정자를 알아서 붙인다(%% 는 psycopg 의 % 이스케이프).
_LEFTOVERS_SQL = (
    "SELECT c.oid::regclass::text FROM pg_class c"
    " JOIN pg_index i ON i.indexrelid = c.oid"
    " JOIN pg_class t ON t.oid = i.indrelid"
    " JOIN pg_class owner ON owner.oid = t.oid OR owner.reltoastrelid = t.oid"
    " WHERE NOT i.indisvalid AND owner.relname = %(table)s"
    " AND c.relname LIKE '%%\\_ccnew%%'"
)
_PRUNE_SQL = "DELETE FROM query_log WHERE created_at < now() - interval '90 days';"
_PRUNE_COUNT_SQL = "SELECT count(*) FROM query_log WHERE created_at < now() - interval '90 days';"

#: store.rs:2904-2931 seal_superseded_claims 두 문장. 옛 노트가 은퇴했을 때만 봉인하고, 건드린 fact 슬롯은
#: 남은 살아 있는 행 중 가장 최신에게 넘긴다.
_SUPERSEDES_EDGES_SQL = "SELECT src, dst FROM edge WHERE kind = 'supersedes';"
_SEAL_SQL = (
    "UPDATE claim c SET superseded_at = COALESCE("
    " (SELECT max(valid_from) FROM claim WHERE source_path = %(newer)s), now())"
    f" WHERE c.source_path = %(older)s AND c.superseded_at IS NULL AND {_RETIRED_NOTE};"
)
_PROMOTE_SQL = (
    "UPDATE claim c SET superseded_at = NULL"
    " FROM (SELECT c.subject, c.predicate, max(c.valid_from) AS mx FROM claim c"
    "       WHERE (c.subject, c.predicate) IN (SELECT subject, predicate FROM claim"
    "                                          WHERE source_path = %(older)s AND kind = 'fact')"
    f"        AND NOT {_RETIRED_NOTE}"
    "       GROUP BY c.subject, c.predicate) m"
    " WHERE c.kind = 'fact' AND c.superseded_at IS NOT NULL"
    "   AND c.subject = m.subject AND c.predicate = m.predicate"
    "   AND c.valid_from = m.mx;"
)
_SEAL_CANDIDATES_SQL = f"SELECT count(*) FROM claim c WHERE c.superseded_at IS NULL AND {_RETIRED_NOTE};"
_DOC_PREFIX = "doc:"


@dataclass
class CompactReport:
    vacuum_ms: int = 0
    reindex_ms: int = 0
    prune_ms: int = 0
    gc_ms: int = 0
    seal_ms: int = 0
    prune_query_log: int = 0
    dropped_invalid_indexes: int = 0
    gc: GcStats = field(default_factory=GcStats)
    sealed_superseded_claims: int = 0
    total_ms: int = 0

    def event_fields(self) -> dict[str, Any]:
        return {
            "vacuum_ms": self.vacuum_ms,
            "reindex_ms": self.reindex_ms,
            "prune_ms": self.prune_ms,
            "gc_ms": self.gc_ms,
            "seal_ms": self.seal_ms,
            "total_ms": self.total_ms,
            "prune_query_log": self.prune_query_log,
            "dropped_invalid_indexes": self.dropped_invalid_indexes,
            "gc_tool": self.gc.tool,
            "gc_concept": self.gc.concept,
            "gc_claim_nodes": self.gc.claim_nodes,
            "gc_claim_edges": self.gc.claim_edges,
            "sealed_superseded_claims": self.sealed_superseded_claims,
        }


def _ms(since: float) -> int:
    return int((time.monotonic() - since) * 1000)


def vacuum_all(cur: Any) -> None:
    for table in TABLES:
        cur.execute(f"VACUUM ANALYZE {table};")


def drop_invalid_indexes(cur: Any, table: str) -> int:
    cur.execute(_LEFTOVERS_SQL, {"table": table})
    names = [row[0] for row in cur.fetchall()]
    for name in names:
        cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name};")
    return len(names)


def reindex_all(cur: Any) -> int:
    dropped = 0
    for table in TABLES:
        dropped += drop_invalid_indexes(cur, table)
        cur.execute(f"REINDEX TABLE CONCURRENTLY {table};")
    return dropped


def prune_query_log(cur: Any) -> int:
    cur.execute(_PRUNE_SQL)
    return cur.rowcount


def seal_superseded_claims(cur: Any, newer: str, older: str) -> int:
    cur.execute(_SEAL_SQL, {"newer": newer, "older": older})
    sealed = cur.rowcount
    cur.execute(_PROMOTE_SQL, {"older": older})
    return sealed


def seal_claims_under_supersedes_edges(cur: Any) -> int:
    cur.execute(_SUPERSEDES_EDGES_SQL)
    edges = cur.fetchall()
    total = 0
    for src, dst in edges:
        if src.startswith(_DOC_PREFIX) and dst.startswith(_DOC_PREFIX):
            total += seal_superseded_claims(cur, src[len(_DOC_PREFIX) :], dst[len(_DOC_PREFIX) :])
    return total


def compact(cur: Any) -> CompactReport:
    report = CompactReport()
    started = time.monotonic()

    t0 = time.monotonic()
    vacuum_all(cur)
    report.vacuum_ms = _ms(t0)

    t0 = time.monotonic()
    report.dropped_invalid_indexes = reindex_all(cur)
    report.reindex_ms = _ms(t0)

    t0 = time.monotonic()
    report.prune_query_log = prune_query_log(cur)
    report.prune_ms = _ms(t0)

    t0 = time.monotonic()
    report.gc = gc_apply(cur)
    report.gc_ms = _ms(t0)

    t0 = time.monotonic()
    report.sealed_superseded_claims = seal_claims_under_supersedes_edges(cur)
    report.seal_ms = _ms(t0)

    report.total_ms = _ms(started)
    return report


@dataclass(frozen=True)
class Preview:
    prune_query_log: int
    invalid_indexes: int
    gc: GcStats
    seal_candidates: int


def preview(cur: Any) -> Preview:
    """쓰기 0 — 바뀔 개수만. 봉인 후보는 은퇴한 노트의 현재 claim 수(간선이 겹치면 실제와 어긋날 수 있는 추정)."""
    cur.execute(_PRUNE_COUNT_SQL)
    pruned = cur.fetchone()[0]
    invalid = 0
    for table in TABLES:
        cur.execute(_LEFTOVERS_SQL, {"table": table})
        invalid += len(cur.fetchall())
    gc = gc_preview(cur)
    cur.execute(_SEAL_CANDIDATES_SQL)
    return Preview(pruned, invalid, gc, cur.fetchone()[0])


def preview_json(p: Preview, session_read_only: str) -> dict[str, Any]:
    return {
        "mode": "shadow",
        "session_read_only": session_read_only,
        "prune_query_log": p.prune_query_log,
        "invalid_ccnew_indexes": p.invalid_indexes,
        "gc": {"tool": p.gc.tool, "concept": p.gc.concept, "claim_nodes": p.gc.claim_nodes},
        "seal_candidates": p.seal_candidates,
    }


@dataclass(frozen=True)
class CompactDeps:
    """connect 는 autocommit 연결 — VACUUM·REINDEX CONCURRENTLY 가 트랜잭션 밖에서 돈다."""

    connect: Callable[[], AbstractContextManager[Any]]
    append_event: Callable[..., bool]


def run_compact(deps: CompactDeps) -> CompactReport:
    """한 회차 — sync 스레드와 같은 잠금 아래서. 고장은 그대로 올라간다(부르는 쪽이 사건에 남긴다)."""
    with RUN_LOCK, deps.connect() as conn, conn.cursor() as cur:
        report = compact(cur)
    deps.append_event(COMPONENT, EVENT, "ok", **report.event_fields())
    return report


def run_once(deps: CompactDeps) -> Either[CompactReport, str]:
    """회차 경계 — 어떤 예외든 오류 사건과 stderr 한 줄로 남기고 값(Err)으로 돌려준다. 스레드는 무시하고
    다음 회차를 기다리고, 1회 진입점은 종료 코드로 바꾼다."""
    try:
        return Ok(run_compact(deps))
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        print(f"[door-compact] run failed: {reason}", file=sys.stderr, flush=True)
        deps.append_event(COMPONENT, EVENT, "error", reason=reason)
        return Err(reason)
