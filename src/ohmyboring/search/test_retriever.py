#!/usr/bin/env python3
"""retriever.py 시험 — pg·임베딩을 모듈 경계에서 스텁하고 파이프라인을 본다. DB 없음.

Run: python3 ohmyboring/search/test_retriever.py   (no pytest dependency)

Mutation targets: 임베딩 Err 에 빈 hits 200 을 내는 변이는 문 핸들러 시험
(agents/door/test_door_search.py)에서 사망 확인 — 여기선 Either 흐름과 Document metadata
계약을 못 박는다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ohmyboring.result import Err, Ok  # noqa: E402
from ohmyboring.search import pg, rank, retriever  # noqa: E402

DSN = "postgresql://boring:boring@127.0.0.1:9/none"


def hit(chunk_id: str, path: str, dist: float, kind: str = "vector_cosine") -> rank.Hit:
    return rank.Hit(
        id=chunk_id,
        content="조각 " + chunk_id,
        origin="personal",
        project="omb",
        source_path=path,
        dist=dist,
        dist_kind=kind,
    )


class RetrieverPipelineTests(unittest.TestCase):
    """스텁 세계 — 벡터 목록 [A,B], 어휘 목록 [C]. A/B 는 같은 문서."""

    def setUp(self):
        self.declared = mock.MagicMock(
            return_value=Ok(
                {
                    "/a.md": pg.RegisterRows(
                        rows=(
                            pg.RegisterRow(
                                node_id="claim:s:p",
                                subject="s",
                                predicate="p",
                                value="v",
                                kind="decision",
                                confidence="certain",
                                valid_from="2026-09-01T00:00:00+00:00",
                                project="omb",
                            ),
                        ),
                        total_matching=2,
                    )
                }
            )
        )
        patcher = mock.patch.multiple(
            retriever.pg,
            connect=lambda _dsn: Ok(mock.MagicMock()),
            vector_search=lambda conn, vec, k, project, since: Ok(
                [hit("a#0", "/a.md", 0.1), hit("b#0", "/b.md", 0.2)]
            ),
            text_search=lambda conn, q, k, project, since: Ok([hit("c#0", "/c.md", 1.5, "text_rank")]),
            ranking_feedback_counts=lambda conn, paths: Ok({"/a.md": rank.Counts(used=1)}),
            consumption_counts=lambda conn, paths: Ok({"/a.md": rank.Counts(used=1, contested=2)}),
            said_by_owner_counts=lambda conn, paths: Ok({"/a.md": 1}),
            superseded_by=lambda conn, paths: Ok({"/b.md": ["/b2.md"]}),
            declared_claims=self.declared,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        embed_patcher = mock.patch.object(retriever.embed_adapter, "embed", return_value=Ok([0.1] * 1024))
        embed_patcher.start()
        self.addCleanup(embed_patcher.stop)

    def test_search_builds_documents_in_scored_order(self):
        result = retriever.PgRetriever(dsn=DSN, max_results=3, max_tokens=2000).search("q")
        match result:
            case Err(failure):
                self.fail(f"search: {failure}")
            case Ok(documents):
                pass
        # RRF + 판정: a#0 = 1/61+한 표 > c#0 = 1/61(어휘 1위) > b#0 = 1/62(벡터 2위).
        self.assertEqual([d.id for d in documents], ["a#0", "c#0", "b#0"])
        first = documents[0]
        self.assertEqual(first.page_content, "조각 a#0")
        self.assertEqual(
            first.metadata["used_count"], 1, "attach 된 consumption 표시를 읽는다 (ranking 카운트와 별개)"
        )
        self.assertEqual(first.metadata["contested_count"], 2)
        self.assertEqual(first.metadata["said_by_owner"], 1)
        self.assertEqual(first.metadata["dist"], 0.1)
        self.assertEqual(first.metadata["dist_kind"], "vector_cosine")

    def test_feedback_counts_flip_document_order(self):
        """판정 카운트가 돌려주는 순서까지 닿는지 — 배선 시험 (wiki-2539).

        벡터 [A,B]·어휘 [] 고정, ranking_feedback_counts 만 {} ↔ B 경로 used=3 으로
        바꿔 끼운다: {} 면 A,B — used=3 이면 B,A. 읽은 카운트를 무시하는 변이는
        이 시험에서만 드러난다 (test_store_read_failure_is_err 는 실패 경로만 본다).
        """
        hits = [hit("a#0", "/a.md", 0.1), hit("b#0", "/b.md", 0.2)]
        with (
            mock.patch.object(retriever.pg, "vector_search", return_value=Ok(hits)),
            mock.patch.object(retriever.pg, "text_search", return_value=Ok([])),
            mock.patch.object(retriever.pg, "ranking_feedback_counts", return_value=Ok({})),
        ):
            match retriever.PgRetriever(dsn=DSN).search("q"):
                case Err(failure):
                    self.fail(f"search: {failure}")
                case Ok(documents):
                    pass
            self.assertEqual([d.id for d in documents], ["a#0", "b#0"])
        with (
            mock.patch.object(retriever.pg, "vector_search", return_value=Ok(hits)),
            mock.patch.object(retriever.pg, "text_search", return_value=Ok([])),
            mock.patch.object(
                retriever.pg,
                "ranking_feedback_counts",
                return_value=Ok({"/b.md": rank.Counts(used=3)}),
            ),
        ):
            match retriever.PgRetriever(dsn=DSN).search("q"):
                case Err(failure):
                    self.fail(f"search: {failure}")
                case Ok(documents):
                    pass
            self.assertEqual([d.id for d in documents], ["b#0", "a#0"])

    def test_metadata_keys_match_boring_retriever_contract(self):
        match retriever.PgRetriever(dsn=DSN).search("q"):
            case Err(failure):
                self.fail(f"search: {failure}")
            case Ok(documents):
                pass
        for key in (
            "source_path",
            "project",
            "origin",
            "used_count",
            "contested_count",
            "said_by_owner",
            "superseded_by",
            "dist",
            "dist_kind",
        ):
            self.assertIn(key, documents[0].metadata)
        self.assertEqual(documents[2].metadata["superseded_by"], ["/b2.md"])

    def test_claims_attached_only_when_requested(self):
        with_claims = retriever.PgRetriever(dsn=DSN, claims=3).search("q")
        match with_claims:
            case Ok(documents):
                pass
            case Err(failure):
                self.fail(f"search: {failure}")
        self.assertEqual(len(documents[0].metadata["claims"]), 1)
        self.assertEqual(documents[0].metadata["claims_total"], 2)
        without = retriever.PgRetriever(dsn=DSN).search("q")
        match without:
            case Ok(documents):
                pass
            case Err(failure):
                self.fail(f"search: {failure}")
        self.assertNotIn("claims", documents[0].metadata)
        self.assertNotIn("claims_total", documents[0].metadata)
        self.assertEqual(self.declared.call_count, 1, "claims=0 이면 declared_claims 질의도 안 탄다")

    def test_embed_failure_is_err(self):
        with mock.patch.object(retriever.embed_adapter, "embed", return_value=Err(_Failure("boom"))):
            result = retriever.PgRetriever(dsn=DSN).search("q")
        match result:
            case Err(failure):
                self.assertIn("embed", failure.detail)
            case Ok(_):
                self.fail("임베딩 실패는 Err — 어휘 목록만으로 답하지 않는다")

    def test_store_read_failure_is_err(self):
        with mock.patch.object(
            retriever.pg, "ranking_feedback_counts", return_value=Err(pg.PgError("db down"))
        ):
            result = retriever.PgRetriever(dsn=DSN).search("q")
        match result:
            case Err(failure):
                self.assertIn("feedback", failure.detail)
            case Ok(_):
                self.fail("판정 읽기 실패는 Err — 조용한 폭락(판정 없는 순위)은 계약 밖이다")

    def test_text_search_failure_short_circuits_and_closes_connection(self):
        """어휘 질의 Err — 뒤 단계는 부르지 않고 접속은 닫는다 (단락 시험, wiki-2539)."""
        conn = mock.MagicMock()
        feedback = mock.MagicMock(return_value=Ok({}))
        facts = mock.MagicMock(return_value=Ok({}))
        consumption = mock.MagicMock(return_value=Ok({}))
        with (
            mock.patch.object(retriever.pg, "connect", return_value=Ok(conn)),
            mock.patch.object(retriever.pg, "text_search", return_value=Err(pg.PgError("lex down"))),
            mock.patch.object(retriever.pg, "ranking_feedback_counts", feedback),
            mock.patch.object(retriever.pg, "rank_facts", facts),
            mock.patch.object(retriever.pg, "consumption_counts", consumption),
        ):
            result = retriever.PgRetriever(dsn=DSN).search("q")
        match result:
            case Err(failure):
                self.assertTrue(failure.detail.startswith("text: "), failure.detail)
            case Ok(_):
                self.fail("어휘 질의 실패는 Err — 뒤 단계로 흘러가면 안 된다")
        feedback.assert_not_called()
        facts.assert_not_called()
        consumption.assert_not_called()
        conn.close.assert_called_once_with()

    def test_clamps(self):
        r = retriever.PgRetriever(dsn=DSN, max_results=999, max_tokens=999_999, claims=99)
        match r.search("q"):
            case Err(failure):
                self.fail(f"search: {failure}")
            case Ok(documents):
                pass
        # max_results 50 클램프 → 풀 200, 예산 넉넉 — 전부 살아 있으면 클램프가 먹힌 것.
        self.assertEqual(len(documents), 3)
        with_claims = retriever.PgRetriever(dsn=DSN, claims=99).search("q")
        match with_claims:
            case Ok(documents):
                pass
            case Err(failure):
                self.fail(f"search: {failure}")
        self.assertEqual(documents[0].metadata["claims_total"], 2, "claims 클램프 10 안쪽")

    def test_get_relevant_documents_raises_on_failure(self):
        with mock.patch.object(retriever.embed_adapter, "embed", return_value=Err(_Failure("boom"))):
            with self.assertRaises(ConnectionError):
                retriever.PgRetriever(dsn=DSN).invoke("q")


class _Failure:
    def __init__(self, detail: str):
        self.detail = detail

    def __str__(self):
        return self.detail


if __name__ == "__main__":
    unittest.main(verbosity=2)
