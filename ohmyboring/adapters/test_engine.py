#!/usr/bin/env python3
"""엔진에 나가는 쪽 한 벌 — 쓰기 문지기(check_drudge_writable)와 요청 JSON 의 동일성을 못 박는다.

Run: python3 ohmyboring/adapters/test_engine.py   (no pytest dependency)

Owns one question — does a collector refuse to distill when drudge cannot store the
result? Getting this wrong either burns an LLM pass per cycle on input that cannot be
written (the 2026-07-25 failure mode) or, in the other direction, blocks ingestion on a
healthy wiki-first engine that simply has no DB to report on.

The wire tests pin the JSON: search/consumption/remember 에서 선택 인자를 요청 타입
(SearchKnobs·ConsumptionMarks·NoteProvenance)으로 묶은 뒤에도 엔진이 받는 본문이 옛
호출 모양과 바이트 그대로 같아야 한다 — 재시도·타임아웃·예외 종류는 그대로이고 묶음만
바뀌었으니까.

Mutation targets: search 가 related=0 일 때 related 키를 넣는 변이, verdict 옆의 used/
contested 를 조용히 떨구는 변이, remember 의 기본 origin 을 바꾸는 변이 각각 시험으로
사망 확인.
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "agents" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ohmyboring.adapters.engine import (  # noqa: E402
    ConsumptionMarks,
    DrudgeClient,
    DrudgeNotWritableError,
    NoteProvenance,
    SearchKnobs,
    check_drudge_writable,
)


class _FakeClient:
    """Stands in for DrudgeClient.health() without any HTTP."""

    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error

    def health(self):
        if self._error is not None:
            raise self._error
        return self._payload


class CheckDrudgeWritableTest(unittest.TestCase):
    def test_blocks_when_db_healthy_is_false(self):
        client = _FakeClient({"status": "degraded", "vector": True, "db_healthy": False})
        with self.assertRaises(DrudgeNotWritableError) as ctx:
            check_drudge_writable(client)
        self.assertIn("db_healthy=false", str(ctx.exception))

    def test_blocks_on_degraded_status_even_if_flag_is_true(self):
        # Defence in depth: status is the engine's own summary of the same probe.
        client = _FakeClient({"status": "degraded", "vector": True, "db_healthy": True})
        with self.assertRaises(DrudgeNotWritableError):
            check_drudge_writable(client)

    def test_allows_healthy_engine(self):
        client = _FakeClient({"status": "ok", "vector": True, "sync": "idle", "db_healthy": True})
        check_drudge_writable(client)  # must not raise

    def test_allows_response_without_db_healthy(self):
        # Wiki-first engine, or a build older than the liveness probe. Absence of the
        # field is not evidence of failure, so ingestion must continue.
        client = _FakeClient({"status": "ok", "vector": False, "sync": "idle"})
        check_drudge_writable(client)  # must not raise

    def test_allows_degraded_status_without_db_healthy_field(self):
        # "degraded" only means the write door when db_healthy is the reason for it.
        client = _FakeClient({"status": "degraded", "vector": False})
        check_drudge_writable(client)  # must not raise

    def test_blocks_when_health_is_unreachable(self):
        client = _FakeClient(error=OSError("connection refused"))
        with self.assertRaises(DrudgeNotWritableError) as ctx:
            check_drudge_writable(client)
        self.assertIn("unreachable", str(ctx.exception))

    def test_defaults_to_a_real_client_when_none_is_given(self):
        payload = {"status": "ok", "vector": True, "db_healthy": True}
        with mock.patch.object(DrudgeClient, "health", return_value=payload) as health:
            check_drudge_writable()
        health.assert_called_once()


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b'{"ok": true}'


class SyncTimeoutTest(unittest.TestCase):
    def test_sync_passes_explicit_timeout_to_urlopen(self):
        seen = {}

        def fake_urlopen(req, timeout):
            seen["timeout"] = timeout
            return _FakeResponse()

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            DrudgeClient(base_url="http://127.0.0.1:9").sync(timeout=123.0)

        self.assertEqual(seen["timeout"], 123.0)


class _Wire:
    """Replaces _retry so the payload the engine would receive is captured, not sent."""

    def __init__(self, client: DrudgeClient):
        self.sent: list[dict] = []
        client._retry = self._retry  # noqa: SLF001 — the wire test owns the client's back door

    def _retry(self, method, path, payload=None, timeout=None):
        self.sent.append({"method": method, "path": path, "payload": payload})
        return {}

    def body(self, index: int = 0) -> str:
        """The request body as serialized text — key order pinned, not just dict equality.
        ensure_ascii=False 로 적어 한글 값을 읽는 대로 적는다(실제 와이어는 ascii escape 이지만
        이 시험이 고치는 것은 키 순서와 값이지 직렬화 인코딩이 아니다)."""
        return json.dumps(self.sent[index]["payload"], ensure_ascii=False)


class SearchWireTest(unittest.TestCase):
    def test_default_knobs_send_the_same_body_as_the_old_defaults(self):
        client = DrudgeClient(base_url="http://drudge.test", retries=0)
        wire = _Wire(client)
        client.search("pool")
        self.assertEqual(
            wire.body(),
            '{"query": "pool", "max_results": 3, "max_tokens": 1500}',
        )
        self.assertEqual(wire.sent[0]["method"], "POST")
        self.assertEqual(wire.sent[0]["path"], "/search")

    def test_full_knobs_send_the_same_body_as_the_old_keywords(self):
        client = DrudgeClient(base_url="http://drudge.test", retries=0)
        wire = _Wire(client)
        client.search(
            "pool",
            SearchKnobs(max_results=5, max_tokens=900, related=1, related_heads=5, claims=2),
        )
        self.assertEqual(
            wire.body(),
            '{"query": "pool", "max_results": 5, "max_tokens": 900, '
            '"related": 1, "related_heads": 5, "claims": 2}',
        )


class ConsumptionWireTest(unittest.TestCase):
    def test_path_lists_send_the_same_body_as_the_old_keywords(self):
        client = DrudgeClient(base_url="http://drudge.test", retries=0)
        wire = _Wire(client)
        client.consumption(
            "s1",
            "2026-09-21T00:00:00+00:00",
            ConsumptionMarks(
                used=["/vault/wiki/a.md"],
                contested=["/vault/wiki/b.md"],
                supersedes=[["/vault/wiki/a.md", "/vault/wiki/b.md"]],
                judge="inferred",
            ),
        )
        self.assertEqual(
            wire.body(),
            '{"session_id": "s1", "observed_at": "2026-09-21T00:00:00+00:00", '
            '"judge": "inferred", "used": ["/vault/wiki/a.md"], "contested": ["/vault/wiki/b.md"], '
            '"supersedes": [["/vault/wiki/a.md", "/vault/wiki/b.md"]]}',
        )
        self.assertEqual(wire.sent[0]["path"], "/consumption")

    def test_a_verdict_travels_alone_like_the_old_keyword(self):
        client = DrudgeClient(base_url="http://drudge.test", retries=0)
        wire = _Wire(client)
        client.consumption("s1", "t", ConsumptionMarks(verdict="used"))
        self.assertEqual(
            wire.body(),
            '{"session_id": "s1", "observed_at": "t", "verdict": "used"}',
        )

    def test_a_verdict_beside_path_lists_is_refused_before_anything_is_sent(self):
        client = DrudgeClient(base_url="http://drudge.test", retries=0)
        wire = _Wire(client)
        with self.assertRaises(ValueError):
            client.consumption(
                "s1",
                "t",
                ConsumptionMarks(used=["/vault/wiki/a.md"], verdict="used"),
            )
        self.assertEqual(wire.sent, [], "nothing may reach the engine when the call is ambiguous")


class RememberWireTest(unittest.TestCase):
    def test_full_provenance_sends_the_same_body_as_the_old_keywords(self):
        client = DrudgeClient(base_url="http://drudge.test", retries=0)
        wire = _Wire(client)
        client.remember(
            "제목",
            "본문",
            NoteProvenance(
                tags=["correction", "slack"],
                supersedes=["/vault/wiki/old.md"],
                origin="work",
                repo="oh-my-boring",
                author="owner",
                judge="owner",
            ),
        )
        self.assertEqual(
            wire.body(),
            '{"title": "제목", "body": "본문", "origin": "work", '
            '"tags": ["correction", "slack"], "supersedes": ["/vault/wiki/old.md"], '
            '"repo": "oh-my-boring", "author": "owner", "judge": "owner"}',
        )
        self.assertEqual(wire.sent[0]["path"], "/remember")

    def test_default_provenance_sends_the_same_body_as_the_old_defaults(self):
        client = DrudgeClient(base_url="http://drudge.test", retries=0)
        wire = _Wire(client)
        client.remember("제목", "본문")
        self.assertEqual(
            wire.body(),
            '{"title": "제목", "body": "본문", "origin": "personal"}',
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
