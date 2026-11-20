"""문의 claim 읽기가 backend=surreal 에서 Surreal 만 읽는다(C-SURALL2 Z3, 끝 조건 z).

가짜 Surreal(httpx.MockTransport)을 `door._surreal_client` 자리에 꽂고 DOOR_PG_DSN 을 비운 채 라우트 핸들러를
그대로 부른다. 고치기 전에는 문이 pg 를 읽으려 해 503(store not configured)이 났다 — 이 시험이 그 자리에서 빨갛다.
봉인 행이 답에 안 나오는 것은 가짜 서버가 `superseded_us = NONE` 을 질의문에서 보고 봉인 행을 빼는 것으로 본다."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

door = importlib.import_module("agents.door.door")
from ohmyboring.store import surreal  # noqa: E402

SURREAL_CFG = {"vector": {"backend": "surreal"}}
SETTINGS = surreal.SurrealSettings("http://surreal.test", "boring", "boring", "root", "pw")

#: subject "s" 의 claim 두 줄: /open.md 는 열림, /sealed.md 는 봉인(superseded_us 있음).
CLAIM_ROWS = [
    {"subject": "s", "predicate": "p", "value": "open", "source_path": "/open.md", "valid_from_us": 2_000_000,
     "superseded_us": None, "kind": "rule"},
    {"subject": "s", "predicate": "p", "value": "sealed", "source_path": "/sealed.md", "valid_from_us": 1_000_000,
     "superseded_us": 1_500_000, "kind": "rule"},
]  # fmt: skip


def _fake_surreal(seen: list[str]) -> surreal.SurrealClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/signin":
            return httpx.Response(200, json={"token": "t"})
        sql = json.loads(request.content)["params"][0]
        seen.append(sql)
        if "FROM document" in sql:
            rows = [{"source_path": "/open.md", "author": "owner"}]
        else:
            rows = [r for r in CLAIM_ROWS if "superseded_us = NONE" not in sql or r["superseded_us"] is None]
        return httpx.Response(200, json={"result": [{"status": "OK", "result": rows}]})

    return surreal.SurrealClient(SETTINGS, transport=httpx.MockTransport(handle))


def _get(handler, path: str, query: str):
    scope = {"type": "http", "method": "GET", "path": path, "query_string": query.encode(), "headers": []}
    return asyncio.run(handler(Request(scope)))


class SurrealClaimReads(unittest.TestCase):
    def setUp(self) -> None:
        self.seen: list[str] = []
        for patcher in (
            mock.patch.dict(os.environ, {"BORING_SURREAL_PASSWORD": "pw"}),
            mock.patch.object(door.boring_config, "load", return_value=SURREAL_CFG),
            mock.patch.object(door, "_surreal_client", lambda settings: _fake_surreal(self.seen)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("DOOR_PG_DSN", None)  # 위 dict 패치가 끝나면 원래 값으로 돌아간다

    def test_claim_source_answers_from_surreal_without_a_pg_dsn_and_skips_the_sealed_row(self) -> None:
        response = _get(door._claim_source, "/claim-source", "subject=s")
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual((payload["note"], payload["value"]), ("/open.md", "open"))
        self.assertEqual([c["note"] for c in payload["candidates"]], ["/open.md"])
        self.assertTrue(any("superseded_us = NONE" in sql for sql in self.seen))

    def test_claim_sources_lists_from_surreal(self) -> None:
        response = _get(door._claim_sources, "/claim-sources", "predicate=p")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([c["note"] for c in json.loads(response.body)["claims"]], ["/open.md"])

    def test_split_subject_rows_come_from_surreal(self) -> None:
        self.assertEqual(door._fetch_split_subject_rows(), [("s", "/open.md")])

    def test_a_surreal_failure_is_a_502_not_a_pg_read(self) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        broken = surreal.SurrealClient(SETTINGS, transport=httpx.MockTransport(down))
        with mock.patch.object(door, "_surreal_client", lambda settings: broken):
            response = _get(door._claim_source, "/claim-source", "subject=s")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(json.loads(response.body)["error"], "store unreachable")

    def test_surreal_without_a_password_is_a_503_naming_the_env(self) -> None:
        os.environ.pop("BORING_SURREAL_PASSWORD", None)
        response = _get(door._claim_source, "/claim-source", "subject=s")
        self.assertEqual(response.status_code, 503)
        self.assertIn("BORING_SURREAL_PASSWORD", json.loads(response.body)["detail"])


class PgBackendUnchanged(unittest.TestCase):
    def test_pg_backend_without_a_dsn_is_still_503(self) -> None:
        with (
            mock.patch.object(door.boring_config, "load", return_value={"vector": {"backend": "pgvector"}}),
            mock.patch.dict("os.environ", clear=False) as env,
        ):
            env.pop("DOOR_PG_DSN", None)
            response = _get(door._claim_source, "/claim-source", "subject=s")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body), {"error": "store not configured"})


if __name__ == "__main__":
    unittest.main()
