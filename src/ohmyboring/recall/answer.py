"""MCP recall 의 파이썬 이식 (E4-2) — drudge/src/serve/mcp.rs::mcp_recall + recall_text.

wiki 직독이 먼저, 한 건도 없을 때만 벡터+어휘(PgRetriever)로 간다. wiki 길의 집합 안 순서는
retrieve.rs::order_wiki_hits(= rank.order_within_set), 렌더는 recall_text 그대로 — 줄은 빈 줄로
잇고 대체된 노트는 라벨에 새 노트 이름이 붙는다. 이 모듈은 handover(session_id)를 쓰지 않는다 —
보인 경로를 Recalled.shown 으로 돌려줄 뿐, 쓰기는 문(DOOR_RECALL_READER=python)이 한다.
wiki 길엔 max_tokens 예산이 걸리지 않는다(엔진 그대로).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo

from ohmyboring.recall import wiki
from ohmyboring.result import Either, Err, Ok
from ohmyboring.search import pg, rank
from ohmyboring.search.retriever import MCP_MAX_RESULTS, MCP_MAX_TOKENS, PgRetriever

NO_EXPERIENCE = "(no experience recalled)"
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"

_DEFAULT_RESULTS = 5
_DEFAULT_TOKENS = 2000
_CLAIMS_PER_NOTE = 5
FULL_BODY_HINT = "(본문 전체: query 에 wiki-NNNN 을 넣으면 그 노트 전체 블록이 앞에 붙는다)"
_U64_LIMIT = 2**64
_I32_MIN, _I32_MAX = -(2**31), 2**31 - 1

Line = tuple[str, str]


@dataclass(frozen=True)
class Args:
    query: str
    max_results: int
    max_tokens: int
    project: str | None
    since_hours: int | None


@dataclass(frozen=True)
class Rejected:
    message: str


@dataclass(frozen=True)
class Failed:
    detail: str


OrderKey = tuple[tuple[bool, bool, float], float]


@dataclass(frozen=True)
class PoolEntry:
    """wiki 길에서 점수로 뽑힐 수 있었던 노트 하나 — 엔진이 같은 자리에서 낼 수 있는 줄과 순서 열쇠."""

    path: str
    line: str
    score: float
    key: OrderKey


@dataclass(frozen=True)
class WikiPool:
    """점수 내림차순(동점은 경로순) 후보 중 k 번째 점수 이상 전부. size 개가 뽑혔다."""

    entries: tuple[PoolEntry, ...]
    size: int


@dataclass(frozen=True)
class Recalled:
    text: str
    path: str  # wiki | vector — 어느 길이 줄을 냈나
    skipped: int = 0  # 읽지 못해 건너뛴 wiki 파일 수
    pool: WikiPool | None = None  # wiki 길만
    shown: tuple[str, ...] = ()  # 답에 실린 줄의 경로 — handover 가 쓸 목록(엔진 mcp_recall 의 paths)
    refined: str = (
        ""  # MCP 답으로 나가는 모양 — claims 있는 노트는 머리줄+현재 claims. text 는 엔진 대조용이라 그대로
    )


@dataclass(frozen=True)
class Seams:
    wiki: Callable[[Args], wiki.WikiResult]
    rank_facts: Callable[[list[str]], Either[dict[str, rank.RankFacts], str]]
    vector: Callable[[Args], Either[list[Line], str]]
    superseded_by: Callable[[list[str]], Either[dict[str, list[str]], str]]
    current_claims: Callable[[list[str]], Either[dict[str, pg.RegisterRows], str]]


def _trimmed(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _u64(value: Any, default: int) -> int:
    ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value < _U64_LIMIT
    return value if ok else default


def _i32(value: Any) -> int | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and _I32_MIN <= value <= _I32_MAX
    return value if ok else None


def parse_args(arguments: dict) -> Either[Args, Rejected]:
    query = _trimmed(arguments.get("query"))
    if query is None:
        return Err(Rejected("missing argument: query"))
    return Ok(
        Args(
            query=query,
            max_results=min(max(_u64(arguments.get("max_results"), _DEFAULT_RESULTS), 1), MCP_MAX_RESULTS),
            max_tokens=min(max(_u64(arguments.get("max_tokens"), _DEFAULT_TOKENS), 1), MCP_MAX_TOKENS),
            project=_trimmed(arguments.get("project")),
            since_hours=_i32(arguments.get("since_hours")),
        )
    )


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def recall_line(path: str, body: str, superseded: dict[str, list[str]]) -> str:
    newer = superseded.get(path)
    label = _basename(path)
    if newer:
        label = f"{label} (superseded by {', '.join(_basename(p) for p in newer)})"
    return f"- [{label}] {body}"


def recall_text(lines: list[Line], superseded: dict[str, list[str]]) -> str:
    return "\n\n".join(recall_line(path, body, superseded) for path, body in lines)


def _claim_line(row: pg.RegisterRow) -> str:
    return f"  · {row.kind} {row.subject}/{row.predicate}: {row.value}"


def _refined_entry(
    path: str,
    body: str,
    superseded: dict[str, list[str]],
    claims: dict[str, pg.RegisterRows],
    titles: dict[str, str],
) -> tuple[str, bool]:
    register = claims.get(path)
    if register is None or not register.rows:
        return recall_line(path, body, superseded), False
    head = recall_line(path, "", superseded).rstrip()
    title = titles.get(path, "")
    head = f"{head} {title}" if title else head
    lines = [head, *(_claim_line(row) for row in register.rows)]
    hidden = register.total_matching - len(register.rows)
    if hidden > 0:
        lines.append(f"  · … +{hidden} more")
    return "\n".join(lines), True


def refined_text(
    lines: list[Line],
    superseded: dict[str, list[str]],
    claims: dict[str, pg.RegisterRows],
    titles: dict[str, str],
) -> str:
    entries = [_refined_entry(path, body, superseded, claims, titles) for path, body in lines]
    text = "\n\n".join(entry for entry, _ in entries)
    return f"{text}\n\n{FULL_BODY_HINT}" if any(has for _, has in entries) else text


@dataclass(frozen=True)
class _Candidate:
    hit: wiki.WikiHit
    key: OrderKey


@dataclass(frozen=True)
class _Selected:
    path: str
    lines: list[Line]
    skipped: int
    pool: tuple[_Candidate, ...] = ()
    size: int = 0
    titles: tuple[tuple[str, str], ...] = ()  # (경로, 제목) — wiki 길만, 머리줄에 붙는다


def _wiki_selected(result: wiki.WikiResult, args: Args, seams: Seams) -> Either[_Selected, Failed]:
    size = min(args.max_results, len(result.hits))
    threshold = result.hits[size - 1].score
    pool_hits = [hit for hit in result.hits if hit.score >= threshold]
    match seams.rank_facts(sorted({hit.source_path for hit in pool_hits})):
        case Err(detail):
            return Err(Failed(f"wiki recall order: rank: rank facts lookup: {detail}"))
        case Ok(facts):
            pool = tuple(
                _Candidate(hit, (rank.rank_key(facts.get(hit.source_path)), -hit.score)) for hit in pool_hits
            )
            scored = [
                rank.Scored(
                    rank.Hit(c.hit.id, c.hit.snippet, "", "", c.hit.source_path, 0.0, "wiki"), c.hit.score
                )
                for c in pool[:size]
            ]
            ordered = rank.order_within_set(scored, facts)
            lines = [(item.hit.source_path, item.hit.content) for item in ordered]
            titles = tuple((hit.source_path, hit.title) for hit in pool_hits)
            return Ok(_Selected("wiki", lines, result.skipped, pool, size, titles))


def _select(args: Args, seams: Seams) -> Either[_Selected, Failed]:
    result = seams.wiki(args)
    if result.hits:
        return _wiki_selected(result, args, seams)
    match seams.vector(args):
        case Err(detail):
            return Err(Failed(f"retrieve: {detail}"))
        case Ok(lines):
            return Ok(_Selected("vector", lines, result.skipped))


def _pool_of(selected: _Selected, superseded: dict[str, list[str]]) -> WikiPool | None:
    if not selected.pool:
        return None
    entries = tuple(
        PoolEntry(
            c.hit.source_path,
            recall_line(c.hit.source_path, c.hit.snippet, superseded),
            c.hit.score,
            c.key,
        )
        for c in selected.pool
    )
    return WikiPool(entries, selected.size)


def answer(args: Args, seams: Seams) -> Either[Recalled, Failed]:
    match _select(args, seams):
        case Err(failure):
            return Err(failure)
        case Ok(selected) if not selected.lines:
            return Ok(Recalled(NO_EXPERIENCE, selected.path, selected.skipped, refined=NO_EXPERIENCE))
        case Ok(selected):
            paths = [c.hit.source_path for c in selected.pool] or [line[0] for line in selected.lines]
            match seams.superseded_by(paths):
                case Err(detail):
                    return Err(Failed(f"recall superseded: {detail}"))
                case Ok(superseded):
                    shown = tuple(path for path, _ in selected.lines)
                    match seams.current_claims(list(shown)):
                        case Err(detail):
                            return Err(Failed(f"recall claims: {detail}"))
                        case Ok(claims):
                            text = recall_text(selected.lines, superseded)
                            refined = refined_text(selected.lines, superseded, claims, dict(selected.titles))
                            pool = _pool_of(selected, superseded)
                            return Ok(Recalled(text, selected.path, selected.skipped, pool, shown, refined))


def _detail(result: Either[Any, Any]) -> Either[Any, str]:
    match result:
        case Err(failure):
            return Err(failure.detail)
        case Ok(value):
            return Ok(value)


def _vector_lines(read_only_dsn: str, args: Args) -> Either[list[Line], str]:
    retriever = PgRetriever(
        dsn=read_only_dsn,
        max_results=args.max_results,
        max_tokens=args.max_tokens,
        project=args.project,
        since_hours=args.since_hours,
    )
    match _detail(retriever.search(args.query)):
        case Err(detail):
            return Err(detail)
        case Ok(documents):
            return Ok([(doc.metadata["source_path"], doc.page_content) for doc in documents])


def run(
    arguments: dict,
    *,
    dsn: str,
    wiki_dir: Path,
    index: wiki.WikiIndex,
    now_ns: int,
) -> Either[Recalled, Rejected | Failed]:
    """문 경계 — 인자를 한 번 좁히고, 읽기 전용 연결 하나를 열어 answer 에 seams 로 건넨다."""
    match parse_args(arguments):
        case Err(rejected):
            return Err(rejected)
        case Ok(args):
            pass
    try:
        read_only_dsn = make_conninfo(dsn, options=READ_ONLY_OPTIONS)
    except psycopg.Error as e:
        return Err(Failed(f"pg dsn: {e}"))
    match pg.connect(read_only_dsn):
        case Err(failure):
            return Err(Failed(f"pg connect: {failure.detail}"))
        case Ok(conn):
            pass
    seams = Seams(
        wiki=lambda a: index.recall(wiki_dir, wiki.Ask(a.query, a.project, a.since_hours), now_ns),
        rank_facts=lambda paths: _detail(pg.rank_facts(conn, paths)),
        vector=lambda a: _vector_lines(read_only_dsn, a),
        superseded_by=lambda paths: _detail(pg.superseded_by(conn, paths)),
        current_claims=lambda paths: _detail(pg.declared_claims(conn, paths, _CLAIMS_PER_NOTE)),
    )
    with contextlib.closing(conn):
        return answer(args, seams)
