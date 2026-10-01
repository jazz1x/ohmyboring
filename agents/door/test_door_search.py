#!/usr/bin/env python3
"""문의 POST /search 핸들러 시험 — FastAPI TestClient, DB·임베딩은 문 경계를 스텁.

Cover:
  (a) 정상 → 200, hit 모양(BoringRetriever 가 읽는 키), 헤더 x-boring-search: python
  (b) related=1 → 기존 프록시로 넘김 (x-boring-search 없음), related=0 은 파이썬 경로
  (c) 임베딩 Err → 502 JSON {error} — 빈 hits 200 금지
  (d) 판정 카운트 Err → 502 JSON
  (e) session_id → handover 호출, 그 실패도 502
  (f) 클램프·400 — max_results 상한 50, 음수·문자열 400, query 없음 400
  (g) query_log 이 search 결과를 한 번 남긴다 (스텁이 받은 인자로 확인)
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from fastapi.testclient import TestClient  # noqa: E402

from ohmyboring.result import Err, Ok  # noqa: E402
from ohmyboring.search import retriever as search_retriever  # noqa: E402

door = importlib.import_module("agents.door.door")


def document(doc_id: str = "/w.md#0", path: str = "/w.md", **metadata_overrides):
    from langchain_core.documents import Document

    metadata = {
        "source_path": path,
        "project": "omb",
        "origin": "personal",
        "used_count": 1,
        "contested_count": 0,
        "said_by_owner": 2,
        "superseded_by": [],
        "dist": 0.3,
        "dist_kind": "vector_cosine",
    }
    metadata.update(metadata_overrides)
    return Document(id=doc_id, page_content="조각 본문", metadata=metadata)


class SearchRouteTests(unittest.TestCase):
    def setUp(self):
        self._saved_dsn = os.environ.get("DOOR_PG_DSN")
        os.environ["DOOR_PG_DSN"] = "postgresql://boring:boring@127.0.0.1:9/none"
        self.client = TestClient(door.app)
        self.made: list[dict] = []
        self.logged: list[tuple] = []

        def fake_retriever(**kwargs):
            self.made.append(kwargs)
            return mock.MagicMock()

        patchers = [
            mock.patch.object(door.search_retriever, "PgRetriever", side_effect=fake_retriever),
            mock.patch.object(door, "_search_execute", side_effect=self._execute),
            mock.patch.object(door, "_search_handover", side_effect=self._handover),
            mock.patch.object(door, "_search_log_query", side_effect=self._log_query),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        if self._saved_dsn is None:
            os.environ.pop("DOOR_PG_DSN", None)
        else:
            os.environ["DOOR_PG_DSN"] = self._saved_dsn

    # -- 스텁의 기본 동작 (각 시험이 필요하면 다시 patch) --
    def _execute(self, retriever, query):
        return Ok([document()])

    def _handover(self, session_id, paths):
        return Ok(object())

    def _log_query(self, query, hits, started):
        self.logged.append((query, hits, started))

    def post(self, payload):
        return self.client.post(
            "/search",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    def test_happy_path_shape_and_header(self):
        response = self.post({"query": "훅 등록", "max_results": 3, "claims": 3})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("x-boring-search"), "python")
        hits = response.json()["hits"]
        self.assertEqual(len(hits), 1)
        for key in (
            "id",
            "origin",
            "project",
            "source_path",
            "snippet",
            "dist",
            "dist_kind",
            "used_count",
            "contested_count",
            "said_by_owner",
        ):
            self.assertIn(key, hits[0])
        # 문 소비자 계약 — agents/memory/retriever.py:_hit_to_document 가 읽는 키.
        self.assertEqual(hits[0]["used_count"], 1)
        self.assertEqual(hits[0]["said_by_owner"], 2)
        # 클램프와 기본값이 그대로 리트리버에 닿는다.
        self.assertEqual(self.made[-1]["max_results"], 3)
        self.assertEqual(self.made[-1]["claims"], 3)
        self.assertEqual(self.made[-1]["max_tokens"], 2000, "기본 max_tokens")
        self.assertEqual(self.made[-1]["project"], None)

    def test_claims_requested_controls_claims_total(self):
        response = self.post({"query": "q", "claims": 3})
        self.assertIn("claims_total", response.json()["hits"][0])
        response = self.post({"query": "q"})
        self.assertNotIn("claims_total", response.json()["hits"][0])

    def test_related_positive_proxies_without_header(self):
        with mock.patch.object(door, "_proxy", new=mock.AsyncMock(return_value=door.Response(content=b"up"))):
            response = self.post({"query": "q", "related": 1})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("x-boring-search"), "프록시로 넘긴 경우엔 헤더가 없다")
        self.assertEqual(self.made, [], "프록시 경로는 리트리버를 만들지 않는다")

    def test_related_zero_answers_in_python(self):
        response = self.post({"query": "q", "related": 0})
        self.assertEqual(response.headers.get("x-boring-search"), "python")
        self.assertEqual(len(self.made), 1)

    def test_embed_failure_is_502_json_not_empty_200(self):
        with mock.patch.object(
            door,
            "_search_execute",
            return_value=Err(search_retriever.SearchFailure("embed: unreachable")),
        ):
            response = self.post({"query": "q"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("error", response.json())
        self.assertNotEqual(response.json().get("hits"), [])

    def test_feedback_count_failure_is_502_json(self):
        with mock.patch.object(
            door,
            "_search_execute",
            return_value=Err(search_retriever.SearchFailure("feedback: db down")),
        ):
            response = self.post({"query": "q"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("feedback", response.json()["error"])

    def test_handover_called_with_shown_paths_and_its_failure_is_502(self):
        with (
            mock.patch.object(
                door,
                "_search_execute",
                return_value=Ok([document(path="/a.md"), document(doc_id="/b.md#0", path="/b.md")]),
            ) as _exec,
            mock.patch.object(door, "_search_handover", side_effect=self._handover) as handover,
        ):
            response = self.post({"query": "q", "session_id": "  s-1  "})
        self.assertEqual(response.status_code, 200)
        handover.assert_called_once_with("s-1", ["/a.md", "/b.md"])
        with mock.patch.object(door, "_search_handover", return_value=Err("disk full")):
            response = self.post({"query": "q", "session_id": "s-1"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("handover", response.json()["error"])

    def test_blank_session_id_skips_handover(self):
        with mock.patch.object(door, "_search_handover") as handover:
            response = self.post({"query": "q", "session_id": "   "})
        self.assertEqual(response.status_code, 200)
        handover.assert_not_called()

    def test_query_log_receives_search_rows(self):
        self.post({"query": "훅 등록", "max_results": 3})
        self.assertEqual(len(self.logged), 1)
        query, hits, _started = self.logged[0]
        self.assertEqual(query, "훅 등록")
        self.assertEqual(hits[0]["source_path"], "/w.md")
        self.assertEqual(hits[0]["dist"], 0.3)

    def test_clamps_and_validation(self):
        self.post({"query": "q", "max_results": 999, "max_tokens": 999_999, "claims": 99})
        self.assertEqual(self.made[-1]["max_results"], 50)
        self.assertEqual(self.made[-1]["max_tokens"], 16_384)
        self.assertEqual(self.made[-1]["claims"], 10)
        for bad in (-1, "3", True):
            response = self.post({"query": "q", "max_results": bad})
            self.assertEqual(response.status_code, 400, f"max_results={bad!r}")
            self.assertIn("max_results", response.json()["error"])
        self.assertEqual(self.post({}).status_code, 400)
        self.assertEqual(self.post({"query": "  "}).status_code, 400)
        self.assertEqual(self.post({"query": "q", "related": -1}).status_code, 400)
        self.assertEqual(self.post({"query": "q", "project": 5}).status_code, 400)

    def test_no_dsn_is_503(self):
        os.environ.pop("DOOR_PG_DSN", None)
        response = self.post({"query": "q"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "store not configured"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
