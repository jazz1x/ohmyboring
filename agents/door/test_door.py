#!/usr/bin/env python3
"""Unit tests for the door — stdlib only, no live engine, no network beyond localhost.

A stub engine (http.server, random port) stands in for the Rust engine and the
real door runs under uvicorn in a thread against it. Covers:
  (a) GET /health body bytes are identical to the engine's — sha256 equal,
      content-type included (AC2, AC3)
  (b) POST /mcp forwards the request body untouched and returns the response
      byte-for-byte, tool order included (AC2, AC3)
  (c) with the engine down the door answers 502 and the ROP JSON body,
      never a 200 with an empty body (AC5)
  (d) unregistered paths (GET /nope, POST /nope, GET /docs) get FastAPI's 404
      and never reach the stub — the door is not a catch-all proxy (AC6)
  (e) the route table is read from the contract snapshot: http_routes ∪ door_routes
      is exactly the registered set, nothing beyond (AC1)
  (l) GET /approved: without DOOR_PG_DSN it is 503 JSON "store not configured",
      never a silent empty 200; with stub rows injected it answers the ADT —
      used/contested separated, ts parsed out of the slack session name,
      sorted newest first (AC2)
  (f) only content-type, accept, mcp-session-id, x-request-id, x-boring-owner-token
      travel upstream; authorization and x-forwarded-for do not (AC4). The owner
      token travels as sent — never added from the door's own env, never rewritten
  (g) a non-JSON upstream content-type passes through untouched (AC3)
  (h) an upstream response without content-type stays without one (AC3)
  (i) a text/event-stream answer is relayed chunk by chunk: the first chunk
      reaches the client before the stub sends its second one, the response is
      chunked (no content-length) (AC1, AC2)
  (j) the query string travels verbatim: /query-log?limit=1 arrives with the
      query attached, a query-less request gets no "?" (AC4)
  (k) a client that disconnects mid-stream makes the door close the upstream —
      the stub sees the connection go away (AC6)
  (l) POST /mcp tools/call `remember` and POST /remember answer byte-for-byte
      (sha256 vs the stub body) and leave one remember_shadow event after the
      response — a raising shadow cannot change the answer (E3a-1)
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "src")]

# Loaded as a package (the way uvicorn and the container load it) because door.py
# imports its sibling with `from . import approved`; test_python_deps.py has no
# notion of repo-local packages, so the import goes through importlib.
import importlib  # noqa: E402

import fastapi  # noqa: E402
import uvicorn  # noqa: E402

door = importlib.import_module("agents.door.door")
approved = importlib.import_module("agents.door.approved")
claim_source = importlib.import_module("agents.door.claim_source")
rules = importlib.import_module("agents.door.rules")

STUB_CONTENT_TYPE = "application/json; charset=utf-8"

SSE_FIRST = b"event: first\ndata: one\n\n"
SSE_SECOND = b"event: second\ndata: two\n\n"

# Deliberately non-canonical bytes: odd spacing, a non-ASCII value. Any
# json.loads → json.dumps round-trip changes these bytes, so a door that
# re-serializes fails test (a) on the hash.
HEALTH_BODY = b'{"status":"ok", "sync":"idle","vector":true,  "note":"\xec\x95\x88\xeb\x85\x95"}'

TOOLS_LIST_BODY = (
    b'{"jsonrpc":"2.0","id":1,"result":{"tools":['
    b'{"name":"alpha","inputSchema":{"type":"object"}},'
    b'{"name":"beta","inputSchema":{"type":"object"}},'
    b'{"name":"gamma","inputSchema":{"type":"object"}}]}}'
)

#: 엔진 recall 응답의 실측 모양(content-type application/json) — 문이 번호 블록을 앞에
#: 붙이는 대상. "boom" 이 들어간 query 면 isError 답을 준다(통제군).
RECALL_BODY = b'{"id":2,"jsonrpc":"2.0","result":{"content":[{"type":"text","text":"- [wiki-2229.md] ..."}],"isError":false}}'
RECALL_ERROR_BODY = (
    b'{"id":2,"jsonrpc":"2.0","result":{"content":[{"type":"text","text":"-32000 boom"}],"isError":true}}'
)

#: 엔진 remember 답의 실측 모양 — 고의로 비정형 바이트(이상한 공백·비아스키)로 만들어
#: 문이 다시 직렬화하면 sha256 이 갈라지게 한다. 화살표·중점·대시는 UTF-8 이스케이프.
REMEMBER_MCP_BODY = (
    b'{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "remembered '
    b"\xe2\x86\x92 wiki/wiki-2901.md \xc2\xb7 chunks 2 \xc2\xb7 graph(tools 0 concepts 0 claims 0) "
    b'\xe2\x80\x94 recallable now"}]}}'
)
REMEMBER_HTTP_BODY = (
    b'{"source_path": "/vault/wiki/wiki-2902.md", "wiki_id": "wiki-2902", "duplicate": null, '
    b'"supersedes": 0, "unknown": 0}'
)


class StubHandler(BaseHTTPRequestHandler):
    """The engine's stand-in: fixed /health, tools/list answer, otherwise echo.

    A POST body {"respond_as": <content-type>|null} overrides the response
    content-type — None omits the header entirely.
    """

    seen_bodies: list[bytes] = []
    seen_content_types: list[str] = []
    seen_headers: list[dict[str, str]] = []
    last_path: str | None = None
    sse_events: list[str] = []
    hits = 0

    def do_GET(self):
        type(self).last_path = self.path
        if self.path == "/health":
            self._reply(200, HEALTH_BODY)
        elif self.path == "/sse":
            self._sse()
        else:
            self._reply(404, b'{"error":"stub 404"}')

    def do_POST(self):
        type(self).last_path = self.path
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        type(self).seen_bodies.append(body)
        type(self).seen_content_types.append(self.headers.get("content-type", ""))
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        respond_as = STUB_CONTENT_TYPE
        if isinstance(parsed, dict) and "respond_as" in parsed:
            respond_as = parsed["respond_as"]
        params = parsed.get("params") if isinstance(parsed, dict) else None
        if isinstance(params, dict) and params.get("name") == "remember":
            # The engine's tools/call remember answer — the shadow reads this path.
            self._reply(200, REMEMBER_MCP_BODY, respond_as)
        elif self.path == "/remember":
            self._reply(200, REMEMBER_HTTP_BODY, respond_as)
        elif isinstance(params, dict) and params.get("name") == "recall":
            # The engine's tools/call recall answer: fixed body, "boom" in the query → isError.
            error = "boom" in str(params.get("arguments", {}))
            self._reply(200, RECALL_ERROR_BODY if error else RECALL_BODY, respond_as)
        elif isinstance(parsed, dict) and parsed.get("method") == "tools/list":
            self._reply(200, TOOLS_LIST_BODY, respond_as)
        else:
            self._reply(200, body, respond_as)

    def _reply(self, code: int, body: bytes, content_type: str | None = STUB_CONTENT_TYPE) -> None:
        type(self).hits += 1
        type(self).seen_headers.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(code)
        if content_type is not None:
            self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self) -> None:
        type(self).sse_events.append("open")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        self.wfile.write(SSE_FIRST)
        self.wfile.flush()
        type(self).sse_events.append("chunk1")
        time.sleep(0.2)
        try:
            self.wfile.write(SSE_SECOND)
            self.wfile.flush()
            type(self).sse_events.append("chunk2")
        except (BrokenPipeError, ConnectionResetError):
            type(self).sse_events.append("closed")
            return
        self.request.settimeout(3.0)
        try:
            if self.rfile.read(1) == b"":
                type(self).sse_events.append("closed")
        except TimeoutError:
            pass
        except (ConnectionResetError, BrokenPipeError):
            type(self).sse_events.append("closed")

    def log_message(self, *args):
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _req(port: int, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), r.headers.get("content-type")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("content-type")


class DoorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved_upstream = os.environ.get("DOOR_UPSTREAM")
        cls.stub = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        cls.stub_thread = threading.Thread(target=cls.stub.serve_forever, daemon=True)
        cls.stub_thread.start()
        os.environ["DOOR_UPSTREAM"] = f"http://127.0.0.1:{cls.stub.server_address[1]}"
        cls.door_port = _free_port()
        cls.door_server = uvicorn.Server(
            uvicorn.Config(door.app, host="127.0.0.1", port=cls.door_port, log_level="warning")
        )
        cls.door_thread = threading.Thread(target=cls.door_server.run, daemon=True)
        cls.door_thread.start()
        cls.sse_port = _free_port()
        cls.sse_app = fastapi.FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        cls.sse_app.add_api_route("/sse", door._proxy, methods=["GET"])
        cls.sse_server = uvicorn.Server(
            uvicorn.Config(cls.sse_app, host="127.0.0.1", port=cls.sse_port, log_level="warning")
        )
        cls.sse_thread = threading.Thread(target=cls.sse_server.run, daemon=True)
        cls.sse_thread.start()
        deadline = 10.0

        while deadline > 0:
            try:
                status, _, _ = _req(cls.door_port, "GET", "/health")
                if status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.05)
            deadline -= 0.05
        else:
            raise RuntimeError("door did not come up within 10s")

        deadline = 10.0
        while deadline > 0:
            try:
                with socket.create_connection(("127.0.0.1", cls.sse_port), timeout=1):
                    break
            except OSError:
                pass
            time.sleep(0.05)
            deadline -= 0.05
        else:
            raise RuntimeError("sse app did not come up within 10s")

    @classmethod
    def tearDownClass(cls):
        cls.door_server.should_exit = True
        cls.sse_server.should_exit = True
        cls.stub.shutdown()
        cls.stub.server_close()
        if cls._saved_upstream is None:
            os.environ.pop("DOOR_UPSTREAM", None)
        else:
            os.environ["DOOR_UPSTREAM"] = cls._saved_upstream

    def test_get_health_bytes_identical(self):
        status, body, content_type = _req(self.door_port, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, HEALTH_BODY)
        self.assertEqual(hashlib.sha256(body).hexdigest(), hashlib.sha256(HEALTH_BODY).hexdigest())
        self.assertEqual(content_type, STUB_CONTENT_TYPE)

    def test_mcp_post_passthrough(self):
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
        status, body, content_type = _req(
            self.door_port, "POST", "/mcp", body=payload, headers={"content-type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(StubHandler.seen_bodies[-1], payload)
        self.assertEqual(StubHandler.seen_content_types[-1], "application/json")
        self.assertEqual(body, TOOLS_LIST_BODY)
        self.assertEqual(content_type, STUB_CONTENT_TYPE)
        names = [t["name"] for t in json.loads(body)["result"]["tools"]]
        self.assertEqual(names, ["alpha", "beta", "gamma"])

    def test_engine_down_returns_502_json(self):
        dead_port = _free_port()
        saved = os.environ["DOOR_UPSTREAM"]
        os.environ["DOOR_UPSTREAM"] = f"http://127.0.0.1:{dead_port}"
        try:
            status, body, content_type = _req(self.door_port, "GET", "/health")
        finally:
            os.environ["DOOR_UPSTREAM"] = saved
        self.assertEqual(status, 502)
        self.assertIn("application/json", content_type)
        self.assertEqual(
            json.loads(body),
            {"error": "engine unreachable", "upstream": f"http://127.0.0.1:{dead_port}"},
        )

    def test_unregistered_paths_are_404(self):
        hits_before = StubHandler.hits
        status, _, _ = _req(self.door_port, "GET", "/nope")
        self.assertEqual(status, 404)
        status, _, _ = _req(
            self.door_port, "POST", "/nope", body=b"{}", headers={"content-type": "application/json"}
        )
        self.assertEqual(status, 404)
        status, _, _ = _req(self.door_port, "GET", "/docs")
        self.assertEqual(status, 404)
        self.assertEqual(StubHandler.hits, hits_before, "unregistered paths must not reach the stub")

    def test_routes_match_contract_snapshot(self):
        contract = json.loads(door._CONTRACT.read_text(encoding="utf-8"))
        entries = contract["http_routes"] + contract.get("door_routes", [])
        expected = set()
        for entry in entries:
            method, path = entry.split(" ", 1)
            expected.add((method, path))
        actual = {(method, route.path) for route in door.app.routes for method in route.methods}
        self.assertEqual(actual, expected)
        self.assertEqual(len(contract["http_routes"]), 24)

    def test_request_header_policy(self):
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "mcp-session-id": "sess-123",
            "x-request-id": "req-abc",
            "x-boring-owner-token": "tok-r9",
            "authorization": "Bearer secret",
            "x-forwarded-for": "1.2.3.4",
        }
        status, _, _ = _req(self.door_port, "POST", "/mcp", body=payload, headers=headers)
        self.assertEqual(status, 200)
        seen = StubHandler.seen_headers[-1]
        for name in ("content-type", "accept", "mcp-session-id", "x-request-id", "x-boring-owner-token"):
            self.assertEqual(seen.get(name), headers[name])
        self.assertNotIn("authorization", seen)
        self.assertNotIn("x-forwarded-for", seen)

    def test_owner_token_travels_as_sent_and_is_never_minted(self):
        payload = json.dumps({"session_id": "s", "verdict": "contested", "judge": "owner"}).encode()
        with mock.patch.dict(os.environ, {"BORING_OWNER_TOKEN": "tok-owner"}):
            seen = {}
            for token in ("tok-r9", None, "wrong"):
                headers = {"content-type": "application/json"}
                headers |= {"x-boring-owner-token": token} if token else {}
                status, _, _ = _req(self.door_port, "POST", "/consumption", body=payload, headers=headers)
                self.assertEqual(status, 200)
                seen[token] = StubHandler.seen_headers[-1]
        self.assertEqual(seen["tok-r9"].get("x-boring-owner-token"), "tok-r9")
        self.assertNotIn("x-boring-owner-token", seen[None])
        self.assertEqual(seen["wrong"].get("x-boring-owner-token"), "wrong")

    def test_non_json_content_type_passthrough(self):
        payload = json.dumps({"respond_as": "text/plain; charset=utf-8"}).encode()
        status, body, content_type = _req(
            self.door_port, "POST", "/status", body=payload, headers={"content-type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertEqual(content_type, "text/plain; charset=utf-8")

    def test_missing_upstream_content_type_stays_missing(self):
        payload = json.dumps({"respond_as": None}).encode()
        status, _, content_type = _req(
            self.door_port, "POST", "/status", body=payload, headers={"content-type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertIsNone(content_type)

    def test_sse_first_chunk_streams_before_stub_sends_second(self):
        sock = socket.create_connection(("127.0.0.1", self.sse_port), timeout=10)
        try:
            sock.sendall(b"GET /sse HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            wire = b""
            started = time.monotonic()
            while b"\r\n\r\n" not in wire:
                wire += sock.recv(4096)
            headers, _, body = wire.partition(b"\r\n\r\n")
            headers = headers.lower()
            self.assertIn(b"content-type: text/event-stream", headers)
            self.assertNotIn(b"content-length", headers)
            self.assertLess(time.monotonic() - started, 0.2)
            started = time.monotonic()
            while SSE_FIRST not in body:
                chunk = sock.recv(64)
                self.assertTrue(chunk, "stream ended before the first chunk")
                body += chunk
            self.assertLess(time.monotonic() - started, 0.2)
        finally:
            sock.close()

    def test_query_string_passes_through_verbatim(self):
        _req(self.door_port, "GET", "/query-log?limit=1")
        self.assertEqual(StubHandler.last_path, "/query-log?limit=1")
        _req(self.door_port, "GET", "/query-log")
        self.assertEqual(StubHandler.last_path, "/query-log")

    def test_client_disconnect_closes_upstream(self):
        StubHandler.sse_events.clear()
        sock = socket.create_connection(("127.0.0.1", self.sse_port), timeout=10)
        sock.sendall(b"GET /sse HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        wire = b""
        while b"\r\n\r\n" not in wire:
            wire += sock.recv(4096)
        sock.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "closed" in StubHandler.sse_events:
                break
            time.sleep(0.05)
        self.assertIn("closed", StubHandler.sse_events)


class McpRecallAugmentTests(unittest.TestCase):
    """POST /mcp tools/call recall — the door prepends numbered-note blocks (wiki-NNNN) to a
    2xx application/json engine answer, read from the vault at BORING_VAULT_DIR. Pinned here:
      - a numbered recall answers with 「- 노트 wiki-NNNN — <title>」 first, engine text after
      - a missing number is said (「은 볼트에 없음」), engine answer still follows
      - controls — recall without numbers, a non-recall tool, an isError answer, a non-JSON
        upstream answer — travel byte-for-byte (the exact stub bytes come back)
      - an unreadable vault costs one stderr line and the engine answer unchanged,
        never a dead recall
    """

    _WIKI_2226 = """---
id: wiki-2226
title: '문이 회상 앞에 번호 노트를 붙인다'
kind: note
origin: personal
date: 2026-09-29
---

첫째 문단 — 엔진 recall 은 검색이라 번호를 못 푼다.
"""

    @classmethod
    def setUpClass(cls):
        cls._saved_upstream = os.environ.get("DOOR_UPSTREAM")
        cls.stub = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        cls.stub_thread = threading.Thread(target=cls.stub.serve_forever, daemon=True)
        cls.stub_thread.start()
        os.environ["DOOR_UPSTREAM"] = f"http://127.0.0.1:{cls.stub.server_address[1]}"
        cls.door_port = _free_port()
        cls.door_server = uvicorn.Server(
            uvicorn.Config(door.app, host="127.0.0.1", port=cls.door_port, log_level="warning")
        )
        cls.door_thread = threading.Thread(target=cls.door_server.run, daemon=True)
        cls.door_thread.start()
        deadline = 10.0
        while deadline > 0:
            try:
                status, _, _ = _req(cls.door_port, "GET", "/health")
                if status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.05)
            deadline -= 0.05
        else:
            raise RuntimeError("door did not come up within 10s")

    @classmethod
    def tearDownClass(cls):
        cls.door_server.should_exit = True
        cls.stub.shutdown()
        cls.stub.server_close()
        if cls._saved_upstream is None:
            os.environ.pop("DOOR_UPSTREAM", None)
        else:
            os.environ["DOOR_UPSTREAM"] = cls._saved_upstream

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        vault = Path(self._tmp.name) / "vault"
        (vault / "wiki").mkdir(parents=True)
        (vault / "wiki" / "wiki-2226.md").write_text(self._WIKI_2226, encoding="utf-8")
        self._saved_vault = os.environ.get("BORING_VAULT_DIR")
        os.environ["BORING_VAULT_DIR"] = str(vault)
        self.addCleanup(self._restore_vault)

    def _restore_vault(self):
        if self._saved_vault is None:
            os.environ.pop("BORING_VAULT_DIR", None)
        else:
            os.environ["BORING_VAULT_DIR"] = self._saved_vault

    def _recall(self, query: str):
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "recall", "arguments": {"query": query}},
            }
        ).encode()
        return _req(
            self.door_port, "POST", "/mcp", body=payload, headers={"content-type": "application/json"}
        )

    def test_a_numbered_recall_leads_with_the_note_block(self):
        status, body, content_type = self._recall("wiki-2226 봐")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, STUB_CONTENT_TYPE)
        text = json.loads(body)["result"]["content"][0]["text"]
        self.assertTrue(
            text.startswith("- 노트 wiki-2226 — 문이 회상 앞에 번호 노트를 붙인다\n  날짜: 2026-09-29"),
            text[:120],
        )
        self.assertIn("첫째 문단 — 엔진 recall 은 검색이라 번호를 못 푼다.", text)
        self.assertTrue(text.endswith("- [wiki-2229.md] ..."), "엔진 본문이 그 뒤에 그대로 와야 한다")

    def test_a_missing_number_is_said_and_the_engine_answer_follows(self):
        status, body, _ = self._recall("wiki-9999 봐")
        self.assertEqual(status, 200)
        text = json.loads(body)["result"]["content"][0]["text"]
        self.assertTrue(text.startswith("- wiki-9999 은 볼트에 없음\n\n- [wiki-2229.md] ..."), text[:120])

    def test_several_numbers_join_in_order(self):
        _, body, _ = self._recall("wiki-9999 와 wiki-2226")
        text = json.loads(body)["result"]["content"][0]["text"]
        self.assertTrue(
            text.startswith("- wiki-9999 은 볼트에 없음\n\n- 노트 wiki-2226 — 문이 회상 앞에"),
            text[:160],
        )

    def test_a_recall_without_numbers_is_byte_identical(self):
        status, body, content_type = self._recall("그냥 검색해줘")
        self.assertEqual((status, body, content_type), (200, RECALL_BODY, STUB_CONTENT_TYPE))

    def test_a_numbered_recall_on_another_route_is_byte_identical(self):
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "recall", "arguments": {"query": "wiki-2226 봐"}},
            }
        ).encode()
        status, body, _ = _req(
            self.door_port, "POST", "/context", body=payload, headers={"content-type": "application/json"}
        )
        self.assertEqual((status, body), (200, RECALL_BODY), "only POST /mcp is decorated")

    def test_a_non_recall_tool_is_byte_identical(self):
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "wiki-2226"}},
            }
        ).encode()
        status, body, _ = _req(
            self.door_port, "POST", "/mcp", body=payload, headers={"content-type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, payload, "다른 도구의 답은 요청 바이트를 그대로 돌려받는다(에코 통제군)")

    def test_an_error_answer_is_byte_identical(self):
        status, body, _ = self._recall("boom wiki-2226")
        self.assertEqual((status, body), (200, RECALL_ERROR_BODY))

    def test_a_non_json_upstream_answer_is_byte_identical(self):
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "recall", "arguments": {"query": "wiki-2226"}},
                "respond_as": "text/plain; charset=utf-8",
            }
        ).encode()
        status, body, content_type = _req(
            self.door_port, "POST", "/mcp", body=payload, headers={"content-type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, RECALL_BODY)
        self.assertEqual(content_type, "text/plain; charset=utf-8")

    def test_an_unreadable_vault_passes_the_answer_and_costs_one_log_line(self):
        def boom(note_id):
            raise OSError("vault mount gone")

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), mock.patch.object(door, "_note_block_for", boom):
            status, body, _ = self._recall("wiki-2226")
        self.assertEqual(status, 200)
        self.assertEqual(body, RECALL_BODY, "볼트 불응은 recall 을 죽이지 않는다 — 엔진 답 그대로")
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, f"로그는 정확히 한 줄이어야 한다: {lines}")
        self.assertIn("vault", lines[0])


class RememberShadowTests(unittest.TestCase):
    """E3a-1 — 문을 지난 remember 는 엔진 답을 바이트 그대로 돌려주고, 응답 뒤에 그림자가
    엔진이 실제로 쓴 노트와 파이썬 렌더를 칸별 대조해 사건(remember_shadow) 한 줄을 남긴다.

    Pinned here:
      - POST /mcp tools/call remember 의 응답이 엔진 답과 바이트 같다(sha256)
      - POST /remember 의 응답이 엔진 답과 바이트 같다(sha256)
      - 응답 뒤 그림자가 사건 한 줄을 남긴다 — status ok, source_path, omb_session_id
      - 그림자가 예외를 던져도 응답은 그대로 — 사건 status=error, 로그 한 줄
      - 응답은 그림자를 기다리지 않는다
    """

    _WIKI_2901 = """---
id: wiki-2901
title: 문 그림자 시험
kind: note
origin: personal
project: ''
date: "2026-10-01"
tags:
- door
- shadow
tools: []
concepts: []
claims: []
relates_to: []
sources: []
omb_session_id: sess-door-1
author: unknown
---

문을 지난 remember 는 엔진 답을 바이트 그대로 돌려준다.
"""

    _WIKI_2902 = """---
id: wiki-2902
title: HTTP remember 그림자 시험
kind: note
origin: personal
project: ''
date: "2026-10-01"
tags: []
tools: []
concepts: []
claims: []
relates_to: []
sources: []
author: inferred
---

POST /remember 도 문을 지난다.
"""

    _MCP_ARGS = {
        "title": "문 그림자 시험",
        "body": "문을 지난 remember 는 엔진 답을 바이트 그대로 돌려준다.",
        "origin": "personal",
        "tags": ["door", "shadow"],
        "omb_session_id": "sess-door-1",
    }
    _HTTP_ARGS = {
        "title": "HTTP remember 그림자 시험",
        "body": "POST /remember 도 문을 지난다.",
        "origin": "personal",
        "author": "inferred",
    }

    @classmethod
    def setUpClass(cls):
        cls._saved = {
            name: os.environ.get(name)
            for name in ("DOOR_UPSTREAM", "BORING_VAULT_DIR", "BORING_EVENT_SINK", "BORING_EVENT_LOG")
        }
        cls._tmp = tempfile.TemporaryDirectory()
        cls.stub = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        cls.stub_thread = threading.Thread(target=cls.stub.serve_forever, daemon=True)
        cls.stub_thread.start()
        os.environ["DOOR_UPSTREAM"] = f"http://127.0.0.1:{cls.stub.server_address[1]}"
        cls.door_port = _free_port()
        cls.door_server = uvicorn.Server(
            uvicorn.Config(door.app, host="127.0.0.1", port=cls.door_port, log_level="warning")
        )
        cls.door_thread = threading.Thread(target=cls.door_server.run, daemon=True)
        cls.door_thread.start()
        deadline = 10.0
        while deadline > 0:
            try:
                status, _, _ = _req(cls.door_port, "GET", "/health")
                if status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.05)
            deadline -= 0.05
        else:
            raise RuntimeError("door did not come up within 10s")

    @classmethod
    def tearDownClass(cls):
        cls.door_server.should_exit = True
        cls.stub.shutdown()
        cls.stub.server_close()
        cls._tmp.cleanup()
        for name, value in cls._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def setUp(self):
        vault = Path(self._tmp.name) / "vault"
        (vault / "wiki").mkdir(parents=True, exist_ok=True)
        (vault / "wiki" / "wiki-2901.md").write_text(self._WIKI_2901, encoding="utf-8")
        (vault / "wiki" / "wiki-2902.md").write_text(self._WIKI_2902, encoding="utf-8")
        os.environ["BORING_VAULT_DIR"] = str(vault)
        os.environ["BORING_EVENT_SINK"] = "spool"
        os.environ["BORING_EVENT_LOG"] = str(Path(self._tmp.name) / f"events-{time.time_ns()}.ndjson")

    def _mcp_payload(self, arguments: dict) -> bytes:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "remember", "arguments": arguments},
            }
        ).encode()

    def _shadow_events(self) -> list[dict]:
        path = Path(os.environ["BORING_EVENT_LOG"])
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("event") == "remember_shadow"
        ]

    def _wait_for_shadow(self, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self._shadow_events()
            if events:
                return events[-1]
            time.sleep(0.05)
        self.fail(f"remember_shadow event did not land in {os.environ['BORING_EVENT_LOG']}")

    def test_mcp_remember_answer_is_byte_identical(self):
        payload = self._mcp_payload(self._MCP_ARGS)
        status, body, content_type = _req(
            self.door_port, "POST", "/mcp", body=payload, headers={"content-type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, REMEMBER_MCP_BODY)
        self.assertEqual(hashlib.sha256(body).hexdigest(), hashlib.sha256(REMEMBER_MCP_BODY).hexdigest())
        self.assertEqual(content_type, STUB_CONTENT_TYPE)
        self.assertEqual(StubHandler.seen_bodies[-1], payload, "엔진에 간 본문도 바이트 그대로")

    def test_http_remember_answer_is_byte_identical(self):
        payload = json.dumps(self._HTTP_ARGS).encode()
        status, body, content_type = _req(
            self.door_port, "POST", "/remember", body=payload, headers={"content-type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, REMEMBER_HTTP_BODY)
        self.assertEqual(hashlib.sha256(body).hexdigest(), hashlib.sha256(REMEMBER_HTTP_BODY).hexdigest())
        self.assertEqual(content_type, STUB_CONTENT_TYPE)

    def test_mcp_remember_leaves_one_shadow_event(self):
        _req(
            self.door_port,
            "POST",
            "/mcp",
            body=self._mcp_payload(self._MCP_ARGS),
            headers={"content-type": "application/json"},
        )
        event = self._wait_for_shadow()
        self.assertEqual(event["component"], "door")
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["source_path"], "/vault/wiki/wiki-2901.md")
        self.assertEqual(event["omb_session_id"], "sess-door-1")
        self.assertEqual(event["fields"], [])
        self.assertIn("excluded", event["relates_to"])

    def test_http_remember_leaves_one_shadow_event(self):
        _req(
            self.door_port,
            "POST",
            "/remember",
            body=json.dumps(self._HTTP_ARGS).encode(),
            headers={"content-type": "application/json"},
        )
        event = self._wait_for_shadow()
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["source_path"], "/vault/wiki/wiki-2902.md")

    def test_a_raising_shadow_cannot_change_the_answer(self):
        payload = self._mcp_payload(self._MCP_ARGS)
        # 그림자는 응답 뒤에 도니, 패치·stderr 갈무리는 사건이 뜰 때까지 살아 있어야 한다.
        with mock.patch.object(door.remember_shadow, "run_shadow", side_effect=RuntimeError("boom")):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                status, body, _ = _req(
                    self.door_port, "POST", "/mcp", body=payload, headers={"content-type": "application/json"}
                )
                event = self._wait_for_shadow()
        self.assertEqual(status, 200)
        self.assertEqual(body, REMEMBER_MCP_BODY, "그림자 예외는 응답을 바꾸지 못한다")
        self.assertEqual(len([ln for ln in buf.getvalue().splitlines() if ln.strip()]), 1)
        self.assertEqual(event["status"], "error")
        self.assertIn("RuntimeError", event["reason"])
        self.assertEqual(event["omb_session_id"], "sess-door-1")

    def test_the_answer_does_not_wait_for_the_shadow(self):
        started = time.monotonic()

        def slow_shadow(**_kwargs):
            time.sleep(0.6)
            return door.remember_shadow.ShadowEvent("ok")

        with mock.patch.object(door.remember_shadow, "run_shadow", slow_shadow):
            status, body, _ = _req(
                self.door_port,
                "POST",
                "/mcp",
                body=self._mcp_payload(self._MCP_ARGS),
                headers={"content-type": "application/json"},
            )
            elapsed = time.monotonic() - started
            self._wait_for_shadow()
        self.assertEqual(status, 200)
        self.assertEqual(body, REMEMBER_MCP_BODY)
        self.assertLess(elapsed, 0.6, "응답이 그림자를 기다리면 이 값이 0.6s 를 넘는다")

    def test_non_remember_calls_leave_no_shadow_event(self):
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
        status, body, _ = _req(
            self.door_port, "POST", "/mcp", body=payload, headers={"content-type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, TOOLS_LIST_BODY)
        time.sleep(0.3)
        self.assertEqual(self._shadow_events(), [], "회상·다른 도구엔 그림자가 따라붙지 않는다")


class ActiveProjectsRouteTests(unittest.TestCase):
    """GET /projects?active_days= through the real door app — stub rows, no DB (AC1)."""

    @classmethod
    def setUpClass(cls):
        cls._saved_upstream = os.environ.get("DOOR_UPSTREAM")
        cls.stub = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        cls.stub_thread = threading.Thread(target=cls.stub.serve_forever, daemon=True)
        cls.stub_thread.start()
        os.environ["DOOR_UPSTREAM"] = f"http://127.0.0.1:{cls.stub.server_address[1]}"
        cls.door_port = _free_port()
        cls.door_server = uvicorn.Server(
            uvicorn.Config(door.app, host="127.0.0.1", port=cls.door_port, log_level="warning")
        )
        cls.door_thread = threading.Thread(target=cls.door_server.run, daemon=True)
        cls.door_thread.start()
        deadline = 10.0
        while deadline > 0:
            try:
                with socket.create_connection(("127.0.0.1", cls.door_port), timeout=1):
                    break
            except OSError:
                pass
            time.sleep(0.05)
            deadline -= 0.05
        else:
            raise RuntimeError("door did not come up within 10s")

    @classmethod
    def tearDownClass(cls):
        cls.door_server.should_exit = True
        cls.stub.shutdown()
        cls.stub.server_close()
        if cls._saved_upstream is None:
            os.environ.pop("DOOR_UPSTREAM", None)
        else:
            os.environ["DOOR_UPSTREAM"] = cls._saved_upstream

    def setUp(self):
        self._saved_dsn = os.environ.get("DOOR_PG_DSN")
        self._rows: list[tuple[str, int]] = []
        self._orig_fetch = door._fetch_active_projects
        door._fetch_active_projects = lambda active_days: self._rows

    def tearDown(self):
        door._fetch_active_projects = self._orig_fetch
        if self._saved_dsn is None:
            os.environ.pop("DOOR_PG_DSN", None)
        else:
            os.environ["DOOR_PG_DSN"] = self._saved_dsn

    def test_no_active_days_proxies_the_plain_engine_route(self):
        # No active_days param: the door's own /projects handler falls through to _proxy,
        # so a call still reaches the stub engine at exactly this path — proved here by the
        # stub's fixed 404 body, which only a real round trip through the stub can produce.
        StubHandler.last_path = None
        status, body, _ = _req(self.door_port, "GET", "/projects")
        self.assertEqual(StubHandler.last_path, "/projects")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "stub 404"})

    def test_no_dsn_with_active_days_is_503_never_empty_200(self):
        os.environ.pop("DOOR_PG_DSN", None)
        status, body, content_type = _req(self.door_port, "GET", "/projects?active_days=14")
        self.assertEqual(status, 503)
        self.assertIn("application/json", content_type)
        self.assertEqual(json.loads(body), {"error": "store not configured"})

    def test_out_of_range_active_days_is_400(self):
        os.environ["DOOR_PG_DSN"] = "postgresql://boring:boring@127.0.0.1:5432/boring"
        for bad in ("0", "-1", "366", "abc", "1.5"):
            status, body, _ = _req(self.door_port, "GET", f"/projects?active_days={bad}")
            self.assertEqual(status, 400, f"active_days={bad}")
            self.assertIn("active_days", json.loads(body)["error"])

    def test_stub_rows_answer_project_and_document_counts(self):
        os.environ["DOOR_PG_DSN"] = "postgresql://boring:boring@127.0.0.1:5432/boring"
        self._rows = [("foodspring-front", 72), ("ohmyboring", 42)]
        status, body, content_type = _req(self.door_port, "GET", "/projects?active_days=14")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        payload = json.loads(body)
        self.assertEqual(payload["active_days"], 14)
        self.assertEqual(
            payload["projects"],
            [{"project": "foodspring-front", "documents": 72}, {"project": "ohmyboring", "documents": 42}],
        )

    def test_active_days_at_bounds_is_200(self):
        os.environ["DOOR_PG_DSN"] = "postgresql://boring:boring@127.0.0.1:5432/boring"
        self._rows = []
        for ok in ("1", "365"):
            status, body, _ = _req(self.door_port, "GET", f"/projects?active_days={ok}")
            self.assertEqual(status, 200, f"active_days={ok}")
            self.assertEqual(json.loads(body)["active_days"], int(ok))


class ActiveDaysPureTests(unittest.TestCase):
    """door._check_active_days — pure: string in, bounded int or ValueError (AC1)."""

    def test_inside_bounds_passthrough(self):
        for ok in ("1", "14", "365"):
            self.assertEqual(door._check_active_days(ok), int(ok))

    def test_outside_bounds_is_a_value_error(self):
        for bad in ("0", "-1", "366", "1.5", "", "abc"):
            with self.assertRaises(ValueError, msg=f"active_days={bad!r}"):
                door._check_active_days(bad)


class ApprovedSelectTests(unittest.TestCase):
    """approved.select_approved — pure: rows in, partitioned dataclasses out (AC1)."""

    NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=approved.SEOUL)

    def _ts(self, hours_ago: float) -> str:
        at = self.NOW - timedelta(hours=hours_ago)
        return f"slack:C1:{at.timestamp()}"

    def test_window_in_and_out(self):
        rows = [
            ("session:" + self._ts(1), "/vault/wiki/wiki-1.md", "used"),
            ("session:" + self._ts(30), "/vault/wiki/wiki-2.md", "used"),
        ]
        selection = approved.select_approved(rows, since_hours=24, now=self.NOW)
        self.assertEqual([a.note for a in selection.approved], ["/vault/wiki/wiki-1.md"])
        self.assertEqual(selection.contested, [])
        self.assertEqual(selection.skipped, [])

    def test_used_and_contested_are_separated(self):
        rows = [
            ("session:" + self._ts(1), "/a.md", "used"),
            ("session:" + self._ts(2), "/b.md", "contested"),
        ]
        selection = approved.select_approved(rows, since_hours=24, now=self.NOW)
        self.assertEqual([a.note for a in selection.approved], ["/a.md"])
        self.assertEqual([c.note for c in selection.contested], ["/b.md"])

    def test_non_slack_session_is_skipped_not_raised(self):
        rows = [
            ("session:ff8b2f41-uuid-session", "/a.md", "used"),
            ("doc:/vault/wiki/wiki-9.md", "/b.md", "used"),
        ]
        selection = approved.select_approved(rows, since_hours=24, now=self.NOW)
        self.assertEqual(selection.approved, [])
        self.assertEqual(
            [(s.session, s.reason) for s in selection.skipped],
            [
                ("session:ff8b2f41-uuid-session", "not a slack card session"),
                ("doc:/vault/wiki/wiki-9.md", "not a slack card session"),
            ],
        )

    def test_session_name_without_ts_is_a_skipped_value(self):
        rows = [("session:slack:C1", "/a.md", "used")]
        selection = approved.select_approved(rows, since_hours=24, now=self.NOW)
        self.assertEqual(selection.approved, [])
        self.assertEqual(selection.skipped[0].session, "session:slack:C1")
        self.assertEqual(selection.skipped[0].reason, "no parseable ts in session name")

    def test_sorted_newest_first(self):
        rows = [
            ("session:" + self._ts(3), "/old.md", "used"),
            ("session:" + self._ts(0.5), "/new.md", "used"),
            ("session:" + self._ts(2), "/mid.md", "used"),
        ]
        selection = approved.select_approved(rows, since_hours=24, now=self.NOW)
        self.assertEqual([a.note for a in selection.approved], ["/new.md", "/mid.md", "/old.md"])

    def test_parse_session_ts(self):
        at = approved.parse_session_ts("slack:C1:1758470000.5")
        self.assertEqual(at.tzinfo, approved.SEOUL)
        self.assertEqual(at.timestamp(), 1758470000.5)
        # the edge src carries a `session:` prefix — same answer
        self.assertEqual(approved.parse_session_ts("session:slack:C1:1758470000.5"), at)
        self.assertIsNone(approved.parse_session_ts("slack:C1"))
        self.assertIsNone(approved.parse_session_ts("session:ff8b2f41-uuid"))


class ApprovedRouteTests(unittest.TestCase):
    """GET /approved through the real door app — stub rows, no DB (AC2)."""

    @classmethod
    def setUpClass(cls):
        cls.door_port = _free_port()
        cls.door_server = uvicorn.Server(
            uvicorn.Config(door.app, host="127.0.0.1", port=cls.door_port, log_level="warning")
        )
        cls.door_thread = threading.Thread(target=cls.door_server.run, daemon=True)
        cls.door_thread.start()
        deadline = 10.0
        while deadline > 0:
            try:
                with socket.create_connection(("127.0.0.1", cls.door_port), timeout=1):
                    break
            except OSError:
                pass
            time.sleep(0.05)
            deadline -= 0.05
        else:
            raise RuntimeError("door did not come up within 10s")

    @classmethod
    def tearDownClass(cls):
        cls.door_server.should_exit = True

    def setUp(self):
        self._saved_dsn = os.environ.get("DOOR_PG_DSN")
        self._rows: list[tuple[str, str, str]] = []
        self._orig_fetch = door._fetch_edges
        door._fetch_edges = lambda: self._rows

    def tearDown(self):
        door._fetch_edges = self._orig_fetch
        if self._saved_dsn is None:
            os.environ.pop("DOOR_PG_DSN", None)
        else:
            os.environ["DOOR_PG_DSN"] = self._saved_dsn

    def test_no_dsn_is_503_never_empty_200(self):
        os.environ.pop("DOOR_PG_DSN", None)
        status, body, content_type = _req(self.door_port, "GET", "/approved")
        self.assertEqual(status, 503)
        self.assertIn("application/json", content_type)
        self.assertEqual(json.loads(body), {"error": "store not configured"})

    def test_stub_rows_answer_the_adt(self):
        os.environ["DOOR_PG_DSN"] = "postgresql://boring:boring@127.0.0.1:5432/boring"
        now = datetime.now(tz=approved.SEOUL)
        self._rows = [
            (
                f"session:slack:C1:{(now - timedelta(hours=2)).timestamp()}",
                "/vault/wiki/wiki-1734.md",
                "used",
            ),
            (
                f"session:slack:C1:{(now - timedelta(hours=1)).timestamp()}",
                "/vault/wiki/wiki-1735.md",
                "contested",
            ),
            ("session:ff8b2f41-uuid", "/vault/wiki/wiki-1700.md", "used"),
        ]
        status, body, _ = _req(self.door_port, "GET", "/approved?since_hours=48")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["since_hours"], 48)
        self.assertEqual(len(payload["approved"]), 1)
        self.assertEqual(payload["approved"][0]["note"], "/vault/wiki/wiki-1734.md")
        self.assertTrue(payload["approved"][0]["session"].startswith("slack:C1:"))
        self.assertEqual(payload["contested"][0]["note"], "/vault/wiki/wiki-1735.md")
        self.assertIn("+09:00", payload["approved"][0]["at"])
        # the uuid session must not leak into either list
        self.assertNotIn("/vault/wiki/wiki-1700.md", json.dumps(payload))

    def test_bad_since_hours_is_400(self):
        status, body, _ = _req(self.door_port, "GET", "/approved?since_hours=abc")
        self.assertEqual(status, 400)
        self.assertIn("since_hours", json.loads(body)["error"])

    def test_since_hours_outside_bounds_is_400(self):
        for bad in ("0", "-1", "1.5", "99999999999999", "8761"):
            status, body, _ = _req(self.door_port, "GET", f"/approved?since_hours={bad}")
            self.assertEqual(status, 400, f"since_hours={bad}")
            self.assertIn("application/json", _)
            self.assertIn("since_hours", json.loads(body)["error"])

    def test_since_hours_at_bounds_is_200(self):
        os.environ["DOOR_PG_DSN"] = "postgresql://boring:boring@127.0.0.1:5432/boring"
        self._rows = []
        for ok in ("1", "8760"):
            status, body, _ = _req(self.door_port, "GET", f"/approved?since_hours={ok}")
            self.assertEqual(status, 200, f"since_hours={ok}")
            self.assertEqual(json.loads(body)["since_hours"], int(ok))


class SinceHoursPureTests(unittest.TestCase):
    """approved.check_since_hours — pure: string in, bounded int or ValueError (AC1)."""

    def test_inside_bounds_passthrough(self):
        for ok in ("1", "24", "8760"):
            self.assertEqual(approved.check_since_hours(ok), int(ok))

    def test_outside_bounds_is_a_value_error(self):
        for bad in ("0", "-1", "1.5", "99999999999999", "8761", "", "abc"):
            with self.assertRaises(ValueError, msg=f"since_hours={bad!r}"):
                approved.check_since_hours(bad)


class ClaimSourcePureTests(unittest.TestCase):
    """claim_source.pick_current — pure: rows in, newest claim + candidates out (AC2)."""

    def test_newest_valid_from_wins_and_ascending_input_is_sorted(self):
        rows = [
            ("/vault/wiki/wiki-1001.md", datetime(2026, 9, 1, tzinfo=approved.SEOUL), "v1", "unknown"),
            ("/vault/wiki/wiki-1405.md", datetime(2026, 9, 20, tzinfo=approved.SEOUL), "v2", "inferred"),
            ("/vault/wiki/wiki-1200.md", datetime(2026, 9, 10, tzinfo=approved.SEOUL), "v3", "agent:hermes"),
        ]
        out = claim_source.pick_current("3차 창 샘플링", rows)
        self.assertEqual(out["subject"], "3차 창 샘플링")
        self.assertEqual(out["note"], "/vault/wiki/wiki-1405.md")
        self.assertEqual(out["valid_from"], "2026-09-20T00:00:00+09:00")
        self.assertEqual(out["value"], "v2", "the current claim's value rides the answer")
        self.assertEqual(
            [c["note"] for c in out["candidates"]],
            ["/vault/wiki/wiki-1405.md", "/vault/wiki/wiki-1200.md", "/vault/wiki/wiki-1001.md"],
        )

    def test_an_owner_claim_wins_over_a_newer_one(self):
        rows = [
            ("/vault/wiki/wiki-1405.md", datetime(2026, 9, 20, tzinfo=approved.SEOUL), "any day", "agent:x"),
            ("/vault/wiki/wiki-1001.md", datetime(2026, 9, 1, tzinfo=approved.SEOUL), "not friday", "owner"),
        ]
        out = claim_source.pick_current("배포 요일", rows)
        self.assertEqual((out["note"], out["value"]), ("/vault/wiki/wiki-1001.md", "not friday"))

    def test_empty_rows_is_none_the_doors_404(self):
        self.assertIsNone(claim_source.pick_current("없는 주어", []))

    def test_group_current_picks_once_per_subject(self):
        rows = [
            ("s-b", "/vault/wiki/wiki-3.md", datetime(2026, 9, 3, tzinfo=approved.SEOUL), "b", "unknown"),
            (
                "s-a",
                "/vault/wiki/wiki-2.md",
                datetime(2026, 9, 20, tzinfo=approved.SEOUL),
                "newer",
                "agent:x",
            ),
            ("s-a", "/vault/wiki/wiki-1.md", datetime(2026, 9, 1, tzinfo=approved.SEOUL), "owner's", "owner"),
        ]
        out = claim_source.group_current(rows)
        self.assertEqual([(c["subject"], c["value"]) for c in out], [("s-a", "owner's"), ("s-b", "b")])
        self.assertEqual(len(out[0]["candidates"]), 2)
        self.assertEqual(claim_source.group_current([]), [])


class ClaimSourceRouteTests(unittest.TestCase):
    """GET /claim-source through the real door app — stub rows, no DB (AC2)."""

    @classmethod
    def setUpClass(cls):
        cls.door_port = _free_port()
        cls.door_server = uvicorn.Server(
            uvicorn.Config(door.app, host="127.0.0.1", port=cls.door_port, log_level="warning")
        )
        cls.door_thread = threading.Thread(target=cls.door_server.run, daemon=True)
        cls.door_thread.start()
        deadline = 10.0
        while deadline > 0:
            try:
                with socket.create_connection(("127.0.0.1", cls.door_port), timeout=1):
                    break
            except OSError:
                pass
            time.sleep(0.05)
            deadline -= 0.05
        else:
            raise RuntimeError("door did not come up within 10s")

    @classmethod
    def tearDownClass(cls):
        cls.door_server.should_exit = True

    def setUp(self):
        self._saved_dsn = os.environ.get("DOOR_PG_DSN")
        self._rows: list = []
        self._orig_fetch = door._fetch_claim_source
        door._fetch_claim_source = lambda subject: self._rows
        self._orig_fetch_list = door._fetch_claim_sources
        door._fetch_claim_sources = lambda predicate: self._rows

    def tearDown(self):
        door._fetch_claim_source = self._orig_fetch
        door._fetch_claim_sources = self._orig_fetch_list
        if self._saved_dsn is None:
            os.environ.pop("DOOR_PG_DSN", None)
        else:
            os.environ["DOOR_PG_DSN"] = self._saved_dsn

    def test_no_dsn_is_503(self):
        os.environ.pop("DOOR_PG_DSN", None)
        status, body, _ = _req(self.door_port, "GET", "/claim-source?subject=x")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"error": "store not configured"})

    def test_empty_subject_is_400(self):
        for path in ("/claim-source", "/claim-source?subject="):
            status, body, _ = _req(self.door_port, "GET", path)
            self.assertEqual(status, 400, path)
            self.assertIn("subject", json.loads(body)["error"])

    def test_unknown_subject_is_404_json(self):
        os.environ["DOOR_PG_DSN"] = "postgresql://boring:boring@127.0.0.1:5432/boring"
        self._rows = []
        status, body, _ = _req(self.door_port, "GET", "/claim-source?subject=nobody")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "no current claim for subject"})

    def test_stub_rows_answer_the_note_path(self):
        os.environ["DOOR_PG_DSN"] = "postgresql://boring:boring@127.0.0.1:5432/boring"
        self._rows = [
            ("/vault/wiki/wiki-1405.md", datetime(2026, 9, 20, tzinfo=approved.SEOUL), "v2", "unknown"),
            ("/vault/wiki/wiki-1001.md", datetime(2026, 9, 1, tzinfo=approved.SEOUL), "v1", "unknown"),
        ]
        subject = urllib.parse.quote("3차 창 샘플링")
        status, body, content_type = _req(self.door_port, "GET", f"/claim-source?subject={subject}")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        payload = json.loads(body)
        self.assertEqual(payload["subject"], "3차 창 샘플링")
        self.assertEqual(payload["note"], "/vault/wiki/wiki-1405.md")
        self.assertEqual(payload["value"], "v2", "the current claim's value rides the answer")
        self.assertEqual(len(payload["candidates"]), 2)

    def test_claim_sources_lists_one_pick_per_subject_400_and_503(self):
        status, body, _ = _req(self.door_port, "GET", "/claim-sources?predicate=")
        self.assertEqual((status, json.loads(body)["error"]), (400, "predicate query param is required"))
        os.environ.pop("DOOR_PG_DSN", None)
        status, _, _ = _req(self.door_port, "GET", "/claim-sources?predicate=p")
        self.assertEqual(status, 503)
        os.environ["DOOR_PG_DSN"] = "postgresql://boring:boring@127.0.0.1:5432/boring"
        self._rows = [
            ("s-b", "/vault/wiki/wiki-3.md", datetime(2026, 9, 3, tzinfo=approved.SEOUL), "b", "unknown"),
            ("s-a", "/vault/wiki/wiki-1.md", datetime(2026, 9, 1, tzinfo=approved.SEOUL), "a", "unknown"),
        ]
        status, body, _ = _req(self.door_port, "GET", "/claim-sources?predicate=p")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["predicate"], "p")
        self.assertEqual([c["subject"] for c in payload["claims"]], ["s-a", "s-b"])


class SplitSubjectsGetRouteTests(unittest.TestCase):
    """GET /repairs/split-subjects through the real door app — stub rows, no DB (AC2).
    One test: shape, rows-desc sort, limit application, out-of-range limit, missing DSN."""

    @classmethod
    def setUpClass(cls):
        cls.door_port = _free_port()
        cls.door_server = uvicorn.Server(
            uvicorn.Config(door.app, host="127.0.0.1", port=cls.door_port, log_level="warning")
        )
        cls.door_thread = threading.Thread(target=cls.door_server.run, daemon=True)
        cls.door_thread.start()
        deadline = 10.0
        while deadline > 0:
            try:
                with socket.create_connection(("127.0.0.1", cls.door_port), timeout=1):
                    break
            except OSError:
                pass
            time.sleep(0.05)
            deadline -= 0.05
        else:
            raise RuntimeError("door did not come up within 10s")

    @classmethod
    def tearDownClass(cls):
        cls.door_server.should_exit = True

    def setUp(self):
        self._saved_dsn = os.environ.get("DOOR_PG_DSN")
        self._rows: list[tuple[str, str]] = []
        self._orig_fetch = door._fetch_split_subject_rows
        door._fetch_split_subject_rows = lambda: self._rows

    def tearDown(self):
        door._fetch_split_subject_rows = self._orig_fetch
        if self._saved_dsn is None:
            os.environ.pop("DOOR_PG_DSN", None)
        else:
            os.environ["DOOR_PG_DSN"] = self._saved_dsn

    def test_shape_sort_limit_and_missing_dsn(self):
        os.environ.pop("DOOR_PG_DSN", None)
        status, body, _ = _req(self.door_port, "GET", "/repairs/split-subjects")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"error": "store not configured"})

        os.environ["DOOR_PG_DSN"] = "postgresql://boring:boring@127.0.0.1:5432/boring"
        self._rows = [
            ("kb rag bot", "/a.md"),
            ("kb-rag-bot", "/b.md"),
            ("foodspring front", "/c.md"),
            ("foodspring-front", "/d.md"),
            ("foodspring-front", "/e.md"),
            ("solo-subject", "/f.md"),  # single spelling — never a group
        ]
        status, body, content_type = _req(self.door_port, "GET", "/repairs/split-subjects?limit=1")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        payload = json.loads(body)
        self.assertEqual(payload["total_groups"], 2)
        self.assertEqual(len(payload["groups"]), 1)
        self.assertEqual(payload["groups"][0]["subject"], "foodspring-front")
        self.assertEqual(payload["groups"][0]["variants"], ["foodspring front", "foodspring-front"])
        self.assertEqual(payload["groups"][0]["rows"], 3)
        self.assertEqual(payload["groups"][0]["notes"], 3)

        for bad in ("0", "-1", "51", "abc"):
            status, body, _ = _req(self.door_port, "GET", f"/repairs/split-subjects?limit={bad}")
            self.assertEqual(status, 400, f"limit={bad}")
            self.assertIn("limit", json.loads(body)["error"])


class SplitSubjectsPostRouteTests(unittest.TestCase):
    """POST /repairs/split-subjects through the real door app — stub cursor + stub sync
    callable, never a live DB or a live engine (AC3)."""

    @classmethod
    def setUpClass(cls):
        cls.door_port = _free_port()
        cls.door_server = uvicorn.Server(
            uvicorn.Config(door.app, host="127.0.0.1", port=cls.door_port, log_level="warning")
        )
        cls.door_thread = threading.Thread(target=cls.door_server.run, daemon=True)
        cls.door_thread.start()
        deadline = 10.0
        while deadline > 0:
            try:
                with socket.create_connection(("127.0.0.1", cls.door_port), timeout=1):
                    break
            except OSError:
                pass
            time.sleep(0.05)
            deadline -= 0.05
        else:
            raise RuntimeError("door did not come up within 10s")

    @classmethod
    def tearDownClass(cls):
        cls.door_server.should_exit = True

    def setUp(self):
        self._saved_dsn = os.environ.get("DOOR_PG_DSN")
        os.environ["DOOR_PG_DSN"] = "postgresql://boring:boring@127.0.0.1:5432/boring"
        self.order: list[str] = []
        self._before_rows = [
            ("foodspring front", "/a.md"),
            ("foodspring-front", "/b.md"),
        ]
        self._after_rows = [("foodspring-front", "/a.md"), ("foodspring-front", "/b.md")]
        self._fetch_calls = 0
        self._orig_connect = door._split_subjects_connect
        self._orig_fetch = door._fetch_split_subject_rows
        self._orig_sync = door._call_engine_sync
        self._orig_emit = door._emit_subject_merged_event
        self._saved_token = os.environ.pop("BORING_OWNER_TOKEN", None)
        order = self.order
        self.owner_notes: set[str] = set()
        self.touched: dict[str, list[str]] = {}
        owner_notes = self.owner_notes
        touched = self.touched

        class _StubCursor:
            def __enter__(self_c):
                return self_c

            def __exit__(self_c, *exc):
                return False

            def execute(self_c, sql, params):
                if sql is door._OWNER_NOTES_SQL:
                    self_c._fetched = [(p,) for p in params[0] if p in owner_notes]
                elif sql is door._SPLIT_DELETE_SQL:
                    order.append("delete")
                    touched["delete"] = list(params[1])
                    self_c.rowcount = len(params[1])
                elif sql is door._SPLIT_UPDATE_SQL:
                    order.append("update")
                    touched["update"] = list(params[0])
                    self_c.rowcount = len(params[0])

            def fetchall(self_c):
                return self_c._fetched

        class _StubConn:
            def cursor(self_c):
                return _StubCursor()

            def commit(self_c):
                order.append("commit")

            def close(self_c):
                pass

        def fake_connect():
            return _StubConn()

        def fake_fetch():
            self._fetch_calls += 1
            return self._before_rows if self._fetch_calls == 1 else self._after_rows

        def fake_sync():
            order.append("sync")
            return self._sync_result

        def fake_emit(subject, deleted_rows, reread_notes, remaining_variants, owner_held):
            order.append("event")
            touched["event_owner_held"] = owner_held
            return "delivered"

        def fake_reread_in_background(merged):
            order.append("reread-started")
            touched["reread_args"] = [
                merged.subject,
                merged.deleted_rows,
                merged.reread_notes,
                merged.owner_held,
            ]
            touched["reread_row"] = merged.row

        self._sync_result = {"ok": True, "summary": {"ingest_new": 0, "ingest_repaired": 2}}
        self._orig_reread = door._reread_in_background
        door._split_subjects_connect = fake_connect
        door._fetch_split_subject_rows = fake_fetch
        door._call_engine_sync = fake_sync
        door._emit_subject_merged_event = fake_emit
        door._reread_in_background = fake_reread_in_background

    def tearDown(self):
        door._split_subjects_connect = self._orig_connect
        door._fetch_split_subject_rows = self._orig_fetch
        door._call_engine_sync = self._orig_sync
        door._emit_subject_merged_event = self._orig_emit
        door._reread_in_background = self._orig_reread
        if self._saved_dsn is None:
            os.environ.pop("DOOR_PG_DSN", None)
        else:
            os.environ["DOOR_PG_DSN"] = self._saved_dsn
        os.environ.pop("BORING_OWNER_TOKEN", None)
        if self._saved_token is not None:
            os.environ["BORING_OWNER_TOKEN"] = self._saved_token

    def _post(self, subject: str, token: str | None = None, row=None):
        payload = json.dumps({"subject": subject, **({} if row is None else {"row": row})}).encode()
        headers = {"content-type": "application/json"}
        if token is not None:
            headers["X-Boring-Owner-Token"] = token
        return _req(
            self.door_port,
            "POST",
            "/repairs/split-subjects",
            body=payload,
            headers=headers,
        )

    def test_owner_note_is_held_without_the_owner_token(self):
        self.owner_notes.add("/a.md")
        self._after_rows = [("foodspring front", "/a.md"), ("foodspring-front", "/b.md")]
        os.environ["BORING_OWNER_TOKEN"] = "tok-owner"

        for token in (None, "wrong"):
            self._fetch_calls = 0
            status, body, _ = self._post("foodspring-front", token)
            self.assertEqual(status, 202)
            out = json.loads(body)
            self.assertEqual(self.touched["delete"], ["/b.md"], f"token={token}")
            self.assertEqual(self.touched["update"], ["/b.md"], f"token={token}")
            self.assertEqual(out["owner_held"], ["/a.md"])
            self.assertEqual(out["deleted_rows"], 1)
            self.assertEqual(self.touched["reread_args"], ["foodspring-front", 1, 1, ["/a.md"]])

        self._fetch_calls = 0
        status, body, _ = self._post("foodspring-front", "tok-owner")
        self.assertEqual(status, 202)
        self.assertEqual(
            self.touched["delete"], ["/a.md", "/b.md"], "control: the owner token merges owner rows"
        )
        self.assertEqual(json.loads(body)["owner_held"], [])

    def test_unknown_subject_and_answer_before_reread(self):
        # (a) unknown canon form — 404, nothing was touched
        status, body, _ = self._post("nobody-canonical")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))
        self.assertEqual(self.order, [])

        # (b) the answer comes once the merge is committed: the reread is started, not awaited —
        # waiting on a whole-vault /sync past the door's timeout reported a done merge as failed
        # and put its buttons back (2026-09-29)
        self._fetch_calls = 0
        status, body, _ = self._post("foodspring-front")
        self.assertEqual(status, 202)
        self.assertEqual(self.order, ["delete", "update", "commit", "reread-started"])
        out = json.loads(body)
        self.assertEqual(out["deleted_rows"], 2)
        self.assertEqual(out["reread_notes"], 2)
        self.assertIsNone(out["remaining_variants"])
        self.assertEqual(out["sync"], "started")
        self.assertEqual(self.touched["delete"], ["/a.md", "/b.md"])
        self.assertEqual(out["owner_held"], [])

    def test_the_card_row_rides_to_the_reread_and_a_malformed_one_is_a_400(self):
        row = {"channel": "C1", "card_ts": "1.0", "idx": 2}
        status, _, _ = self._post("foodspring-front", row=row)
        self.assertEqual(status, 202)
        self.assertEqual(self.touched["reread_row"], door.card_types.RowRef(**row))

        self.order.clear()
        status, _, _ = self._post("foodspring-front", row={"channel": "C1"})
        self.assertEqual(status, 400)
        self.assertEqual(self.order, [], "a malformed row touches nothing")

    def test_reread_recounts_and_records_or_says_it_failed(self):
        self._fetch_calls = 1
        merged = door._Merged("foodspring-front", 2, 2, [], None)
        door._reread_batch([merged])
        self.assertEqual(self.order, ["sync", "event"])

        self.order.clear()
        self._sync_result = {"ok": False, "error": "engine unreachable: timed out"}
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            door._reread_batch([merged])
        self.assertEqual(self.order, ["sync"], "no subject_merged event for a reread that did not happen")
        self.assertIn("foodspring-front", buf.getvalue())
        self.assertIn("timed out", buf.getvalue())


class EmitSubjectMergedEventTests(unittest.TestCase):
    """F3 (2026-09-22): _emit_subject_merged_event's own status handling, not the wholesale
    stub the POST-order test above uses. A non-2xx /events answer must be reported (one
    stderr line naming the subject and the status) and reflected in the return value the
    caller folds into the POST response — it must not read as a silent success."""

    def test_non_2xx_events_answer_is_reported_on_stderr_and_returned(self):
        orig_fetch = door._fetch

        def fake_fetch(url, body, headers, method):
            return door._pass_through(500, b'{"error":"boom"}', "application/json")

        door._fetch = fake_fetch
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                status = door._emit_subject_merged_event("foodspring-front", 2, 2, 1, [])
        finally:
            door._fetch = orig_fetch
        self.assertEqual(status, "failed: 500")
        self.assertIn("foodspring-front", buf.getvalue())
        self.assertIn("500", buf.getvalue())


class RulesGroupPureTests(unittest.TestCase):
    """rules.group_rules — pure: claim rows in, grouped rules + incomplete count out."""

    def _rows(self):
        return [
            ("rule-b", "rule", "b sentence", "/vault/wiki/wiki-2.md"),
            ("rule-b", "trigger", "bbb", "/vault/wiki/wiki-2.md"),
            ("rule-a", "trigger", "hermes 헤르메스 + 제거", "/vault/wiki/wiki-1.md"),
            ("rule-a", "rule", "a newer sentence", "/vault/wiki/wiki-9.md"),
            ("rule-a", "rule", "a sentence", "/vault/wiki/wiki-1.md"),
            ("rule-c", "rule", "c has no trigger", "/vault/wiki/wiki-3.md"),
            ("rule-d", "trigger", "d has no rule", "/vault/wiki/wiki-4.md"),
            ("rule-e", "note", "unrelated predicate is ignored", "/vault/wiki/wiki-5.md"),
        ]

    def test_grouped_by_subject_sorted_with_newest_rule_winning(self):
        out = rules.group_rules(self._rows())
        self.assertEqual([r["subject"] for r in out["rules"]], ["rule-a", "rule-b"])
        a = out["rules"][0]
        self.assertEqual(a["rule"], "a newer sentence", "rows arrive newest-first; the first claim wins")
        self.assertEqual(a["trigger"], "hermes 헤르메스 + 제거")
        self.assertEqual(a["source_path"], "/vault/wiki/wiki-9.md")
        self.assertEqual(
            out["rules"][1],
            {
                "subject": "rule-b",
                "rule": "b sentence",
                "trigger": "bbb",
                "source_path": "/vault/wiki/wiki-2.md",
            },
        )

    def test_subjects_missing_rule_or_trigger_are_counted_not_dropped_silently(self):
        out = rules.group_rules(self._rows())
        self.assertEqual(out["incomplete"], 3, "rule-c (no trigger), rule-d (no rule), rule-e (neither)")
        self.assertNotIn("rule-c", [r["subject"] for r in out["rules"]])

    def test_empty_rows_is_an_empty_answer(self):
        self.assertEqual(rules.group_rules([]), {"rules": [], "incomplete": 0})


class RulesRouteTests(unittest.TestCase):
    """GET /rules through the real door app — stub rows, no DB."""

    @classmethod
    def setUpClass(cls):
        cls.door_port = _free_port()
        cls.door_server = uvicorn.Server(
            uvicorn.Config(door.app, host="127.0.0.1", port=cls.door_port, log_level="warning")
        )
        cls.door_thread = threading.Thread(target=cls.door_server.run, daemon=True)
        cls.door_thread.start()
        deadline = 10.0
        while deadline > 0:
            try:
                with socket.create_connection(("127.0.0.1", cls.door_port), timeout=1):
                    break
            except OSError:
                pass
            time.sleep(0.05)
            deadline -= 0.05
        else:
            raise RuntimeError("door did not come up within 10s")

    @classmethod
    def tearDownClass(cls):
        cls.door_server.should_exit = True

    def setUp(self):
        self._saved_dsn = os.environ.get("DOOR_PG_DSN")
        self._rows: list = []
        self._orig_fetch = door._fetch_rules
        door._fetch_rules = lambda: self._rows

    def tearDown(self):
        door._fetch_rules = self._orig_fetch
        if self._saved_dsn is None:
            os.environ.pop("DOOR_PG_DSN", None)
        else:
            os.environ["DOOR_PG_DSN"] = self._saved_dsn

    def test_no_dsn_is_503_never_empty_200(self):
        os.environ.pop("DOOR_PG_DSN", None)
        status, body, content_type = _req(self.door_port, "GET", "/rules")
        self.assertEqual(status, 503)
        self.assertIn("application/json", content_type)
        self.assertEqual(json.loads(body), {"error": "store not configured"})

    def test_stub_rows_answer_grouped_rules_with_incomplete_count(self):
        os.environ["DOOR_PG_DSN"] = "postgresql://boring:boring@127.0.0.1:5432/boring"
        self._rows = [
            ("rule-hermes-not-removed", "rule", "hermes 를 빼지 마세요", "/vault/wiki/wiki-1955.md"),
            (
                "rule-hermes-not-removed",
                "trigger",
                "hermes 헤르메스 + 제거|빼|내리|remove|drop",
                "/vault/wiki/wiki-1955.md",
            ),
            ("rule-only-rule", "rule", "no trigger here", "/vault/wiki/wiki-1960.md"),
        ]
        status, body, content_type = _req(self.door_port, "GET", "/rules")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        payload = json.loads(body)
        self.assertEqual(payload["incomplete"], 1)
        self.assertEqual(
            payload["rules"],
            [
                {
                    "subject": "rule-hermes-not-removed",
                    "rule": "hermes 를 빼지 마세요",
                    "trigger": "hermes 헤르메스 + 제거|빼|내리|remove|drop",
                    "source_path": "/vault/wiki/wiki-1955.md",
                }
            ],
        )


class RunCardRouteTests(unittest.TestCase):
    """POST /run/morning-card · /run/weekly-card — the door runs the card programs as
    subprocesses (stubbed here) and answers {ok, exit, posted_ts?, tail}. Covered: a healthy
    run, a non-zero exit surfacing as ok:false with the code (never hidden), the tool's
    "already posted" line parsing into posted_ts, a timeout, and one run at a time per route
    (a second call while one runs is 409)."""

    @classmethod
    def setUpClass(cls):
        cls.door_port = _free_port()
        cls.door_server = uvicorn.Server(
            uvicorn.Config(door.app, host="127.0.0.1", port=cls.door_port, log_level="warning")
        )
        cls.door_thread = threading.Thread(target=cls.door_server.run, daemon=True)
        cls.door_thread.start()
        deadline = 10.0
        while deadline > 0:
            try:
                with socket.create_connection(("127.0.0.1", cls.door_port), timeout=1):
                    break
            except OSError:
                pass
            time.sleep(0.05)
            deadline -= 0.05
        else:
            raise RuntimeError("door did not come up within 10s")

    @classmethod
    def tearDownClass(cls):
        cls.door_server.should_exit = True

    def test_a_healthy_run_answers_ok_with_the_ts(self):
        calls = []

        def fake_run(script, timeout_s):
            calls.append((script, timeout_s))
            return subprocess.CompletedProcess(
                ["x"], 0, stdout="[card] posted ts=1727480000.000100\n", stderr=""
            )

        with mock.patch.object(door, "_run_card_subprocess", fake_run):
            status, body, content_type = _req(self.door_port, "POST", "/run/morning-card")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        payload = json.loads(body)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["exit"], 0)
        self.assertEqual(payload["posted_ts"], "1727480000.000100")
        self.assertIn("posted ts=", payload["tail"])
        self.assertTrue(str(calls[0][0]).endswith(os.path.join("agents", "slack", "card.py")))

    def test_the_weekly_route_runs_the_weekly_program(self):
        calls = []

        def fake_run(script, timeout_s):
            calls.append(script)
            return subprocess.CompletedProcess(["x"], 0, stdout="[weekly] posted ts=2.0\n", stderr="")

        with mock.patch.object(door, "_run_card_subprocess", fake_run):
            status, body, _ = _req(self.door_port, "POST", "/run/weekly-card")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["posted_ts"], "2.0")
        self.assertTrue(
            str(calls[0]).endswith(os.path.join("agents", "slack", "weekly_card.py")),
            "the weekly route must execute the weekly poster, not the morning card",
        )

    def test_a_nonzero_exit_is_ok_false_with_the_code_and_tail(self):
        """The mutation this kills: the route folding a failed card into ok:true — the exit
        code and the tool's own refusal line must reach the caller verbatim."""

        def fake_run(script, timeout_s):
            return subprocess.CompletedProcess(
                ["x"], 3, stdout="", stderr="[card] 카드 거부: 문이 응답하지 않음\n"
            )

        with mock.patch.object(door, "_run_card_subprocess", fake_run):
            status, body, _ = _req(self.door_port, "POST", "/run/morning-card")
        self.assertEqual(
            status, 200, "the run completed — the tool's answer rides in the body, not the status"
        )
        payload = json.loads(body)
        self.assertEqual(payload["ok"], False)
        self.assertEqual(payload["exit"], 3)
        self.assertIn("카드 거부", payload["tail"])
        self.assertNotIn("posted_ts", payload)

    def test_the_already_posted_line_still_carries_the_ts(self):
        def fake_run(script, timeout_s):
            return subprocess.CompletedProcess(
                ["x"], 0, stdout="[card] already posted today (ts=1727480000.999900)\n", stderr=""
            )

        with mock.patch.object(door, "_run_card_subprocess", fake_run):
            status, body, _ = _req(self.door_port, "POST", "/run/morning-card")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["exit"], 0)
        self.assertEqual(payload["posted_ts"], "1727480000.999900")

    def test_a_timeout_is_ok_false_with_a_null_exit(self):
        def fake_run(script, timeout_s):
            raise subprocess.TimeoutExpired(["x"], timeout_s)

        with mock.patch.object(door, "_run_card_subprocess", fake_run):
            status, body, _ = _req(self.door_port, "POST", "/run/weekly-card")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["ok"], False)
        self.assertIsNone(payload["exit"])
        self.assertIn("timeout", payload["error"])

    def test_a_second_call_while_one_runs_is_409(self):
        entered = threading.Event()
        release = threading.Event()
        results = []
        results_lock = threading.Lock()

        def slow_run(script, timeout_s):
            entered.set()
            release.wait(15)
            return subprocess.CompletedProcess(["x"], 0, stdout="[card] posted ts=3.0\n", stderr="")

        def fire():
            result = _req(self.door_port, "POST", "/run/morning-card")
            with results_lock:
                results.append(result)

        try:
            with mock.patch.object(door, "_run_card_subprocess", slow_run):
                first = threading.Thread(target=fire)
                first.start()
                self.assertTrue(entered.wait(10), "the first run never started")
                fire()  # the second call lands while the first still holds the route
                release.set()
                first.join(10)
        finally:
            release.set()

        self.assertEqual(len(results), 2)
        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, [200, 409], f"one run completes, the concurrent one is refused: {results}")
        by_status = {r[0]: json.loads(r[1]) for r in results}
        self.assertEqual(by_status[409], {"error": "morning-card already running"})
        self.assertEqual(by_status[200]["ok"], True)
        self.assertEqual(by_status[200]["posted_ts"], "3.0")


class _FakeWeb:
    """A Slack message the door reads and rewrites — history returns it as it stands now."""

    def __init__(self, blocks):
        self.blocks = blocks
        self.updates = []

    def conversations_history(self, **kw):
        return {"messages": [{"ts": "1.0", "blocks": self.blocks}]}

    def chat_update(self, *, channel, ts, blocks):
        self.updates.append((channel, ts))
        self.blocks = blocks


def _card_with_pending_rows(n):
    repairs = [
        door.card_types.Repair(subject=f"subj-{i}", variants=[f"subj {i}", f"subj-{i}"], rows=3, notes=2)
        for i in range(n)
    ]
    blocks = door.card_view.build_blocks([], repairs=repairs, repairs_total_groups=n, lang="ko")
    for i in range(n):
        blocks = door.card_view.mark_progress(blocks, i, door.card_types.Pending(), lang="ko")
    return blocks


class RereadSingleFlightTests(unittest.TestCase):
    """Three merges landing while a reread runs must not start three /sync passes (2026-09-30
    08:04: three overlapped, two timed out), and each merge's card row settles only once the
    reread that covered it has ended."""

    def setUp(self):
        self.syncs = 0
        self.running = 0
        self.peak = 0
        self.count_lock = threading.Lock()
        self.sync_result = {"ok": True, "summary": {}}
        self.gate = threading.Event()
        self.first_entered = threading.Event()
        self.second_entered = threading.Event()
        self.events = []
        self.web = _FakeWeb(_card_with_pending_rows(3))
        self.patches = [
            mock.patch.object(door, "_call_engine_sync", self._sync),
            mock.patch.object(door, "_fetch_split_subject_rows", lambda: [("subj-0", "/a.md")]),
            mock.patch.object(door, "_emit_subject_merged_event", self._emit),
            mock.patch.object(door, "_slack_client", lambda: self.web),
            mock.patch.object(door.boring_config, "note_lang", lambda: "ko"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        self.gate.set()
        worker = door._REREAD_WORKER
        if worker is not None:
            worker.join(10)
        for p in self.patches:
            p.stop()

    def _sync(self):
        with self.count_lock:
            self.syncs += 1
            self.running += 1
            self.peak = max(self.peak, self.running)
            if self.running > 1:
                self.second_entered.set()
        self.first_entered.set()
        self.gate.wait(10)
        with self.count_lock:
            self.running -= 1
        return self.sync_result

    def _emit(self, subject, *rest):
        self.events.append(subject)
        return "delivered"

    def _merge(self, idx):
        row = door.card_types.RowRef(channel="C1", card_ts="1.0", idx=idx)
        door._reread_in_background(door._Merged(f"subj-{idx}", 3, 2, [], row))

    def _run_three(self):
        self._merge(0)
        self.assertTrue(self.first_entered.wait(10))
        self._merge(1)
        self._merge(2)
        # A second /sync starting while the first is held is the defect; give it the chance to.
        self.second_entered.wait(0.5)
        worker = door._REREAD_WORKER
        self.gate.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())

    def _texts(self):
        return [
            b["elements"][0]["text"]
            for b in self.web.blocks
            if str(b.get("block_id", "")).endswith(":status")
        ]

    def test_three_merges_run_at_most_two_syncs_and_settle_all_three_rows(self):
        self._run_three()
        self.assertEqual(self.peak, 1, "two /sync passes overlapped")
        self.assertLessEqual(self.syncs, 2)
        self.assertEqual(
            self.syncs, 2, "control: the merges queued behind the first still get their own reread"
        )
        self.assertEqual(sorted(self.events), ["subj-0", "subj-1", "subj-2"])
        texts = self._texts()
        self.assertEqual(len(texts), 3)
        self.assertTrue(all(t.startswith("✓ 완료") for t in texts), texts)

    def test_a_failed_reread_fails_every_row_it_covered_and_records_no_merge(self):
        self.sync_result = {"ok": False, "error": "engine unreachable: timed out"}
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self._run_three()
        texts = self._texts()
        self.assertEqual(len(texts), 3)
        self.assertTrue(all(t.startswith("✕ 실패") and "timed out" in t for t in texts), texts)
        self.assertEqual(self.events, [])

    def test_a_batch_that_raises_fails_its_rows_and_the_queue_behind_it_still_rereads(self):
        calls = iter([ValueError("recount exploded"), self.sync_result])

        def sync():
            with self.count_lock:
                self.syncs += 1
            self.first_entered.set()
            self.gate.wait(10)
            result = next(calls)
            if isinstance(result, Exception):
                raise result
            return result

        buf = io.StringIO()
        with mock.patch.object(door, "_call_engine_sync", sync), contextlib.redirect_stderr(buf):
            self._merge(0)
            self.assertTrue(self.first_entered.wait(10))
            self._merge(1)
            worker = door._REREAD_WORKER
            self.gate.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(door._REREAD_WORKER)
        texts = {
            b["block_id"]: b["elements"][0]["text"]
            for b in self.web.blocks
            if str(b.get("block_id", "")).endswith(":status")
        }
        self.assertTrue(texts["card:0:status"].startswith("✕ 실패"), texts)
        self.assertTrue(texts["card:1:status"].startswith("✓ 완료"), texts)
        self.assertEqual(self.events, ["subj-1"])

    def test_settling_one_row_leaves_every_other_block_as_slack_holds_it(self):
        before = self.web.blocks
        outcome = door.card_types.Done(text="✓ 완료 — x")
        door._show_row(door.card_types.RowRef(channel="C1", card_ts="1.0", idx=1), outcome, "ko")
        changed = [i for i, (a, b) in enumerate(zip(before, self.web.blocks)) if a != b]
        self.assertEqual(len(before), len(self.web.blocks))
        self.assertEqual(len(changed), 1)
        self.assertEqual(self.web.blocks[changed[0]]["block_id"], "card:1:status")
        self.assertEqual(self.web.blocks[changed[0]]["elements"][0]["text"], "✓ 완료 — x")

    def test_a_merge_after_the_worker_drained_starts_a_fresh_one(self):
        self.gate.set()
        self._merge(0)
        first = door._REREAD_WORKER
        if first is not None:
            first.join(10)
        self._merge(1)
        second = door._REREAD_WORKER
        if second is not None:
            second.join(10)
        self.assertEqual(self.syncs, 2)
        self.assertIsNone(door._REREAD_WORKER)


if __name__ == "__main__":
    unittest.main()
