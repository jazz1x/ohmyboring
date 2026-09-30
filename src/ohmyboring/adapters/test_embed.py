#!/usr/bin/env python3
"""embed.py — /embeddings 와이어 모양과 실패 값(ROP).

Run: python3 src/ohmyboring/adapters/test_embed.py   (no pytest dependency)

고정 변이: 주소 뒤에 /embeddings 붙이기·Bearer 넣기·Refused/Malformed/Unreachable 분기를
하나씩 빼면 여기 시험이 빨갛게 끝난다. 네트워크 없음 — 전송은 전부 가짜.
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ohmyboring.adapters import embed as embedder  # noqa: E402
from ohmyboring.result import Err, Ok  # noqa: E402


class _Reply:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _opener_ok(payload: dict, seen: dict):
    def opener(req, timeout):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.data)
        seen["timeout"] = timeout
        return _Reply(json.dumps(payload).encode("utf-8"))

    return opener


class EmbedTests(unittest.TestCase):
    def test_wire_shape_openai_embeddings(self):
        seen = {}
        opener = _opener_ok({"data": [{"embedding": [0.5, 1.0]}]}, seen)
        result = embedder.embed(
            "안녕", base_url="http://llm.test/v1/", model="bge-m3", api_key="", opener=opener
        )
        self.assertEqual(result, Ok([0.5, 1.0]))
        self.assertEqual(seen["url"], "http://llm.test/v1/embeddings")
        self.assertEqual(seen["body"], {"model": "bge-m3", "input": "안녕"})
        self.assertNotIn("Authorization", seen["headers"])

    def test_api_key_becomes_bearer(self):
        seen = {}
        opener = _opener_ok({"data": [{"embedding": [1]}]}, seen)
        embedder.embed("x", base_url="http://l/v1", api_key="tok", opener=opener)
        self.assertEqual(seen["headers"].get("Authorization"), "Bearer tok")

    def test_http_error_is_refused(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", hdrs=None, fp=None)

        result = embedder.embed("x", base_url="http://l/v1", api_key="", opener=opener)
        assert isinstance(result, Err), result
        self.assertIsInstance(result.error, embedder.Refused)
        self.assertEqual(result.error.status, 500)
        self.assertIn("500", str(result.error))

    def test_bad_json_is_malformed(self):
        result = embedder.embed(
            "x",
            base_url="http://l/v1",
            api_key="",
            opener=lambda req, timeout: _Reply(b"not json"),
        )
        assert isinstance(result, Err), result
        self.assertIsInstance(result.error, embedder.Malformed)

    def test_wrong_shape_is_malformed(self):
        result = embedder.embed(
            "x",
            base_url="http://l/v1",
            api_key="",
            opener=lambda req, timeout: _Reply(json.dumps({"data": []}).encode()),
        )
        assert isinstance(result, Err), result
        self.assertIsInstance(result.error, embedder.Malformed)

    def test_connection_failure_is_unreachable(self):
        def opener(req, timeout):
            raise OSError("connection refused")

        result = embedder.embed("x", base_url="http://l/v1", api_key="", opener=opener)
        assert isinstance(result, Err), result
        self.assertIsInstance(result.error, embedder.Unreachable)


if __name__ == "__main__":
    unittest.main(verbosity=2)
