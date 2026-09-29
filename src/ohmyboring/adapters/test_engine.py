#!/usr/bin/env python3
"""엔진에 나가는 쪽 한 벌 — 쓰기 문지기(check_drudge_writable)와 요청 JSON 의 동일성을 못 박는다.

Run: python3 ohmyboring/adapters/test_engine.py   (no pytest dependency)

Owns one question — does a collector refuse to distill when drudge cannot store the
result? Getting this wrong either burns an LLM pass per cycle on input that cannot be
written (the 2026-07-25 failure mode) or, in the other direction, blocks ingestion on a
healthy wiki-first engine that simply has no DB to report on.

The wire tests pin the JSON: search/consumption/remember 에서 선택 인자를 요청 타입
(SearchKnobs·ConsumptionMarks·NoteProvenance)으로 묶은 뒤에도 엔진이 받는 본문이 옛
호출 모양과 바이트 그대로 같아야 한다 — 재시도·타임아웃은 그대로이고 묶음만 바뀌었으니까.
`ConsumptionMarks` 는 Verdict|PathMarks 합 타입이라 verdict 옆의 목록 동행은 호출 모양이
없어졌다(구조가 지킨다 — 엔진에 가기 전에 타입이 막는다).

Mutation targets: search 가 related=0 일 때 related 키를 넣는 변이, verdict 옆의 목록을
조용히 떨구는 변이, remember 의 기본 origin 을 바꾸는 변이, 실패를 Either 대신 예외로
던지는 변이 각각 시험으로 사망 확인.
"""

from __future__ import annotations

import http.client
import io
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "src", ROOT / "agents" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ohmyboring.adapters import engine  # noqa: E402
from ohmyboring.adapters.engine import (  # noqa: E402
    ConsumptionMarks,
    DrudgeClient,
    Malformed,
    NoteProvenance,
    NotWritable,
    PathMarks,
    Refused,
    SearchKnobs,
    Unreachable,
    Verdict,
    check_drudge_writable,
)
from ohmyboring.result import Err, Ok  # noqa: E402


class _FakeClient:
    """Stands in for DrudgeClient.health() without any HTTP — Either 로 답한다."""

    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error

    def health(self):
        if self._error is not None:
            return Err(self._error)
        return Ok(self._payload)


class CheckDrudgeWritableTest(unittest.TestCase):
    def test_blocks_when_db_healthy_is_false(self):
        client = _FakeClient({"status": "degraded", "vector": True, "db_healthy": False})
        result = check_drudge_writable(client)
        match result:
            case Err(NotWritable(detail)):
                self.assertIn("db_healthy=false", detail)
            case other:
                self.fail(f"expected Err(NotWritable), got {other!r}")

    def test_blocks_on_degraded_status_even_if_flag_is_true(self):
        # Defence in depth: status is the engine's own summary of the same probe.
        client = _FakeClient({"status": "degraded", "vector": True, "db_healthy": True})
        match check_drudge_writable(client):
            case Err(NotWritable()):
                pass
            case other:
                self.fail(f"expected Err(NotWritable), got {other!r}")

    def test_allows_healthy_engine(self):
        client = _FakeClient({"status": "ok", "vector": True, "sync": "idle", "db_healthy": True})
        self.assertEqual(check_drudge_writable(client).value, None)

    def test_allows_response_without_db_healthy(self):
        # Wiki-first engine, or a build older than the liveness probe. Absence of the
        # field is not evidence of failure, so ingestion must continue.
        client = _FakeClient({"status": "ok", "vector": False, "sync": "idle"})
        self.assertEqual(check_drudge_writable(client).value, None)

    def test_allows_degraded_status_without_db_healthy_field(self):
        # "degraded" only means the write door when db_healthy is the reason for it.
        client = _FakeClient({"status": "degraded", "vector": False})
        self.assertEqual(check_drudge_writable(client).value, None)

    def test_blocks_when_health_is_unreachable(self):
        client = _FakeClient(error=Unreachable("connection refused"))
        match check_drudge_writable(client):
            case Err(NotWritable(detail)):
                self.assertIn("unreachable", detail)
            case other:
                self.fail(f"expected Err(NotWritable), got {other!r}")

    def test_blocks_when_health_is_refused(self):
        client = _FakeClient(error=Refused(503, "Service Unavailable", "maintenance"))
        match check_drudge_writable(client):
            case Err(NotWritable(detail)):
                self.assertIn("unreachable", detail)
            case other:
                self.fail(f"expected Err(NotWritable), got {other!r}")

    def test_a_health_that_is_not_json_blocks_with_the_old_message(self):
        class _Html(_FakeResponse):
            def read(self):
                return b"<html>bad gateway</html>"

        with mock.patch.object(urllib.request, "urlopen", return_value=_Html()):
            result = check_drudge_writable(DrudgeClient(base_url="http://drudge.test", retries=0))
        self.assertEqual(
            result,
            Err(NotWritable("drudge /health unreachable: Expecting value: line 1 column 1 (char 0)")),
        )

    def test_defaults_to_a_real_client_when_none_is_given(self):
        from ohmyboring.result import Ok

        payload = {"status": "ok", "vector": True, "db_healthy": True}
        with mock.patch.object(DrudgeClient, "health", return_value=Ok(payload)) as health:
            check_drudge_writable()
        health.assert_called_once()


class RequestFailureTest(unittest.TestCase):
    """실패 한 변형당 하나 — 연결 실패는 Unreachable, 4xx/5xx 는 Refused."""

    def test_connection_failure_comes_home_as_unreachable(self):
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = DrudgeClient(base_url="http://drudge.test", retries=0).request("GET", "/health")
        match result:
            case Err(Unreachable(detail)):
                self.assertIn("connection refused", detail)
            case other:
                self.fail(f"expected Err(Unreachable), got {other!r}")

    def test_a_4xx_comes_home_as_refused_with_the_body(self):
        exc = urllib.error.HTTPError(
            "http://drudge.test/search", 403, "Forbidden", {}, io.BytesIO(b'{"error":"no"}')
        )
        with mock.patch.object(urllib.request, "urlopen", side_effect=exc):
            result = DrudgeClient(base_url="http://drudge.test", retries=0).request("POST", "/search", {})
        match result:
            case Err(Refused(status, reason, body)):
                self.assertEqual((status, reason), (403, "Forbidden"))
                self.assertEqual(body, '{"error":"no"}')
            case other:
                self.fail(f"expected Err(Refused), got {other!r}")

    def test_a_5xx_is_retried_then_refused(self):
        exc = urllib.error.HTTPError("http://drudge.test/search", 500, "boom", {}, io.BytesIO(b"down"))
        with mock.patch.object(urllib.request, "urlopen", side_effect=exc) as urlopen:
            result = DrudgeClient(base_url="http://drudge.test", retries=1).request("POST", "/search", {})
        self.assertEqual(urlopen.call_count, 2, "5xx 는 남은 재시도를 쓴다")
        match result:
            case Err(Refused(status, _reason, body)):
                self.assertEqual((status, body), (500, "down"))
            case other:
                self.fail(f"expected Err(Refused), got {other!r}")

    def test_failure_strings_equal_the_old_exception_strings(self):
        """로그 문구 보존 — 부르는 쪽이 옛 str(예외) 로 남기던 줄이 그대로 나온다."""
        http_exc = urllib.error.HTTPError(
            "http://drudge.test/health", 503, "Service Unavailable", {}, io.BytesIO(b"maintenance")
        )
        url_exc = urllib.error.URLError("connection refused")
        reset_exc = ConnectionResetError(104, "Connection reset by peer")
        hangup_exc = http.client.RemoteDisconnected("Remote end closed connection without response")
        cases = (http_exc, url_exc, reset_exc, hangup_exc)
        for exc in cases:
            with mock.patch.object(urllib.request, "urlopen", side_effect=exc):
                result = DrudgeClient(base_url="http://drudge.test", retries=0).request("GET", "/health")
            match result:
                case Err(failure):
                    self.assertEqual(str(failure), str(exc))
                case other:
                    self.fail(f"expected Err for {exc!r}, got {other!r}")
        self.assertEqual(str(http_exc), "HTTP Error 503: Service Unavailable")

    def test_a_socket_reset_is_unreachable_and_is_not_retried(self):
        reset = ConnectionResetError(104, "Connection reset by peer")
        with mock.patch.object(urllib.request, "urlopen", side_effect=reset) as urlopen:
            result = DrudgeClient(base_url="http://drudge.test", retries=3).request("GET", "/health")
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(result, Err(Unreachable(str(reset))))

    def test_a_200_that_is_not_json_is_malformed_with_the_old_decoder_message(self):
        class _Html(_FakeResponse):
            def read(self):
                return b"<html>bad gateway</html>"

        with mock.patch.object(urllib.request, "urlopen", return_value=_Html()):
            result = DrudgeClient(base_url="http://drudge.test", retries=0).request("GET", "/health")
        self.assertEqual(result, Err(Malformed("Expecting value: line 1 column 1 (char 0)")))

    def test_a_200_that_is_not_utf8_is_malformed(self):
        class _Latin1(_FakeResponse):
            def read(self):
                return b"\xff\xfe"

        with mock.patch.object(urllib.request, "urlopen", return_value=_Latin1()):
            result = DrudgeClient(base_url="http://drudge.test", retries=0).request("GET", "/health")
        match result:
            case Err(Malformed(detail)):
                self.assertIn("utf-8", detail)
            case other:
                self.fail(f"expected Err(Malformed), got {other!r}")

    def test_no_raise_escapes_the_adapter_on_io_failure(self):
        from ohmyboring.result import Err, Ok

        with mock.patch.object(urllib.request, "urlopen", side_effect=TimeoutError("slow")):
            for call in (
                lambda c: c.request("GET", "/health"),
                lambda c: c.search("pool"),
                lambda c: c.handover("s", "t", []),
                lambda c: c.consumption("s", "t", Verdict(verdict="used")),
                lambda c: c.remember("t", "b"),
                lambda c: c.health(),
                lambda c: c.sync(),
                lambda c: c.audit(),
                lambda c: c.mcp_call("remember", {}),
                lambda c: c.context(),
            ):
                result = call(DrudgeClient(base_url="http://drudge.test", retries=0))
                self.assertIsInstance(result, (Ok, Err), f"must return Either, got {result!r}")


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
    """Replaces request() so the payload the engine would receive is captured, not sent."""

    def __init__(self, client: DrudgeClient):
        self.sent: list[dict] = []
        client.request = self._request  # noqa: SLF001 — the wire test owns the client's back door

    def _request(self, method, path, payload=None, timeout=None):
        self.sent.append({"method": method, "path": path, "payload": payload})
        return Ok({})

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
            PathMarks(
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
        client.consumption("s1", "t", Verdict(verdict="used"))
        self.assertEqual(
            wire.body(),
            '{"session_id": "s1", "observed_at": "t", "verdict": "used"}',
        )

    def test_the_marks_are_a_sum_type_with_two_variants(self):
        marks: ConsumptionMarks = Verdict(verdict="used", judge="owner")
        self.assertIsInstance(marks, Verdict)
        marks = PathMarks(used=["/vault/wiki/a.md"])
        self.assertIsInstance(marks, PathMarks)


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


class _Reply:
    def __init__(self, text):
        self._body = json.dumps({"result": {"content": [{"type": "text", "text": text}]}}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def _http_error(code):
    return urllib.error.HTTPError("http://drudge.test/mcp", code, "boom", None, None)


class CallRememberTest(unittest.TestCase):
    def _call(self, outcomes):
        """Run call_remember against a scripted sequence of urlopen outcomes; return (result, calls, sleeps)."""
        script = list(outcomes)
        calls, sleeps = [], []

        def fake_urlopen(req, timeout):
            calls.append((json.loads(req.data), timeout))
            outcome = script.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with (
            mock.patch.object(urllib.request, "urlopen", fake_urlopen),
            mock.patch.object(engine.time, "sleep", sleeps.append),
            mock.patch.object(engine.sys, "stderr", io.StringIO()),
            mock.patch.dict(engine.os.environ, {"DISTILL_REMEMBER_RETRIES": "2"}),
        ):
            result = engine.call_remember("t", "b", "personal", "repo", ["x"], [], [], [], "sid")
        return result, calls, sleeps

    def test_transient_server_errors_are_retried_with_doubling_waits(self):
        result, calls, sleeps = self._call([_http_error(503), _http_error(502), _Reply("remembered wiki-1")])
        self.assertEqual(result, engine.RememberOutcome(True, "remembered"))
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(len(calls), 3)

    def test_a_client_error_is_final_and_not_retried(self):
        result, calls, sleeps = self._call([_http_error(400)])
        self.assertEqual(result, engine.RememberOutcome(False, "failed"))
        self.assertEqual((sleeps, len(calls)), ([], 1))

    def test_a_duplicate_is_a_success(self):
        result, _calls, _sleeps = self._call([_Reply("skipped — duplicate of wiki-3")])
        self.assertEqual(result, engine.RememberOutcome(True, "duplicate"))

    def test_the_request_names_the_session_and_the_tool(self):
        _result, calls, _sleeps = self._call([_Reply("remembered wiki-1")])
        payload, timeout = calls[0]
        self.assertEqual(payload["params"]["name"], "remember")
        self.assertEqual(payload["params"]["arguments"]["omb_session_id"], "sid")
        self.assertEqual(timeout, 45)


if __name__ == "__main__":
    unittest.main(verbosity=2)
