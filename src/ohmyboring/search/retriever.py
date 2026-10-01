"""PgRetriever — LangChain BaseRetriever over the python ranking pipeline (문의 /search).

질의 → list[Document]. metadata 키는 agents/memory/retriever.py:_hit_to_document 가 읽는
이름과 같다 — 문 소비자(아침 카드 후보 기록·Deep Agent recall)가 바꿔 끼우지 않고 받는다.
실패는 Either 값으로 흐르고 이 모듈의 match 들이 접는다 — BaseRetriever 계약만 예외로 접는다
(빈 목록이 아니라 raise).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from ohmyboring.adapters import embed as embed_adapter
from ohmyboring.result import Either, Err, Ok
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
    """search() 이 미리 계산해 _search 에 넘기는 수치들 — 클램프 뒤의 확정값."""

    pool: int
    max_results: int
    max_chars: int


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
        max_chars = max_tokens * 4
        if max_results == 0 or max_chars == 0:
            return Ok([])  # retrieve.rs:282 — 빈 결과 조기 반환
        pool = rank.pool_size(max_results)
        vector = embed_adapter.embed(query)
        match vector:
            case Err(failure):
                return Err(SearchFailure(f"embed: {failure}"))
        connected = pg.connect(self.dsn)
        match connected:
            case Err(failure):
                return Err(SearchFailure(f"store: {failure.detail}"))
        with contextlib.closing(connected.value):
            plan = _Plan(pool=pool, max_results=max_results, max_chars=max_chars)
            return self._search(connected.value, query, vector.value, plan)

    def _search(
        self,
        conn,
        query: str,
        vector: list[float],
        plan: _Plan,
    ) -> Either[list[Document], SearchFailure]:
        vec_hits = pg.vector_search(conn, vector, plan.pool, self.project, self.since_hours)
        match vec_hits:
            case Err(failure):
                return Err(SearchFailure(f"vector: {failure.detail}"))
        txt_hits = pg.text_search(conn, query, plan.pool, self.project, self.since_hours)
        match txt_hits:
            case Err(failure):
                return Err(SearchFailure(f"text: {failure.detail}"))
        pool_paths = sorted({h.source_path for h in vec_hits.value + txt_hits.value})
        feedback = pg.ranking_feedback_counts(conn, pool_paths)
        match feedback:
            case Err(failure):
                return Err(SearchFailure(f"feedback: {failure.detail}"))
        scored = rank.within_budget(
            rank.merge_hits(vec_hits.value, txt_hits.value, feedback.value),
            plan.max_results,
            plan.max_chars,
        )
        facts = pg.rank_facts(conn, [s.hit.source_path for s in scored])
        match facts:
            case Err(failure):
                return Err(SearchFailure(f"rank_facts: {failure.detail}"))
        ordered = rank.order_within_set(scored, facts.value)
        return self._attach(conn, ordered)

    def _attach(self, conn, ordered: list[rank.Scored]) -> Either[list[Document], SearchFailure]:
        paths = [scored.hit.source_path for scored in ordered]
        consumption = pg.consumption_counts(conn, paths)
        match consumption:
            case Err(failure):
                return Err(SearchFailure(f"consumption: {failure.detail}"))
        said = pg.said_by_owner_counts(conn, paths)
        match said:
            case Err(failure):
                return Err(SearchFailure(f"said_by_owner: {failure.detail}"))
        superseded = pg.superseded_by(conn, paths)
        match superseded:
            case Err(failure):
                return Err(SearchFailure(f"superseded_by: {failure.detail}"))
        declared: dict[str, pg.RegisterRows] = {}
        per_hit = min(max(self.claims, 0), SEARCH_MAX_CLAIMS)
        if per_hit > 0:
            claims = pg.declared_claims(conn, paths, per_hit)
            match claims:
                case Err(failure):
                    return Err(SearchFailure(f"claims: {failure.detail}"))
            declared = claims.value
        return Ok(
            [
                self._to_document(scored, consumption.value, said.value, superseded.value, declared)
                for scored in ordered
            ]
        )

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
