"""PgRetriever — LangChain BaseRetriever over the python ranking pipeline (문의 /search).

질의 → list[Document]. metadata 키는 agents/memory/retriever.py:_hit_to_document 가 읽는
이름과 같다 — 문 소비자(아침 카드 후보 기록·Deep Agent recall)가 바꿔 끼우지 않고 받는다.
계산은 Either 값으로 흐르고 bind 로 단계를 잇고, 실패 값은 _map_err 가 SearchFailure 로
바꾼다 — match 로 접는 갈래는 BaseRetriever 계약(_get_relevant_documents) 한 자리뿐이다
(빈 목록이 아니라 raise).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from ohmyboring.adapters import embed as embed_adapter
from ohmyboring.result import Either, Err, Ok, bind
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
class _Attachment:
    """attach 단계가 모으는 표시 한 벌 — ordered 는 출발 멤버, 나머지 칸은 bind 가 채운다."""

    ordered: list[rank.Scored]
    consumption: dict[str, rank.Counts] = field(default_factory=dict)
    said: dict[str, int] = field(default_factory=dict)
    superseded: dict[str, list[str]] = field(default_factory=dict)
    declared: dict[str, pg.RegisterRows] = field(default_factory=dict)


def _paths(ordered: list[rank.Scored]) -> list[str]:
    return [scored.hit.source_path for scored in ordered]


def _map_err(
    result: Either[Any, Any], to_failure: Callable[[Any], SearchFailure]
) -> Either[Any, SearchFailure]:
    """Err 안의 실패 값만 SearchFailure 로 바꾼다 — 파이프라인의 실패 쪽 접는 자리는 이 한 곳."""

    if isinstance(result, Err):
        return Err(to_failure(result.error))
    return result


def _pg_failure(prefix: str) -> Callable[[pg.PgError], SearchFailure]:
    """pg 단계의 실패에 detail prefix 를 붙인 SearchFailure — 502 본문 모양이 여기서 나온다."""

    return lambda failure: SearchFailure(f"{prefix}: {failure.detail}")


def _embed_failure(failure: embed_adapter.EmbedFailure) -> SearchFailure:
    return SearchFailure(f"embed: {failure}")


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
        max_results = min(max(self.max_results, 1), MCP_MAX_RESULTS)
        max_tokens = min(max(self.max_tokens, 1), MCP_MAX_TOKENS)
        plan = _Plan(
            pool=rank.pool_size(max_results),
            max_results=max_results,
            max_chars=max_tokens * 4,
        )
        return bind(
            _map_err(embed_adapter.embed(query), _embed_failure),
            lambda vector: self._connected(query, vector, plan),
        )

    def _connected(
        self, query: str, vector: list[float], plan: _Plan
    ) -> Either[list[Document], SearchFailure]:
        """임베딩 뒤의 단계들 — 접속을 열고 파이프라인이 끝까지 달리게 한 뒤 무조건 닫는다."""
        return bind(
            _map_err(pg.connect(self.dsn), _pg_failure("store")),
            lambda conn: self._run(conn, query, vector, plan),
        )

    def _run(
        self, conn, query: str, vector: list[float], plan: _Plan
    ) -> Either[list[Document], SearchFailure]:
        with contextlib.closing(conn):
            return bind(
                _map_err(
                    pg.vector_search(conn, vector, plan.pool, self.project, self.since_hours),
                    _pg_failure("vector"),
                ),
                lambda vec_hits: self._text(conn, query, vec_hits, plan),
            )

    def _text(
        self, conn, query: str, vec_hits: list[rank.Hit], plan: _Plan
    ) -> Either[list[Document], SearchFailure]:
        return bind(
            _map_err(
                pg.text_search(conn, query, plan.pool, self.project, self.since_hours),
                _pg_failure("text"),
            ),
            lambda txt_hits: self._feedback(conn, vec_hits, txt_hits, plan),
        )

    def _feedback(
        self, conn, vec_hits: list[rank.Hit], txt_hits: list[rank.Hit], plan: _Plan
    ) -> Either[list[Document], SearchFailure]:
        pool_paths = sorted({h.source_path for h in vec_hits + txt_hits})
        return bind(
            _map_err(pg.ranking_feedback_counts(conn, pool_paths), _pg_failure("feedback")),
            lambda feedback: self._score(conn, vec_hits, txt_hits, feedback, plan),
        )

    def _score(
        self,
        conn,
        vec_hits: list[rank.Hit],
        txt_hits: list[rank.Hit],
        feedback: dict[str, rank.Counts],
        plan: _Plan,
    ) -> Either[list[Document], SearchFailure]:
        scored = rank.within_budget(
            rank.merge_hits(vec_hits, txt_hits, feedback),
            plan.max_results,
            plan.max_chars,
        )
        return bind(
            _map_err(
                pg.rank_facts(conn, _paths(scored)),
                _pg_failure("rank_facts"),
            ),
            lambda facts: self._attach(conn, rank.order_within_set(scored, facts)),
        )

    def _attach(self, conn, ordered: list[rank.Scored]) -> Either[list[Document], SearchFailure]:
        return bind(
            _map_err(pg.consumption_counts(conn, _paths(ordered)), _pg_failure("consumption")),
            lambda consumption: self._said(
                conn, replace(_Attachment(ordered=ordered), consumption=consumption)
            ),
        )

    def _said(self, conn, attachment: _Attachment) -> Either[list[Document], SearchFailure]:
        return bind(
            _map_err(pg.said_by_owner_counts(conn, _paths(attachment.ordered)), _pg_failure("said_by_owner")),
            lambda said: self._superseded(conn, replace(attachment, said=said)),
        )

    def _superseded(self, conn, attachment: _Attachment) -> Either[list[Document], SearchFailure]:
        return bind(
            _map_err(pg.superseded_by(conn, _paths(attachment.ordered)), _pg_failure("superseded_by")),
            lambda superseded: self._claims(conn, replace(attachment, superseded=superseded)),
        )

    def _claims(self, conn, attachment: _Attachment) -> Either[list[Document], SearchFailure]:
        per_hit = min(max(self.claims, 0), SEARCH_MAX_CLAIMS)
        if per_hit <= 0:
            return Ok(self._documents(attachment))
        return bind(
            _map_err(
                pg.declared_claims(conn, _paths(attachment.ordered), per_hit),
                _pg_failure("claims"),
            ),
            lambda declared: Ok(self._documents(replace(attachment, declared=declared))),
        )

    def _documents(self, attachment: _Attachment) -> list[Document]:
        return [
            self._to_document(
                scored, attachment.consumption, attachment.said, attachment.superseded, attachment.declared
            )
            for scored in attachment.ordered
        ]

    def _to_document(self, scored, consumption, said, superseded, declared) -> Document:
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

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        # BaseRetriever 의 프레임워크 계약 — BoringRetriever 와 같이 실패는 빈 목록이 아니라 raise.
        match self.search(query):
            case Ok(documents):
                return documents
            case Err(failure):
                raise ConnectionError(f"python /search failed: {failure.detail}")
