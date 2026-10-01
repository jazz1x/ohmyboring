"""psycopg 질의 한 곳 — /search 파이프라인이 store 에 묻는 모든 SQL.

drudge/src/store.rs 의 해당 질의를 그대로 옮긴다. 실패는 psycopg 경계에서 Err 값으로 —
부르는 쪽이 한 곳에서 접는다 (조용한 폭락 금지). pgvector 파이썬 패키지 없이 벡터는
'[f1,f2,...]'::vector 문자열 캐스트로 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC

import psycopg

from ohmyboring.result import Either, Err, Ok
from ohmyboring.search import rank, redact


#: 문서 노드 id — store.rs doc_node_id (chunk id 의 '#' 꼬리는 뗀다).
def doc_node_id(chunk_or_path: str) -> str:
    path = chunk_or_path.rsplit("#", 1)[0] if "#" in chunk_or_path else chunk_or_path
    return f"doc:{path}"


@dataclass(frozen=True)
class PgError:
    """질의 실패 값 — 예외 메시지를 담는다."""

    detail: str


@dataclass(frozen=True)
class RegisterRow:
    """claim 레지스터 행 — SearchHit.claims 가 실는 모양 (store.rs RegisterRow)."""

    node_id: str
    subject: str
    predicate: str
    value: str
    kind: str
    confidence: str
    valid_from: str  # RFC 3339
    project: str

    def as_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "kind": self.kind,
            "confidence": self.confidence,
            "valid_from": self.valid_from,
            "project": self.project,
        }


@dataclass(frozen=True)
class RegisterRows:
    rows: tuple[RegisterRow, ...]
    total_matching: int


@dataclass(frozen=True)
class HandoverReport:
    handed: int
    unknown: int


@dataclass(frozen=True)
class RelatedDoc:
    """related_by_shared_ground 한 행 — store.rs RecentDoc."""

    source_path: str
    project: str
    content: str
    tags: list[str]


#: claim.kind() 대로 — 빈 kind 는 fact, (next|blocked) 가 작업 부정을 담으면 fact.
_WORK_DENIALS = frozenset(
    {
        "none",
        "nothing",
        "n/a",
        "na",
        "not applicable",
        "no",
        "-",
        "--",
        "없음",
        "없다",
        "없습니다",
        "없어요",
        "해당 없음",
        "해당없음",
        "남은 작업 없음",
        "남은 작업이 없음",
        "なし",
        "無し",
        "該当なし",
    }
)


def _canonical_kind(kind: str, value: str) -> str:
    k = kind.strip()
    if not k:
        return "fact"
    v = value.strip().rstrip(".。").strip().lower()
    if k in ("next", "blocked") and v in _WORK_DENIALS:
        return "fact"
    return k


def _canonical_confidence(confidence: str) -> str:
    c = confidence.strip()
    return c if c else "unknown"


def _attempt(thunk) -> Either:
    """psycopg 경계 — 예외를 Err 값으로 접는 유일한 자리."""
    try:
        return Ok(thunk())
    except psycopg.Error as e:
        return Err(PgError(str(e)))


def connect(dsn: str) -> Either[psycopg.Connection, PgError]:
    return _attempt(lambda: psycopg.connect(dsn))


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


_VECTOR_SQL = (
    "SELECT c.id, c.content, c.origin, c.project, c.source_path,"
    " (c.embedding <=> %s::vector)::float4 AS dist"
    " FROM chunk c"
    " JOIN document d ON d.source_path = c.source_path"
    " WHERE (%s::text IS NULL OR c.project = %s)"
    " AND (%s::int IS NULL OR d.updated_at >= now() - make_interval(hours => %s))"
    " ORDER BY c.embedding <=> %s::vector"
    " LIMIT %s;"
)

_TEXT_SQL = (
    "SELECT c.id, c.content, c.origin, c.project, c.source_path,"
    " ts_rank(c.tsv, plainto_tsquery('simple', %s))::float4 AS dist"
    " FROM chunk c"
    " JOIN document d ON d.source_path = c.source_path"
    " WHERE c.tsv @@ plainto_tsquery('simple', %s)"
    " AND (%s::text IS NULL OR c.project = %s)"
    " AND (%s::int IS NULL OR d.updated_at >= now() - make_interval(hours => %s))"
    " ORDER BY dist DESC"
    " LIMIT %s;"
)


def _row_to_hit(row: tuple, dist_kind: str) -> rank.Hit:
    return rank.Hit(
        id=row[0],
        content=row[1],
        origin=row[2],
        project=row[3],
        source_path=row[4],
        dist=float(row[5]),
        dist_kind=dist_kind,
    )


def vector_search(
    conn: psycopg.Connection,
    vec: list[float],
    k: int,
    project: str | None,
    since_hours: int | None,
) -> Either[list[rank.Hit], PgError]:
    """store.rs:2237 vector_search_filtered — 코사인 <=>, project/since_hours 필터, LIMIT pool."""
    literal = _vector_literal(vec)

    def run() -> list[rank.Hit]:
        with conn.cursor() as cur:
            cur.execute(_VECTOR_SQL, (literal, project, project, since_hours, since_hours, literal, k))
            return [_row_to_hit(row, "vector_cosine") for row in cur.fetchall()]

    return _attempt(run)


def text_search(
    conn: psycopg.Connection,
    query: str,
    k: int,
    project: str | None,
    since_hours: int | None,
) -> Either[list[rank.Hit], PgError]:
    """store.rs:2295 text_search_filtered — ts_rank(simple), @@ plainto_tsquery."""
    return _attempt(
        lambda: [
            _row_to_hit(row, "text_rank")
            for row in _exec_fetchall(
                conn, _TEXT_SQL, (query, query, project, project, since_hours, since_hours, k)
            )
        ]
    )


def _exec_fetchall(conn: psycopg.Connection, sql: str, params: tuple) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


#: counts_by (store.rs:3070) — ranking(=owner 필터) 와 consumption(=모든 judge) 이 같은
#: 질의를 owner_only 만 바꿔 탄다. 행은 rank.tally_feedback 이 센다 (필터의 순수한 자리).
_COUNTS_SQL = (
    "SELECT e.dst, e.kind, e.judge, d.author FROM edge e"
    " LEFT JOIN document d ON d.source_path = substr(e.dst, 5)"
    " WHERE e.dst = ANY(%s) AND e.kind IN ('used','contested');"
)


def _counts(
    conn: psycopg.Connection, paths: list[str], owner_only: bool
) -> Either[dict[str, rank.Counts], PgError]:
    doc_ids = [doc_node_id(path) for path in paths]
    match _attempt(lambda: _exec_fetchall(conn, _COUNTS_SQL, (doc_ids,))):
        case Err(e):
            return Err(e)
        case Ok(rows):
            return Ok(rank.tally_feedback(rows, owner_only))


def ranking_feedback_counts(
    conn: psycopg.Connection, paths: list[str]
) -> Either[dict[str, rank.Counts], PgError]:
    """ranking 이 읽는 판정 — owner 노트엔 owner 판정 contested 만 깎는다 (store.rs:3063)."""
    return _counts(conn, paths, owner_only=True)


def consumption_counts(conn: psycopg.Connection, paths: list[str]) -> Either[dict[str, rank.Counts], PgError]:
    """hit 옆의 used/contested 표시 — judge 불문 전부 (store.rs:3054). handed 는 절대 안 센다."""
    return _counts(conn, paths, owner_only=False)


def said_by_owner_counts(conn: psycopg.Connection, paths: list[str]) -> Either[dict[str, int], PgError]:
    """store.rs:3113 — 노트 자기 claim 행 중 said_by='owner' 이고 현재 행인 것."""
    return _attempt(
        lambda: {
            row[0]: row[1]
            for row in _exec_fetchall(
                conn,
                "SELECT source_path, count(*) FROM claim"
                " WHERE source_path = ANY(%s) AND said_by = 'owner' AND superseded_at IS NULL"
                " GROUP BY source_path;",
                (paths,),
            )
        }
    )


def superseded_by(conn: psycopg.Connection, paths: list[str]) -> Either[dict[str, list[str]], PgError]:
    """store.rs:3181 — supersedes 간선의 대상(옛 노트) 마다 새 노트 경로."""
    doc_ids = [doc_node_id(path) for path in paths]

    def run() -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for dst, src in _exec_fetchall(
            conn, "SELECT dst, src FROM edge WHERE dst = ANY(%s) AND kind = 'supersedes';", (doc_ids,)
        ):
            out.setdefault(dst.removeprefix("doc:"), []).append(src.removeprefix("doc:"))
        return out

    return _attempt(run)


#: store.rs:1197-1250 related_by_shared_ground — 공유 claim 이 2, 공유 concept:% 가 1. 후보는 self
#: 보다 updated_at 이 오래된 document 뿐 (선별 전에 ranking 하면 자기 자신이 먹통이 된다는 측정이
#: 그 근거 — 주석 그대로 옮김). LIMIT 전에 doc_node ASC, 최종은 shared DESC 에 source_path ASC
#: (Rust 에서 결정적이던 경우와 같은 전순서 — E2c 결정).
_RELATED_BY_SHARED_GROUND_SQL = (
    "WITH self_concepts AS ("
    " SELECT dst FROM edge WHERE src = %s AND kind = 'about'"
    " AND dst LIKE 'concept:%'"
    "), self_claims AS ("
    " SELECT dst FROM edge WHERE src = %s AND kind = 'claims'"
    "), shares AS ("
    " SELECT e.src AS doc_node, 2 AS weight"
    " FROM edge e JOIN self_claims sl ON e.dst = sl.dst"
    " WHERE e.src <> %s AND e.kind = 'claims'"
    " UNION ALL"
    " SELECT e.src AS doc_node, 1 AS weight"
    " FROM edge e JOIN self_concepts sc ON e.dst = sc.dst"
    " WHERE e.src <> %s AND e.kind = 'about'"
    "), ranked AS ("
    " SELECT s.doc_node, sum(s.weight) AS shared"
    " FROM shares s"
    " JOIN document od ON ('doc:' || od.source_path) = s.doc_node"
    " WHERE od.updated_at < (SELECT updated_at FROM document WHERE source_path = %s)"
    " GROUP BY s.doc_node ORDER BY shared DESC, s.doc_node ASC LIMIT %s"
    ")"
    " SELECT d.source_path, d.project, d.tags,"
    " string_agg(c.content, E'\\n' ORDER BY c.chunk_idx) AS content"
    " FROM ranked r"
    " JOIN document d ON ('doc:' || d.source_path) = r.doc_node"
    " JOIN chunk c ON c.source_path = d.source_path"
    " GROUP BY d.source_path, d.project, d.tags, r.shared"
    " ORDER BY r.shared DESC, d.source_path ASC;"
)


def related_by_shared_ground(
    conn: psycopg.Connection, source_path: str, limit: int
) -> Either[list[RelatedDoc], PgError]:
    """store.rs:1187 — self 의 claims/concept:% 간선 dst 를 공유하는 더 오래된 문서들."""

    def run() -> list[RelatedDoc]:
        doc_id = doc_node_id(source_path)
        return [
            RelatedDoc(
                source_path=row[0],
                project=row[1],
                tags=list(row[2] or []),
                content=row[3] or "",
            )
            for row in _exec_fetchall(
                conn,
                _RELATED_BY_SHARED_GROUND_SQL,
                (doc_id, doc_id, doc_id, doc_id, source_path, limit),
            )
        ]

    return _attempt(run)


def rank_facts(conn: psycopg.Connection, paths: list[str]) -> Either[dict[str, rank.RankFacts], PgError]:
    """store.rs:3205 — 대첸 여부·저자·갱신 시각. 집합 안 순서가 읽는 유일한 사실."""

    def run() -> dict[str, rank.RankFacts]:
        out: dict[str, rank.RankFacts] = {}
        for path, is_owner, updated_at, superseded in _exec_fetchall(
            conn,
            "SELECT d.source_path, d.author = 'owner', d.updated_at,"
            " EXISTS (SELECT 1 FROM edge e"
            " WHERE e.dst = 'doc:' || d.source_path AND e.kind = 'supersedes')"
            " FROM document d WHERE d.source_path = ANY(%s);",
            (paths,),
        ):
            out[path] = rank.RankFacts(superseded=superseded, owner=is_owner, updated_at=updated_at)
        return out

    return _attempt(run)


_DECLARED_CLAIMS_SQL = (
    "WITH asked AS ("
    " SELECT unnest(%s::text[]) AS doc_id, unnest(%s::text[]) AS path"
    "), declared AS ("
    " SELECT a.path AS doc_path, c.subject, c.predicate, c.value, c.kind, c.confidence, c.valid_from,"
    " dm.project,"
    " ROW_NUMBER() OVER ("
    " PARTITION BY e.src, e.dst"
    " ORDER BY (c.source_path = a.path) DESC, c.valid_from DESC"
    " ) AS pick"
    " FROM edge e"
    " JOIN asked a ON a.doc_id = e.src"
    " JOIN claim c ON ('claim:' || c.subject || ':' || c.predicate) = e.dst"
    " JOIN document dm ON dm.source_path = c.source_path"
    " WHERE e.kind = 'claims'"
    " AND c.superseded_at IS NULL"
    " AND c.kind <> 'fact'"
    "), ranked AS ("
    " SELECT doc_path, subject, predicate, value, kind, confidence, valid_from, project,"
    " CASE kind WHEN 'decision' THEN 0"
    " WHEN 'next' THEN 1"
    " WHEN 'blocked' THEN 1"
    " WHEN 'risk' THEN 2"
    " ELSE 3 END AS tier,"
    " COUNT(*) OVER (PARTITION BY doc_path) AS total"
    " FROM declared"
    " WHERE pick = 1"
    "), picked AS ("
    " SELECT *, ROW_NUMBER() OVER ("
    " PARTITION BY doc_path"
    " ORDER BY tier ASC, valid_from DESC, subject ASC, predicate ASC"
    " ) AS rn"
    " FROM ranked"
    ")"
    " SELECT doc_path, subject, predicate, value, kind, confidence, valid_from, project, total"
    " FROM picked"
    " WHERE rn <= %s"
    " ORDER BY doc_path, rn;"
)


def declared_claims(
    conn: psycopg.Connection, paths: list[str], per_doc: int
) -> Either[dict[str, RegisterRows], PgError]:
    """store.rs:1773 — 현재 claims→claim 간선이 잇는 행만, tier·최신 순, 한 문서당 per_doc."""

    def run() -> dict[str, RegisterRows]:
        doc_ids = [doc_node_id(path) for path in paths]
        out: dict[str, RegisterRows] = {}
        for row in _exec_fetchall(conn, _DECLARED_CLAIMS_SQL, (doc_ids, paths, per_doc)):
            path, subject, predicate, value, kind, confidence, valid_from, project, total = row
            entry = out.get(path)
            if entry is None:
                entry = RegisterRows(rows=(), total_matching=total)
            out[path] = RegisterRows(
                rows=entry.rows
                + (
                    RegisterRow(
                        node_id=f"claim:{subject}:{predicate}",
                        subject=subject,
                        predicate=predicate,
                        value=value,
                        kind=_canonical_kind(kind, value),
                        confidence=_canonical_confidence(confidence),
                        valid_from=valid_from.astimezone(UTC).isoformat(),
                        project=project,
                    ),
                ),
                total_matching=entry.total_matching,
            )
        return out

    return _attempt(run)


def record_handover(
    conn: psycopg.Connection, session_id: str, observed_at: str, paths: list[str]
) -> Either[HandoverReport, PgError]:
    """store.rs:3028 — session 노드 upsert + 알려진 경로 마다 handed 간선. 쓰기는 여기서 커밋."""

    def run() -> HandoverReport:
        session_node = f"session:{session_id}"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO node (id, kind, label, outcome) VALUES (%s, 'session', %s, NULL)"
                " ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label, outcome = EXCLUDED.outcome;",
                (session_node, observed_at),
            )
            cur.execute("SELECT source_path FROM document WHERE source_path = ANY(%s);", (paths,))
            known = {row[0] for row in cur.fetchall()}
            handed = 0
            for path in paths:
                if path not in known:
                    continue
                cur.execute(
                    "INSERT INTO edge (src, dst, kind, judge) VALUES (%s, %s, 'handed', NULL)"
                    " ON CONFLICT DO NOTHING;",
                    (session_node, doc_node_id(path)),
                )
                handed += 1
        conn.commit()
        return HandoverReport(handed=handed, unknown=len(paths) - handed)

    return _attempt(run)


def log_query(conn: psycopg.Connection, row: QueryLogRow) -> Either[None, PgError]:
    """store.rs:2349 query_log — label-recall 의 표본 표. 쓰기는 여기서 커밋.

    적기 전 query·answer_snippet 은 redact 로 가린다 — store.rs:2359 과 같은
    누수 경계(query_log 은 백업·/query-log 으로 나간다)."""

    def run() -> None:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO query_log (endpoint, query, hit_paths, hit_dists, hit_dist_kinds,"
                " sources, answer_snippet, latency_ms)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s);",
                (
                    row.endpoint,
                    redact.redact(row.query),
                    [path for path, _, _ in row.logged_hits],
                    [dist for _, dist, _ in row.logged_hits],
                    [kind for _, _, kind in row.logged_hits],
                    row.sources,
                    redact.redact(row.answer_snippet),
                    row.latency_ms,
                ),
            )
        conn.commit()
        return None

    return _attempt(run)


@dataclass(frozen=True)
class QueryLogRow:
    """query_log 한 행 — logged_hits 는 (path, dist|None, dist_kind|None)."""

    endpoint: str
    query: str
    logged_hits: tuple[tuple[str, float | None, str | None], ...]
    sources: tuple[str, ...]
    answer_snippet: str
    latency_ms: int | None
