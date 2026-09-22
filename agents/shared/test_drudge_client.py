#!/usr/bin/env python3
"""Guardrail: the collector write-door preflight (check_drudge_writable).

Owns one question — does a collector refuse to distill when drudge cannot store the
result? Getting this wrong either burns an LLM pass per cycle on input that cannot be
written (the 2026-07-25 failure mode) or, in the other direction, blocks ingestion on a
healthy wiki-first engine that simply has no DB to report on.
"""

from __future__ import annotations

import os
import sys
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from drudge_client import (  # noqa: E402
    DrudgeClient,
    DrudgeNotWritableError,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
