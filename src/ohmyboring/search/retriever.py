"""PgRetriever — LangChain BaseRetriever over the python ranking pipeline (문의 /search).

질의 → list[Document]. metadata 키는 agents/memory/retriever.py:_hit_to_document 가 읽는
이름과 같다 — 문 소비자(아침 카드 후보 기록·Deep Agent recall)가 바꿔 끼우지 않고 받는다.
계산은 Either 값으로 흐른다. 단계는 모듈 함수 `state -> Either[state, SearchFailure]` 가
replace 로 한 칸씩 채우고, 순서는 STEPS 한 자리에 펼쳐 있다 — 임베딩·접속도 첫 두 단계.
search 는 ExitStack 을 열고 `functools.reduce(bind, STEPS, ...)` 로 한 줄로 잇고 문서
목록은 마지막 map_ok 에서 나온다. 실패 값은 _map_err 가 SearchFailure 로 바꾼다. 접속은
_connect 이 받은 stack 에 closing 을 등록해, 뒤 단계가 Err 로 끝나도 닫힌다. match 로
접는 갈래는 BaseRetriever 계약(_get_relevant_documents) 한 자리뿐이다 (빈 목록이 아니라
raise).
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
#: serve.rs:785-787 — related 는 머리 hit 하나당 이 값까지만 (req.related().min(3)).
SEARCH_MAX_RELATED = 3
#: drudge/src/serve/http.rs:35 — RELATED_SNIPPET_CHARS.
RELATED_SNIPPET_CHARS = 1200


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
    """파이프라인이 한 줄로 끌고 가는 값 한 벌 — 앞줄은 입력, 뒷줄은 각 단계가 채운다."""

    query: str
    dsn: str
    plan: _Plan
    project: str | None
    since_hours: int | None
    claims: int
    related: int
    related_heads: int
    stack: contextlib.ExitStack
    vector: list[float] = field(default_factory=list)
    conn: Any = None
    vec_hits: list[rank.Hit] = field(default_factory=list)
    txt_hits: list[rank.Hit] = field(default_factory=list)
    feedback: dict[str, rank.Counts] = field(default_factory=dict)
    ordered: list[rank.Scored] = field(default_factory=list)
    consumption: dict[str, rank.Counts] = field(default_factory=dict)
    said: dict[str, int] = field(default_factory=dict)
    superseded: dict[str, list[str]] = field(default_factory=dict)
    declared: dict[str, pg.RegisterRows] = field(default_factory=dict)
    related_docs: dict[str, list[dict[str, str]]] = field(default_factory=dict)


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


def _embed(state: _State) -> Either[_State, SearchFailure]:
    match _map_err(embed_adapter.embed(state.query), _embed_failure):
        case Err(failure):
            return Err(failure)
        case Ok(vector):
            return Ok(replace(state, vector=vector))


def _connect(state: _State) -> Either[_State, SearchFailure]:
    match _map_err(pg.connect(state.dsn), _pg_failure("store")):
        case Err(failure):
            return Err(failure)
        case Ok(conn):
            state.stack.enter_context(contextlib.closing(conn))
            return Ok(replace(state, conn=conn))


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


def _related(state: _State) -> Either[_State, SearchFailure]:
    """related>0 일 때 머리 hit related_heads 개 의 related_by_shared_ground 을 붙인다 — http.rs:539-561.
    관계 없음(related==0) 이면 질의 없이 상태 그대로. 실패는 'related: …' — 문 핸들러가 502 로 접는다."""
    if state.related <= 0:
        return Ok(state)
    heads = _paths(state.ordered)[: state.related_heads]
    lists: list[list[tuple[str, str]]] = []
    for path in heads:
        match _map_err(pg.related_by_shared_ground(state.conn, path, state.related), _pg_failure("related")):
            case Err(failure):
                return Err(failure)
            case Ok(docs):
                lists.append([(doc.source_path, doc.content) for doc in docs])
    return Ok(
        replace(
            state,
            related_docs=rank.attach_related(heads, _paths(state.ordered), lists, RELATED_SNIPPET_CHARS),
        )
    )


#: 파이프라인 순서 — 이 목록 읽는 순서가 실행 순서다. 단계는 서로를 부르지 않는다.
STEPS: tuple[Callable[[_State], Either[_State, SearchFailure]], ...] = (
    _embed,
    _connect,
    _vector_search,
    _text_search,
    _feedback_counts,
    _score,
    _attach_consumption,
    _said_by_owner,
    _superseded,
    _claims,
    _related,
)


def _documents(state: _State) -> list[Document]:
    enrich = _Enrich(
        consumption=state.consumption,
        said=state.said,
        superseded=state.superseded,
        declared=state.declared,
        related=state.related_docs,
    )
    return [_to_document(scored, enrich) for scored in state.ordered]


@dataclass(frozen=True)
class _Enrich:
    """hit 옆에 붙는 표들 — attach 단계들이 state 에 채우고 _to_document 가 경로로 찾아 싣는다."""

    consumption: dict[str, rank.Counts]
    said: dict[str, int]
    superseded: dict[str, list[str]]
    declared: dict[str, pg.RegisterRows]
    related: dict[str, list[dict[str, str]]]


def _to_document(scored, enrich: _Enrich) -> Document:
    hit = scored.hit
    counts = enrich.consumption.get(hit.source_path, rank.Counts())
    metadata: dict[str, Any] = {
        "source_path": hit.source_path,
        "project": hit.project,
        "origin": hit.origin,
        "used_count": counts.used,
        "contested_count": counts.contested,
        "said_by_owner": enrich.said.get(hit.source_path, 0),
        "superseded_by": enrich.superseded.get(hit.source_path, []),
        "dist": hit.dist,
        "dist_kind": hit.dist_kind,
    }
    related = enrich.related.get(hit.source_path)
    if related:
        metadata["related"] = related
    rows = enrich.declared.get(hit.source_path)
    if rows is not None:
        if rows.rows:
            metadata["claims"] = [row.as_dict() for row in rows.rows]
        metadata["claims_total"] = rows.total_matching
    return Document(id=hit.id, page_content=hit.content, metadata=metadata)


class PgRetriever(BaseRetriever):
    """문(:7710)의 파이썬 /search 한 벌 — RRF 융합·판정 넛지·예산·집합 안 순서·related 까지.

    dsn 으로 store 에 직접 붙고 임베딩은 ohmyboring.config 정본(bge-m3)을 쓴다.
    클램프는 drudge 와 같다: max_results 1..50, max_tokens 1..16_384 (→ max_chars = tokens*4),
    claims 0..10, related 0..3 (serve.rs:785), related_heads 0..max_results (serve.rs:789).
    """

    dsn: str
    max_results: int = 5
    max_tokens: int = 2000
    project: str | None = None
    since_hours: int | None = None
    claims: int = 0
    related: int = 0
    related_heads: int = 0

    def search(self, query: str) -> Either[list[Document], SearchFailure]:
        """클램프 → ExitStack → STEPS 한 줄 → 문서 목록 (접속은 스택이 닫는다)."""

        max_results = min(max(self.max_results, 1), MCP_MAX_RESULTS)
        max_tokens = min(max(self.max_tokens, 1), MCP_MAX_TOKENS)
        plan = _Plan(
            pool=rank.pool_size(max_results),
            max_results=max_results,
            max_chars=max_tokens * 4,
        )
        with contextlib.ExitStack() as stack:
            return map_ok(
                functools.reduce(
                    bind,
                    STEPS,
                    Ok(
                        _State(
                            query=query,
                            dsn=self.dsn,
                            plan=plan,
                            project=self.project,
                            since_hours=self.since_hours,
                            claims=self.claims,
                            related=min(max(self.related, 0), SEARCH_MAX_RELATED),
                            related_heads=min(max(self.related_heads, 0), max_results),
                            stack=stack,
                        )
                    ),
                ),
                _documents,
            )

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        # BaseRetriever 의 프레임워크 계약 — BoringRetriever 와 같이 실패는 빈 목록이 아니라 raise.
        match self.search(query):
            case Ok(documents):
                return documents
            case Err(failure):
                raise ConnectionError(f"python /search failed: {failure.detail}")
