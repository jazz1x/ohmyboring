"""위생 집계 — drudge audit.rs:74 stats 의 파이썬 짝. 읽기만 한다(SELECT 뿐, 로그 문구는 옮기지 않음).

값은 sync_run 사건 칸으로 간다(`audit_` 접두). by_project 는 칸이 너무 길어 싣지 않는다.
SQL 은 store.rs 의 all_meta·graph_stats·semantic_stats·claim_era_counts·claim_stale_count 와 같은 집계.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ohmyboring.result import Either, Err, Ok
from ohmyboring.store import surreal
from ohmyboring.store.surreal import SurrealClient, SurrealError

#: store.rs:394 INFORMATIVE_VALUE_CHARS · :402 TAUTOLOGICAL_PREDICATES.
INFORMATIVE_VALUE_CHARS = 25
TAUTOLOGICAL_PREDICATES = (
    "^(incident|decision|status|state|상태|결정|next[-_ ]?(step|action)|action|다음 작업)$"
)

_CHUNKS_SQL = "SELECT count(*), count(DISTINCT source_path) FROM chunk;"
_BY_ORIGIN_SQL = "SELECT origin, count(*) FROM chunk GROUP BY origin;"
_BY_KIND_SQL = "SELECT kind, count(*) FROM chunk GROUP BY kind;"
_MISSING_PROJECT_SQL = "SELECT count(*) FROM chunk WHERE project = '';"
_DOCUMENTS_SQL = "SELECT count(*) FROM document;"
_EDGES_SQL = "SELECT count(*) FROM edge;"
_CLAIMS_WITH_DOC_SQL = "SELECT count(DISTINCT dst) FROM edge WHERE kind = 'claims';"
_NODE_KIND_SQL = "SELECT count(*) FROM node WHERE kind = %(kind)s;"
_EDGE_KIND_SQL = "SELECT count(*) FROM edge WHERE kind = %(kind)s;"
_ERA_SQL = (
    "SELECT count(*) FILTER (WHERE era = 'anchored'),"
    " count(*) FILTER (WHERE era = 'unanchored'),"
    " count(*) FILTER (WHERE era = 'pre-anchor'),"
    " count(*) FILTER (WHERE length(value) < %(chars)s OR predicate ~* %(tautology)s)"
    " FROM claim WHERE superseded_at IS NULL;"
)
_STALE_SQL = "SELECT count(*) FROM claim WHERE stale_at IS NOT NULL;"

_NODE_KINDS = ("project", "topic", "claim", "decision", "risk", "tool", "concept")
_EDGE_KINDS = ("uses", "about")


@dataclass(frozen=True)
class AuditStats:
    total_chunks: int
    total_files: int
    by_origin: dict[str, int]
    by_kind: dict[str, int]
    company_contamination: int
    company_allowed: bool
    missing_origin: int
    missing_project: int
    clean: bool
    graph_documents: int
    graph_edges: int
    graph_claims_with_doc: int
    nodes: dict[str, int]
    edges: dict[str, int]
    claims_anchored: int
    claims_unanchored: int
    claims_pre_anchor: int
    claims_label_only: int
    claims_stale: int

    def event_fields(self) -> dict[str, Any]:
        return {
            "audit_total_chunks": self.total_chunks,
            "audit_total_files": self.total_files,
            "audit_by_origin": self.by_origin,
            "audit_by_kind": self.by_kind,
            "audit_company_contamination": self.company_contamination,
            "audit_missing_origin": self.missing_origin,
            "audit_missing_project": self.missing_project,
            "audit_clean": self.clean,
            "audit_graph_documents": self.graph_documents,
            "audit_graph_edges": self.graph_edges,
            "audit_graph_claims_with_doc": self.graph_claims_with_doc,
            "audit_nodes": self.nodes,
            "audit_edges": self.edges,
            "audit_claims_anchored": self.claims_anchored,
            "audit_claims_unanchored": self.claims_unanchored,
            "audit_claims_pre_anchor": self.claims_pre_anchor,
            "audit_claims_label_only": self.claims_label_only,
            "audit_claims_stale": self.claims_stale,
        }


def _one(cur: Any, sql: str, params: dict[str, Any] | None = None) -> tuple:
    cur.execute(sql, params)
    return tuple(cur.fetchone())


def _tally(cur: Any, sql: str) -> dict[str, int]:
    cur.execute(sql)
    rows = sorted(cur.fetchall(), key=lambda row: (-row[1], row[0]))
    return {key: count for key, count in rows}


def stats(cur: Any, allow_company: bool) -> AuditStats:
    total_chunks, total_files = _one(cur, _CHUNKS_SQL)
    by_origin = _tally(cur, _BY_ORIGIN_SQL)
    by_kind = _tally(cur, _BY_KIND_SQL)
    company_contamination = by_origin.get("company", 0)
    missing_origin = by_origin.get("", 0)
    nodes = {kind: _one(cur, _NODE_KIND_SQL, {"kind": kind})[0] for kind in _NODE_KINDS}
    edges = {kind: _one(cur, _EDGE_KIND_SQL, {"kind": kind})[0] for kind in _EDGE_KINDS}
    anchored, unanchored, pre_anchor, label_only = _one(
        cur, _ERA_SQL, {"chars": INFORMATIVE_VALUE_CHARS, "tautology": TAUTOLOGICAL_PREDICATES}
    )
    return AuditStats(
        total_chunks=total_chunks,
        total_files=total_files,
        by_origin=by_origin,
        by_kind=by_kind,
        company_contamination=company_contamination,
        company_allowed=allow_company,
        missing_origin=missing_origin,
        missing_project=_one(cur, _MISSING_PROJECT_SQL)[0],
        clean=(allow_company or company_contamination == 0) and missing_origin == 0,
        graph_documents=_one(cur, _DOCUMENTS_SQL)[0],
        graph_edges=_one(cur, _EDGES_SQL)[0],
        graph_claims_with_doc=_one(cur, _CLAIMS_WITH_DOC_SQL)[0],
        nodes=nodes,
        edges=edges,
        claims_anchored=anchored,
        claims_unanchored=unanchored,
        claims_pre_anchor=pre_anchor,
        claims_label_only=label_only,
        claims_stale=_one(cur, _STALE_SQL)[0],
    )


def _count(statement: list[dict[str, Any]]) -> int:
    """`SELECT count() AS n ... GROUP ALL` 은 빈 표에서 행이 없다 — 그 0 은 "센 결과 0"이다."""
    return statement[0]["n"] if statement else 0


def _by_kind_count(statement: list[dict[str, Any]], kinds: tuple[str, ...]) -> dict[str, int]:
    found = {row["kind"]: row["n"] for row in statement}
    return {kind: found.get(kind, 0) for kind in kinds}


def stats_surreal(client: SurrealClient, allow_company: bool) -> Either[AuditStats, SurrealError]:
    """`stats` 의 Surreal 짝(엔진 doc_reads.rs graph_stats·all_meta, claim_reads.rs claim_era_counts 와 같은 집계).
    chunk 에는 kind 열이 없어 노트의 kind 를 document 에서 가져온다 — document 행이 없는 chunk 는 두 표가 갈린 것이라
    조용히 세지 않고 Err 로 알린다."""
    label_only = (
        f"string::len(value) < {INFORMATIVE_VALUE_CHARS} OR "
        f"string::matches(predicate, <regex> {surreal.text('(?i)' + TAUTOLOGICAL_PREDICATES)})"
    )
    sql = (
        "SELECT source_path, origin, count() AS n FROM chunk GROUP BY source_path, origin;"
        "SELECT source_path, kind FROM document;"
        "SELECT count() AS n FROM chunk WHERE project = '' GROUP ALL;"
        "SELECT count() AS n FROM document GROUP ALL;"
        "SELECT count() AS n FROM edge GROUP ALL;"
        "SELECT count() AS n FROM (SELECT dst FROM edge WHERE kind = 'claims' GROUP BY dst) GROUP ALL;"
        "SELECT kind, count() AS n FROM node GROUP BY kind;"
        "SELECT kind, count() AS n FROM edge GROUP BY kind;"
        "SELECT count() AS n FROM claim_row WHERE superseded_us = NONE AND era = 'anchored' GROUP ALL;"
        "SELECT count() AS n FROM claim_row WHERE superseded_us = NONE AND era = 'unanchored' GROUP ALL;"
        "SELECT count() AS n FROM claim_row WHERE superseded_us = NONE AND era = 'pre-anchor' GROUP ALL;"
        f"SELECT count() AS n FROM claim_row WHERE superseded_us = NONE AND ({label_only}) GROUP ALL;"
        "SELECT count() AS n FROM claim_row WHERE stale_us != NONE GROUP ALL;"
    )
    match client.query(sql):
        case Err() as failed:
            return failed
        case Ok(s) if len(s) == 13:
            pass
        case Ok(s):
            return Err(SurrealError(f"audit: 13 statements expected, got {len(s)}"))
    chunk_groups, doc_kinds = s[0], {row["source_path"]: row["kind"] for row in s[1]}
    by_origin: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for group in chunk_groups:
        if group["source_path"] not in doc_kinds:
            return Err(SurrealError(f"audit: chunk of {group['source_path']} has no document row in surreal"))
        by_origin[group["origin"]] = by_origin.get(group["origin"], 0) + group["n"]
        kind = doc_kinds[group["source_path"]]
        by_kind[kind] = by_kind.get(kind, 0) + group["n"]
    ordered = lambda tally: dict(sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])))  # noqa: E731
    company = by_origin.get("company", 0)
    missing_origin = by_origin.get("", 0)
    return Ok(
        AuditStats(
            total_chunks=sum(by_origin.values()),
            total_files=len({g["source_path"] for g in chunk_groups}),
            by_origin=ordered(by_origin),
            by_kind=ordered(by_kind),
            company_contamination=company,
            company_allowed=allow_company,
            missing_origin=missing_origin,
            missing_project=_count(s[2]),
            clean=(allow_company or company == 0) and missing_origin == 0,
            graph_documents=_count(s[3]),
            graph_edges=_count(s[4]),
            graph_claims_with_doc=_count(s[5]),
            nodes=_by_kind_count(s[6], _NODE_KINDS),
            edges=_by_kind_count(s[7], _EDGE_KINDS),
            claims_anchored=_count(s[8]),
            claims_unanchored=_count(s[9]),
            claims_pre_anchor=_count(s[10]),
            claims_label_only=_count(s[11]),
            claims_stale=_count(s[12]),
        )
    )
