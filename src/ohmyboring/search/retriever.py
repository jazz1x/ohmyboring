"""PgRetriever — LangChain BaseRetriever over the python ranking pipeline (문의 /search).

질의 → list[Document]. metadata 키는 agents/memory/retriever.py:_hit_to_document 가 읽는
이름과 같다 — 문 소비자(아침 카드 후보 기록·Deep Agent recall)가 바꿔 끼우지 않고 받는다.
계산은 Either 값으로 흐른다. 단계는 모듈 함수 `state -> Either[state, SearchFailure]` 가
replace 로 한 칸씩 채우고, 순서는 STEPS 한 자리에 펼쳐 있다 — search 는 임베딩·접속을
연 뒤 `functools.reduce(bind, STEPS, ...)` 로 한 줄로 잇고 문서 목록은 마지막 map_ok
에서 나온다. 실패 값은 _map_err 가 SearchFailure 로 바꾼다. match 로 접는 갈래는 search
서두(임베딩·접속)와 BaseRetriever 계약(_get_relevant_documents) 두 자리뿐이다 (빈 목록이
아니라 raise).
"""

from __future__ import annotations

import contextlib
import functools
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from ohmyboring.adapters import embed as embed_adapter
from ohmyboring.result import Either, Err, Ok, bind, map_ok
from ohmyboring.search import pg, rank

#: drudge/src/serve.rs:1336-1340 — /search 의 고정 상한.
MCP_MAX_RESULTS = 50
MCP_MAX_TOKENS = 16_384
SEARCH_MAX_CLAIMS = 10


@dataclass(frozen=True)
class SearchFailure:
    """검색 실패 값 — 임베딩·질의·판정 읽기 어느 쪽이든. 문 핸들러가 502 로 접는다."""

    detail: str


@dataclass(frozen=True)
class _Plan:
    """search() 이 미리 계산해 파이프라인에 넘기는 수치들 — 클램프 뒤의 확정값."""

    pool: int
    max_results: int
    max_chars: int


@dataclass(frozen=True)
class _State:
    """파이프라인이 한 줄로 끌고 가는 값 한 벌 — 각 단계가 replace 로 한 칸씩 채운다."""

    query: str
    vector: list[float]
    conn: Any
    plan: _Plan
    project: str | None
    since_hours: int | None
    claims: int
    vec_hits: list[rank.Hit] = field(default_factory=list)
    txt_hits: list[rank.Hit] = field(default_factory=list)
    feedback: dict[str, rank.Counts] = field(default_factory=dict)
    ordered: list[rank.Scored] = field(default_factory=list)
    consumption: dict[str, rank.Counts] = field(default_factory=dict)
    said: dict[str, int] = field(default_factory=dict)
    superseded: dict[str, list[str]] = field(default_factory=dict)
    declared: dict[str, pg.RegisterRows] = field(default_factory=dict)


def _paths(ordered: list[rank.Scored]) -> list[str]:
    return [scored.hit.source_path for scored in ordered]


def _map_err(result: Either[Any, Any], to_failure: Callable[[Any], SearchFailure]) -> Either[Any, Any]:
    """Err 안의 실패 값만 SearchFailure 로 바꾼다 — 파이프라인의 실패 쪽 접는 자리는 이 한 곳."""

    if isinstance(result, Err):
        return Err(to_failure(result.error))
    return result


def _pg_failure(prefix: str) -> Callable[[pg.PgError], SearchFailure]:
    """pg 단계의 실패에 detail prefix 를 붙인 SearchFailure — 502 본문 모양이 여기서 나온다."""

    return lambda failure: SearchFailure(f"{prefix}: {failure.detail}")


def _embed_failure(failure: embed_adapter.EmbedFailure) -> SearchFailure:
    return SearchFailure(f"embed: {failure}")


def _capture(
    state: _State, result: Either[Any, Any], prefix: str, name: str
) -> Either[_State, SearchFailure]:
    """pg 계산의 공통 꼴 — 결과를 _map_err 로 접고, 성공 값은 지명한 state 칸에 담는다."""

    match _map_err(result, _pg_failure(prefix)):
        case Err(failure):
            return Err(failure)
        case Ok(value):
            return Ok(replace(state, **{name: value}))


def _vector_search(state: _State) -> Either[_State, SearchFailure]:
    return _capture(
        state,
        pg.vector_search(state.conn, state.vector, state.plan.pool, state.project, state.since_hours),
        "vector",
        "vec_hits",
    )


def _text_search(state: _State) -> Either[_State, SearchFailure]:
    return _capture(
        state,
        pg.text_search(state.conn, state.query, state.plan.pool, state.project, state.since_hours),
        "text",
        "txt_hits",
    )


def _feedback_counts(state: _State) -> Either[_State, SearchFailure]:
    pool_paths = sorted({h.source_path for h in state.vec_hits + state.txt_hits})
    return _capture(state, pg.ranking_feedback_counts(state.conn, pool_paths), "feedback", "feedback")


def _score(state: _State) -> Either[_State, SearchFailure]:
    scored = rank.within_budget(
        rank.merge_hits(state.vec_hits, state.txt_hits, state.feedback),
        state.plan.max_results,
        state.plan.max_chars,
    )
    match _map_err(pg.rank_facts(state.conn, _paths(scored)), _pg_failure("rank_facts")):
        case Err(failure):
            return Err(failure)
        case Ok(facts):
            return Ok(replace(state, ordered=rank.order_within_set(scored, facts)))


def _attach_consumption(state: _State) -> Either[_State, SearchFailure]:
    return _capture(
        state, pg.consumption_counts(state.conn, _paths(state.ordered)), "consumption", "consumption"
    )


def _said_by_owner(state: _State) -> Either[_State, SearchFailure]:
    return _capture(
        state, pg.said_by_owner_counts(state.conn, _paths(state.ordered)), "said_by_owner", "said"
    )


def _superseded(state: _State) -> Either[_State, SearchFailure]:
    return _capture(state, pg.superseded_by(state.conn, _paths(state.ordered)), "superseded_by", "superseded")


def _claims(state: _State) -> Either[_State, SearchFailure]:
    per_hit = min(max(state.claims, 0), SEARCH_MAX_CLAIMS)
    if per_hit <= 0:
        return Ok(state)
    return _capture(
        state,
        pg.declared_claims(state.conn, _paths(state.ordered), per_hit),
        "claims",
        "declared",
    )


#: 파이프라인 순서 — 이 목록 읽는 순서가 실행 순서다. 단계는 서로를 부르지 않는다.
STEPS: tuple[Callable[[_State], Either[_State, SearchFailure]], ...] = (
    _vector_search,
    _text_search,
    _feedback_counts,
    _score,
    _attach_consumption,
    _said_by_owner,
    _superseded,
    _claims,
)


def _documents(state: _State) -> list[Document]:
    return [
        _to_document(scored, state.consumption, state.said, state.superseded, state.declared)
        for scored in state.ordered
    ]


def _to_document(scored, consumption, said, superseded, declared) -> Document:
    hit = scored.hit
    counts = consumption.get(hit.source_path, rank.Counts())
    metadata: dict[str, Any] = {
        "source_path": hit.source_path,
        "project": hit.project,
        "origin": hit.origin,
        "used_count": counts.used,
        "contested_count": counts.contested,
        "said_by_owner": said.get(hit.source_path, 0),
        "superseded_by": superseded.get(hit.source_path, []),
        "dist": hit.dist,
        "dist_kind": hit.dist_kind,
    }
    rows = declared.get(hit.source_path)
    if rows is not None:
        if rows.rows:
            metadata["claims"] = [row.as_dict() for row in rows.rows]
        metadata["claims_total"] = rows.total_matching
    return Document(id=hit.id, page_content=hit.content, metadata=metadata)


class PgRetriever(BaseRetriever):
    """문(:7710)의 파이썬 /search 한 벌 — RRF 융합·판정 넛지·예산·집합 안 순서까지.

    dsn 으로 store 에 직접 붙고 임베딩은 ohmyboring.config 정본(bge-m3)을 쓴다.
    클램프는 drudge 와 같다: max_results 1..50, max_tokens 1..16_384 (→ max_chars = tokens*4),
    claims 0..10. related 는 여기 없다 — 그래프 확장은 문 핸들러가 드러지로 넘긴다.
    """

    dsn: str
    max_results: int = 5
    max_tokens: int = 2000
    project: str | None = None
    since_hours: int | None = None
    claims: int = 0

    def search(self, query: str) -> Either[list[Document], SearchFailure]:
        """임베딩 → 접속(어느 갈래로 끝나든 닫기) → STEPS 한 줄 → 문서 목록."""

        max_results = min(max(self.max_results, 1), MCP_MAX_RESULTS)
        max_tokens = min(max(self.max_tokens, 1), MCP_MAX_TOKENS)
        plan = _Plan(
            pool=rank.pool_size(max_results),
            max_results=max_results,
            max_chars=max_tokens * 4,
        )
        match _map_err(embed_adapter.embed(query), _embed_failure):
            case Err(failure):
                return Err(failure)
            case Ok(vector):
                pass
        match _map_err(pg.connect(self.dsn), _pg_failure("store")):
            case Err(failure):
                return Err(failure)
            case Ok(conn):
                pass
        state = _State(
            query=query,
            vector=vector,
            conn=conn,
            plan=plan,
            project=self.project,
            since_hours=self.since_hours,
            claims=self.claims,
        )
        with contextlib.closing(conn):
            return map_ok(functools.reduce(bind, STEPS, Ok(state)), _documents)

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        # BaseRetriever 의 프레임워크 계약 — BoringRetriever 와 같이 실패는 빈 목록이 아니라 raise.
        match self.search(query):
            case Ok(documents):
                return documents
            case Err(failure):
                raise ConnectionError(f"python /search failed: {failure.detail}")
