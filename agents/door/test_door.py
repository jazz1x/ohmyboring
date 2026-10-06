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
  (m) streams never starve short requests: with more GET /mcp streams open
      than the default thread pool has workers, /health (proxied) and a
      door-native route still answer within a second — red against a mutant
      that reads streams on the default pool again (the 2026-10-02 stall)
  (l) POST /mcp tools/call `remember` and POST /remember answer byte-for-byte
      (sha256 vs the stub body) and leave one remember_shadow event after the
      response — a raising shadow cannot change the answer (E3a-1)
  (n) E4-1 — the seven register reads (HTTP POST /decisions·/risks·/next_actions·
      /stalled·/recurrences·/context·/status and the MCP tools decisions·risks·
      next_actions·stalled·recurrences·context·project_status) answer byte-for-byte
      at the default reader (engine) and leave one read_shadow event per request —
      mismatch/race/error classification, raising shadow cannot change the answer;
      DOOR_REGISTER_READER=python answers from inside the door (AskResp /
      Structured envelope / validation messages) without touching the stub
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import shutil
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
from concurrent.futures import ThreadPoolExecutor
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

# 그림자 사건(remember_shadow·read_shadow)이 시험 밖으로 새어 나가지 못하게 — 싱크 기본값을
# spool 로 깔고 로그는 임시 파일로 보든다. 세션 사유: 이 두 값을 안 정한 채 띄운 문의 그림자가
# 기본(db) 싱크로 localhost:7710 의 진짜 문에 POST 해 운영 event_log 를 더럽힌 일이 있다.
# 각 시험 클래스의 setUp 이 이 값들을 정하면 그것이 우선(setdefault 라).
os.environ.setdefault("BORING_EVENT_SINK", "spool")
os.environ.setdefault(
    "BORING_EVENT_LOG", os.path.join(tempfile.gettempdir(), f"door-test-events-{os.getpid()}.ndjson")
)

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

#: 문이 tools/list 에 gap 을 덧붙인 뒤의 기대 — 엔진 도구는 앞에 그대로, gap 은 맨 끝.
TOOLS_LIST_WITH_GAP = json.loads(TOOLS_LIST_BODY)
TOOLS_LIST_WITH_GAP["result"]["tools"].append(door.gap_parse.TOOL)

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

#: 엔진 /events 응답의 실측 모양 — 고의로 비정형 바이트(이상한 공백·비아스키)로 만들어 문이
#: 다시 직렬화하면 갈라지게 한다. E4-α 가 프록시 표에 넣은 경로가 바이트 그대로 가는지 본다.
EVENTS_BODY = b'{"entries": [],  "maybe_truncated":false, "note":"\xec\x95\x88\xeb\x85\x95"}'

#: 엔진 레지스터 답의 실측 모양(E4-1) — 읽기 그림자 시험이 고르는 canned 답. remember 와
#: 같은 이유로 직렬화 비정형(공백·유니코드)을 넣어 문이 다시 직렬화하면 갈라지게 한다.
REGISTER_HTTP_BODY = json.dumps(
    {
        "answer": (
            "Showing 1 of 1 matching claims (limit_applied=false).\n"
            "* subj — pred: a value with enough characters (kind=decision, confidence=certain)"
        ),
        "sources": ["subj"],
    },
    ensure_ascii=False,
).encode()
REGISTER_MCP_PAYLOAD = {
    "answer": (
        "Showing 1 of 1 matching claims (limit_applied=false).\n"
        "* subj — pred: a value with enough characters (kind=risk, confidence=likely)"
    ),
    "sources": ["subj"],
    "items": [
        {
            "node_id": "claim:subj:pred",
            "subject": "subj",
            "predicate": "pred",
            "value": "a value with enough characters",
            "kind": "risk",
            "confidence": "likely",
            "valid_from": "2026-09-30T01:23:45.123456+00:00",
            "project": "omb",
        }
    ],
    "limit_applied": False,
    "total_matching": 1,
}
REGISTER_MCP_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {
            "content": [{"type": "text", "text": json.dumps(REGISTER_MCP_PAYLOAD, ensure_ascii=False)}],
            "structuredContent": REGISTER_MCP_PAYLOAD,
            "isError": False,
        },
    },
    ensure_ascii=False,
).encode()
RECURRENCES_BODY = json.dumps(
    {"rows": [], "days": 30, "max_distance": 0.2, "min_days_apart": 3}, ensure_ascii=False
).encode()
CONTEXT_BODY = json.dumps(
    {
        "decisions": [],
        "risks": [],
        "facts": [],
        "glossary": [],
        "next_actions": [],
        "language": "ko",
    },
    ensure_ascii=False,
).encode()
STATUS_BODY = json.dumps(
    {"answer": "No recent records or claims found for project 'omb'.", "sources": []}
).encode()


def _mcp_envelope(payload: dict) -> bytes:
    """canned MCP 도구 답 봉투 — structuredContent 와 content[].text 둘 다 싣는 엔진 모양."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "structuredContent": payload,
                "isError": False,
            },
        },
        ensure_ascii=False,
    ).encode()


#: register_mode 일 때 스텁이 고르는 canned 답의 룩업 — 경로·도구 이름 둘 다.
_REGISTER_STUB_PATHS = {
    "/decisions": REGISTER_HTTP_BODY,
    "/risks": REGISTER_HTTP_BODY,
    "/next_actions": REGISTER_HTTP_BODY,
    "/stalled": REGISTER_HTTP_BODY,
    "/recurrences": RECURRENCES_BODY,
    "/context": CONTEXT_BODY,
    "/status": STATUS_BODY,
}
_REGISTER_STUB_TOOLS = {
    "decisions": REGISTER_MCP_BODY,
    "risks": REGISTER_MCP_BODY,
    "next_actions": REGISTER_MCP_BODY,
    "stalled": REGISTER_MCP_BODY,
    "recurrences": _mcp_envelope({"rows": [], "days": 30, "max_distance": 0.2, "min_days_apart": 3}),
    "context": _mcp_envelope(json.loads(CONTEXT_BODY)),
    "project_status": _mcp_envelope(json.loads(STATUS_BODY)),
}


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
    register_mode = False

    def do_GET(self):
        type(self).last_path = self.path
        if self.path == "/health":
            self._reply(200, HEALTH_BODY)
        elif self.path.startswith("/events"):
            self._reply(200, EVENTS_BODY)
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
        elif type(self).register_mode and self.path in _REGISTER_STUB_PATHS:
            # E4-1 읽기 그림자 시험의 canned 엔진 답 — 기본(거짓)이면 아래 echo 그대로.
            self._reply(200, _REGISTER_STUB_PATHS[self.path], respond_as)
        elif (
            type(self).register_mode
            and isinstance(params, dict)
            and params.get("name") in _REGISTER_STUB_TOOLS
        ):
            self._reply(200, _REGISTER_STUB_TOOLS[params["name"]], respond_as)
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
        self.assertEqual(json.loads(body), TOOLS_LIST_WITH_GAP)
        self.assertEqual(content_type, STUB_CONTENT_TYPE)
        names = [t["name"] for t in json.loads(body)["result"]["tools"]]
        self.assertEqual(
            names, ["alpha", "beta", "gamma", "gap"], "엔진 도구는 순서 그대로 앞에, gap 은 끝에"
        )
        engine_tools = json.loads(TOOLS_LIST_BODY)["result"]["tools"]
        self.assertEqual(json.loads(body)["result"]["tools"][:3], engine_tools)

    def test_tools_list_non_2xx_and_non_list_answers_pass_through_untouched(self):
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}, "respond_as": "text/plain"}
        ).encode()
        status, body, content_type = _req(
            self.door_port, "POST", "/mcp", body=payload, headers={"content-type": "application/json"}
        )
        self.assertEqual((status, content_type), (200, "text/plain"))
        self.assertEqual(body, TOOLS_LIST_BODY, "JSON 아닌 답은 gap 을 안 덧붙이고 바이트 그대로")

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
        self.assertEqual(len(contract["http_routes"]), 30)

    def test_events_get_passes_through_byte_identical(self):
        # E4-α — /events entered the proxy table with the contract re-snapshot; the door
        # relays query and answer bytes untouched (this kills a " Events"→engine revert).
        status, body, content_type = _req(self.door_port, "GET", "/events?limit=2&component=stub")
        self.assertEqual(status, 200)
        self.assertEqual(StubHandler.last_path, "/events?limit=2&component=stub")
        self.assertEqual(body, EVENTS_BODY)
        self.assertEqual(content_type, STUB_CONTENT_TYPE)

    def test_events_post_body_passes_through_byte_identical(self):
        payload = b'{"component":"stub","event":"probe","otel":{"attributes":{}}}'
        status, body, content_type = _req(
            self.door_port, "POST", "/events", body=payload, headers={"content-type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(StubHandler.seen_bodies[-1], payload)
        self.assertEqual(StubHandler.last_path, "/events")
        self.assertEqual(body, payload)
        self.assertEqual(content_type, STUB_CONTENT_TYPE)

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


class _CapDefaultExecutor:
    """시험 장치 — 문 앱을 감싸 기본 스레드 풀을 작게 줄인다. 기본 풀을 쓰는 경로(프록시
    _fetch·문 자체 경로)와 스트림 읽기가 같은 풀을 쓰는 옛 구현으로 되돌리는 변이에서는
    열린 스트림이 줄인 풀을 메워 아래 클래스의 시험이 red 가 된다 — 호스트 CPU 수와 무관한
    「스트림 수 > 기본 풀」 재현."""

    def __init__(self, app, workers: int):
        self._app = app
        self._workers = workers
        self._capped = False

    async def __call__(self, scope, receive, send):
        if not self._capped:
            self._capped = True
            asyncio.get_running_loop().set_default_executor(
                ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="door-test-default")
            )
        await self._app(scope, receive, send)


class HangStubHandler(StubHandler):
    """GET /mcp 만 바꾼 그릇 — SSE 헤더와 첫 덩어리를 별도 없이 본 뒤 5초 잠든다.

    문 쪽 read1 이 두 번째 덩어리를 기다리며 잠든 동안 문 자체 경로가 얼마나
    빨리 답하는지 재는 시험의 그릇."""

    def do_GET(self):
        if self.path != "/mcp":
            return super().do_GET()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(SSE_FIRST)
            self.wfile.flush()
            time.sleep(5.0)
        except (BrokenPipeError, ConnectionResetError):
            pass


class StreamsNeverStarveShortRequestsTests(unittest.TestCase):
    """스트림이 기본 풀보다 많이 열린 채로도 짧은 요청이 1초 안에 단다.

    문 서버의 기본 풀을 고의로 4로 줄이고(GET /mcp 스트림 8개 — 어느 CPU 에서든
    「스트림 수 > 기본 풀」) 스트림을 연 채 /health(프록시·to_thread)와 /approved
    (문 자체 경로·to_thread)의 응답을 잰다. 스트림 읽기가 전용 풀에 있으면 기본
    풀은 비어 있어 둘 다 1초 안에 오고, _stream_chunks 를 「기본 풀에서 read1」하는
    옛 구현으로 되돌리는 변이에서는 잠든 읽기가 풀을 메워 /health 가 밀려 red."""

    STREAMS = 8
    POOL = 4

    @classmethod
    def setUpClass(cls):
        cls._saved_upstream = os.environ.get("DOOR_UPSTREAM")
        cls._saved_dsn = os.environ.get("DOOR_PG_DSN")
        cls.stub = ThreadingHTTPServer(("127.0.0.1", 0), HangStubHandler)
        cls.stub_thread = threading.Thread(target=cls.stub.serve_forever, daemon=True)
        cls.stub_thread.start()
        os.environ["DOOR_UPSTREAM"] = f"http://127.0.0.1:{cls.stub.server_address[1]}"
        # /approved 가 to_thread 까지 가게 하는 DSN — 닫힌 포트라 즉시 거절돼 502.
        os.environ["DOOR_PG_DSN"] = f"postgresql://127.0.0.1:{_free_port()}/boring"
        cls.door_port = _free_port()
        cls.door_server = uvicorn.Server(
            uvicorn.Config(
                _CapDefaultExecutor(door.app, cls.POOL),
                host="127.0.0.1",
                port=cls.door_port,
                log_level="warning",
            )
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
        if cls._saved_dsn is None:
            os.environ.pop("DOOR_PG_DSN", None)
        else:
            os.environ["DOOR_PG_DSN"] = cls._saved_dsn

    def _open_stream(self):
        sock = socket.create_connection(("127.0.0.1", self.door_port), timeout=10)
        sock.sendall(
            b"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\nConnection: close\r\n\r\n"
        )
        wire = b""
        while SSE_FIRST not in wire:
            chunk = sock.recv(4096)
            self.assertTrue(chunk, "stream ended before its first chunk")
            wire += chunk
        return sock

    def test_many_open_streams_short_requests_answer_within_a_second(self):
        self.assertGreater(self.STREAMS, self.POOL, "test must open more streams than the pool has workers")
        streams = [self._open_stream() for _ in range(self.STREAMS)]
        try:
            started = time.monotonic()
            status, body, _ = _req(self.door_port, "GET", "/health")
            health_s = time.monotonic() - started
            self.assertEqual(status, 200)
            self.assertEqual(body, HEALTH_BODY)
            self.assertLess(health_s, 1.0, f"/health took {health_s:.2f}s with {self.STREAMS} streams open")

            started = time.monotonic()
            status, body, _ = _req(self.door_port, "GET", "/approved")
            approved_s = time.monotonic() - started
            self.assertEqual(status, 502)
            self.assertIn(b"store unreachable", body)
            self.assertLess(
                approved_s,
                1.0,
                f"/approved took {approved_s:.2f}s with {self.STREAMS} streams open",
            )
        finally:
            for sock in streams:
                sock.close()


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
    """E3a-2 — 문을 지난 remember 는 엔진 답을 바이트 그대로 돌려주고, 응답 뒤에 그림자가
    파이썬 쓰기 경로의 결정(PII 게이트·중복 문 갈래, 걸러진 것은 결정만)을 엔진 응답의
    결정과 맞추고, 저장·대체에서는 실제 노트와 칸별 대조까지 해 사건(remember_shadow)
    한 줄을 남긴다. 사건에는 어긋남을 가를 사유(decision/fields/pii-…)가 실린다.

    Pinned here:
      - POST /mcp tools/call remember 의 응답이 엔진 답과 바이트 같다(sha256)
      - POST /remember 의 응답이 엔진 답과 바이트 같다(sha256)
      - 응답 뒤 그림자가 사건 한 줄을 남긴다 — status ok, source_path, omb_session_id,
        결정 대조(decision/engine_decision)까지
      - 볼트 rules/pii.yaml 이 있으면 문이 게이트를 불러 온다 — 가린 엔진 노트와 원문
        요청은 이제 ok(옛 pii_not_ported 어긋남이 풀리는 사례)
      - 그림자가 예외를 던져도 응답은 그대로 — 사건 status=error, 로그 한 줄
      - 응답은 그림자를 기다리지 않는다
      - 문 안의 노트 색인을 미리 채우면 그림자의 중복 문 스캔은 쓰기마다 목록·stat
        만 하고 노트를 새로 읽지 않는다 — 결정은 여전히 ok (E3b-2)
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
        # The shadow write runs after the answer; restoring BORING_EVENT_LOG before the door has
        # stopped sent late shadows into the real spool (6 sess-switch rows, 2026-10-02).
        cls.door_server.should_exit = True
        cls.door_thread.join(timeout=10)
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
        if vault.exists():
            shutil.rmtree(vault)
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
        # DSN 이 없는 시험 환경이라 임베딩 갈래는 못 보고 「저장(미확인)」이지만,
        # 결정 대조 자체는 engine stored 와 맞아 ok 다.
        self.assertTrue(event["decision"].startswith("stored"), event["decision"])
        self.assertEqual(event["engine_decision"], "stored:/vault/wiki/wiki-2901.md")

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

    def test_shadow_scans_through_the_prefilled_note_index(self):
        """E3b-2 — 문 안의 노트 색인을 미리 채워 두면 그림자의 중복 문 스캔은 쓰기마다
        디렉터리 목록·stat 만 보고 노트를 새로 읽지 않는다(읽기는 채우기 때 딱 한 바퀴).
        결정은 디스크 훑기와 같아 사건은 ok — 색인이 판정을 바꾸거나 읽기를 새로 하는
        변이가 이 단언에서 빨갛게 끝난다."""
        vault = Path(os.environ["BORING_VAULT_DIR"])
        reads: list[str] = []

        def counting_read(vault_dir: str, note_id: str) -> str | None:
            reads.append(note_id)
            return door.vault_notes.read_note(vault_dir, note_id)

        index = door.remember_index.NoteIndex(
            door.remember_index.DiskSeams(
                vault_dir=str(vault),
                list_notes=lambda: door._list_wiki_notes(str(vault)),
                read_note=counting_read,
                split_frontmatter=door.vault_note.split_frontmatter,
            )
        )
        index.prefill()
        self.assertTrue(index.usable, "픽스처 볼트는 채울 수 있다")
        reads.clear()
        previous = getattr(door.app.state, "note_index", None)
        door.app.state.note_index = index
        try:
            _req(
                self.door_port,
                "POST",
                "/mcp",
                body=self._mcp_payload(self._MCP_ARGS),
                headers={"content-type": "application/json"},
            )
            event = self._wait_for_shadow()
        finally:
            door.app.state.note_index = previous
        self.assertEqual(event["status"], "ok", event.get("reason"))
        self.assertTrue(event["decision"].startswith("stored"), event["decision"])
        self.assertEqual(
            reads,
            [],
            f"색인이 채워져 있으니 쓰기의 재고는 목록·stat 만 — 노트를 새로 읽으면 안 된다: {reads}",
        )

    _PII_YAML = """---
version: "1.0"
rules:
  - name: ipv4_any
    regex: '\\b(?:(?:25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)\\.){3}(?:25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)\\b'
    action: redact
    replacement: "[IP]"
    severity: warning
    reason: IPv4
"""

    def test_vault_pii_rules_are_loaded_by_the_shadow(self):
        # 볼트에 rules/pii.yaml 이 있으면 문이 그림자에 게이트를 싣는다. 엔진 노트의
        # 본문만 가려져 있고(엔진이 게이트를 지난 흔적) 요청에는 원문이 있어도, 같은
        # 규칙으로 가린 파이썬 렌더와 칸이 맞아 사건은 ok 다 — E3a-1 의 pii_not_ported
        # 어긋남이 이 wiring 에서 풀리는 사례다.
        vault = Path(os.environ["BORING_VAULT_DIR"])
        rules = vault / "rules"
        rules.mkdir(exist_ok=True)
        (rules / "pii.yaml").write_text(self._PII_YAML, encoding="utf-8")
        # 엔진이 실제로 쓴 것은 게이트를 지난 본문 — IP 가 가려진 채로 디스크에 있다.
        note = self._WIKI_2901.replace(
            "문을 지난 remember 는 엔진 답을 바이트 그대로 돌려준다.",
            "문은 [IP] 에 묶인다.",
        )
        (vault / "wiki" / "wiki-2901.md").write_text(note, encoding="utf-8")
        args = dict(self._MCP_ARGS)
        args["body"] = "문은 10.1.2.3 에 묶인다."
        _req(
            self.door_port,
            "POST",
            "/mcp",
            body=self._mcp_payload(args),
            headers={"content-type": "application/json"},
        )
        event = self._wait_for_shadow()
        self.assertEqual(event["status"], "ok", event.get("reason"))
        self.assertTrue(event["decision"].startswith("stored"), event["decision"])

    def test_shadow_event_reason_sorts_a_mismatch(self):
        # 걸러둘 결정이 갈리면 사건 사유는 decision 으로 시작한다 — (가)/(나)/(다) 판별
        # 재료가 사건 한 줄에 있는지를 문 wiring 에서도 본다. 엔진은 wiki-2901 로
        # 저장했는데(답이 그러니) 같은 세션 노트 wiki-2999 가 볼트에 있으면 파이썬은
        # 걸러둘 것이다 — 경로가 갈려 어긋남.
        vault = Path(os.environ["BORING_VAULT_DIR"])
        ghost = self._WIKI_2901.replace("wiki-2901", "wiki-2999")
        (vault / "wiki" / "wiki-2999.md").write_text(ghost, encoding="utf-8")
        _req(
            self.door_port,
            "POST",
            "/mcp",
            body=self._mcp_payload(self._MCP_ARGS),
            headers={"content-type": "application/json"},
        )
        event = self._wait_for_shadow()
        self.assertEqual(event["status"], "mismatch")
        self.assertTrue(event["reason"].startswith("decision "), event.get("reason"))
        self.assertIn("engine=stored", event["reason"])
        self.assertIn("python=skipped", event["reason"])

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

        # run_shadow 는 ShadowRequest 하나를 위치 인자로 받는다 — 이 목도 그 모양을 따라야
        # 진짜로 0.6s 자고, 응답 앞에서 그림자를 기다리는 변이가 이 단언으로 빨갛게 끝난다.
        def slow_shadow(_request, **_kwargs):
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
            event = self._wait_for_shadow()
        self.assertEqual(status, 200)
        self.assertEqual(body, REMEMBER_MCP_BODY)
        self.assertEqual(event["status"], "ok", "자고 난 그림자의 사건은 ok — 목 깨짐을 덮지 않는다")
        self.assertLess(elapsed, 0.6, "응답이 그림자를 기다리면 이 값이 0.6s 를 넘는다")

    def test_non_remember_calls_leave_no_shadow_event(self):
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
        status, body, _ = _req(
            self.door_port, "POST", "/mcp", body=payload, headers={"content-type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), TOOLS_LIST_WITH_GAP)
        time.sleep(0.3)
        self.assertEqual(self._shadow_events(), [], "회상·다른 도구엔 그림자가 따라붙지 않는다")


class RememberWriterSwitchTests(unittest.TestCase):
    """E3c-1 — remember 쓰기 주인 스위치 (DOOR_REMEMBER_WRITER).

    기본(engine)이면 remember 는 프록시 그대로(엔진 답 바이트, 그림자까지 — RememberShadowTests).
    python 이면 문이 엔진에 넘기지 않고 문 안의 쓰기 글(remember_writer.run_write)이 결정·쓰고
    엔진 답 모양(JSON-RPC 봉투 / RememberResp 다섯 칸)으로 답한다. run_write 는 여기서 목으로
    두고 선로(스위치·봉투·상태 부호)만 본다 — 진짜 쓰기 길은 remember/test_writer.py 와
    scripts/e3c1-verify.py(일회용 스키마+엔진 동기 0 변경)가 본다.
    """

    _ARGS = {"title": "스위치 시험", "body": "본문", "omb_session_id": "sess-switch"}

    @classmethod
    def setUpClass(cls):
        cls._saved = {
            name: os.environ.get(name)
            for name in (
                "DOOR_UPSTREAM",
                "DOOR_PG_DSN",
                "DOOR_REMEMBER_WRITER",
                "BORING_VAULT_DIR",
                "BORING_EVENT_SINK",
                "BORING_EVENT_LOG",
                "BORING_OWNER_TOKEN",
            )
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
        # The shadow write runs after the answer; restoring BORING_EVENT_LOG before the door has
        # stopped sent late shadows into the real spool (6 sess-switch rows, 2026-10-02).
        cls.door_server.should_exit = True
        cls.door_thread.join(timeout=10)
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
        if vault.exists():
            shutil.rmtree(vault)
        (vault / "wiki").mkdir(parents=True, exist_ok=True)
        os.environ["BORING_VAULT_DIR"] = str(vault)
        os.environ["BORING_EVENT_SINK"] = "spool"
        os.environ["BORING_EVENT_LOG"] = str(Path(self._tmp.name) / f"events-{time.time_ns()}.ndjson")
        os.environ["DOOR_PG_DSN"] = "postgresql://unused/unused"  # run_write 는 목으로 둔다
        StubHandler.seen_bodies.clear()

    def tearDown(self):
        os.environ.pop("DOOR_REMEMBER_WRITER", None)

    def _mcp_payload(self) -> bytes:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "remember", "arguments": self._ARGS},
            }
        ).encode()

    def _written(self):
        return door.remember_writer.Written(
            wiki_id="wiki-0001",
            source_path="/vault/wiki/wiki-0001.md",
            message="remembered → wiki/wiki-0001.md · chunks 1 · graph(tools 0 concepts 0 claims 0) — recallable now",
            duplicate=None,
            supersedes=0,
            unknown=0,
        )

    def test_default_writer_is_engine_and_remember_proxies(self):
        os.environ.pop("DOOR_REMEMBER_WRITER", None)
        self.assertEqual(door._remember_writer(), "engine")
        status, body, _ = _req(
            self.door_port,
            "POST",
            "/remember",
            body=json.dumps(self._ARGS).encode(),
            headers={"content-type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, REMEMBER_HTTP_BODY, "기본은 엔진 답 바이트 그대로")
        self.assertEqual(len(StubHandler.seen_bodies), 1, "기본은 엔진에 본문을 넘긴다")

    def test_unknown_switch_value_falls_back_to_engine(self):
        os.environ["DOOR_REMEMBER_WRITER"] = "python2"
        self.assertEqual(door._remember_writer(), "engine")
        status, body, _ = _req(
            self.door_port,
            "POST",
            "/remember",
            body=json.dumps(self._ARGS).encode(),
            headers={"content-type": "application/json"},
        )
        self.assertEqual(body, REMEMBER_HTTP_BODY)

    def test_python_writer_mcp_answers_without_upstream(self):
        os.environ["DOOR_REMEMBER_WRITER"] = "python"
        with mock.patch.object(door.remember_writer, "run_write", return_value=self._written()) as run:
            status, body, content_type = _req(
                self.door_port,
                "POST",
                "/mcp",
                body=self._mcp_payload(),
                headers={"content-type": "application/json"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(StubHandler.seen_bodies, [], "켜면 엔진에 remember 가 안 간다")
        self.assertEqual(content_type, "application/json")
        answer = json.loads(body)
        self.assertEqual(answer["jsonrpc"], "2.0")
        self.assertEqual(answer["id"], 7, "요청 id 를 그대로 돌려준다")
        self.assertEqual(
            answer["result"],
            {
                "content": [
                    {
                        "type": "text",
                        "text": "remembered → wiki/wiki-0001.md · chunks 1 · "
                        "graph(tools 0 concepts 0 claims 0) — recallable now",
                    }
                ],
                "isError": False,
            },
        )
        request, deps = run.call_args.args
        self.assertEqual(request.route, "mcp")
        self.assertEqual(request.arguments["title"], "스위치 시험")
        self.assertEqual(request.omb_session_id, "sess-switch")
        self.assertFalse(deps.is_owner)

    def test_python_writer_http_answers_remember_resp_shape(self):
        os.environ["DOOR_REMEMBER_WRITER"] = "python"
        with mock.patch.object(door.remember_writer, "run_write", return_value=self._written()):
            status, body, _ = _req(
                self.door_port,
                "POST",
                "/remember",
                body=json.dumps(self._ARGS).encode(),
                headers={"content-type": "application/json"},
            )
        self.assertEqual(StubHandler.seen_bodies, [])
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                "source_path": "/vault/wiki/wiki-0001.md",
                "wiki_id": "wiki-0001",
                "duplicate": None,
                "supersedes": 0,
                "unknown": 0,
            },
        )

    def test_python_writer_skipped_duplicate_answer(self):
        os.environ["DOOR_REMEMBER_WRITER"] = "python"
        skipped = door.remember_writer.Written(
            wiki_id="wiki-0099",
            source_path="/vault/wiki/wiki-0099.md",
            message="skipped — duplicate of /vault/wiki/wiki-0099.md",
            duplicate="/vault/wiki/wiki-0099.md",
            supersedes=0,
            unknown=0,
        )
        with mock.patch.object(door.remember_writer, "run_write", return_value=skipped):
            status, body, _ = _req(
                self.door_port,
                "POST",
                "/remember",
                body=json.dumps(self._ARGS).encode(),
                headers={"content-type": "application/json"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                "source_path": "/vault/wiki/wiki-0099.md",
                "wiki_id": "wiki-0099",
                "duplicate": "/vault/wiki/wiki-0099.md",
                "supersedes": 0,
                "unknown": 0,
            },
        )

    def test_python_writer_refused_maps_engine_error_shapes(self):
        os.environ["DOOR_REMEMBER_WRITER"] = "python"
        refused = door.remember_writer.Refused(-32602, "missing argument: title")
        with mock.patch.object(door.remember_writer, "run_write", return_value=refused):
            status, body, _ = _req(
                self.door_port,
                "POST",
                "/remember",
                body=json.dumps(self._ARGS).encode(),
                headers={"content-type": "application/json"},
            )
        self.assertEqual(status, 400, "-32602 는 400 — serve/http.rs:690-696")
        self.assertEqual(json.loads(body), {"error": "missing argument: title"})
        with mock.patch.object(
            door.remember_writer,
            "run_write",
            return_value=door.remember_writer.Refused(-32603, "PII gate blocked by rule 'x'"),
        ):
            mcp_status, mcp_body, _ = _req(
                self.door_port,
                "POST",
                "/mcp",
                body=self._mcp_payload(),
                headers={"content-type": "application/json"},
            )
        self.assertEqual(mcp_status, 200, "MCP 거절도 JSON-RPC 봉투로 200 — handle_mcp")
        answer = json.loads(mcp_body)
        self.assertEqual(answer["id"], 7)
        self.assertEqual(answer["error"], {"code": -32603, "message": "PII gate blocked by rule 'x'"})
        self.assertNotIn("result", answer)

    def test_python_writer_raising_is_engine_error_never_silent_200(self):
        os.environ["DOOR_REMEMBER_WRITER"] = "python"
        with mock.patch.object(door.remember_writer, "run_write", side_effect=RuntimeError("boom")):
            status, body, _ = _req(
                self.door_port,
                "POST",
                "/remember",
                body=json.dumps(self._ARGS).encode(),
                headers={"content-type": "application/json"},
            )
        self.assertEqual(status, 500)
        self.assertEqual(list(json.loads(body)), ["error"])

    def test_python_writer_without_dsn_is_503(self):
        os.environ["DOOR_REMEMBER_WRITER"] = "python"
        os.environ.pop("DOOR_PG_DSN", None)
        status, body, _ = _req(
            self.door_port,
            "POST",
            "/remember",
            body=json.dumps(self._ARGS).encode(),
            headers={"content-type": "application/json"},
        )
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"error": "store not configured"})


class WriterEventDispatchTests(unittest.TestCase):
    """E3c-2 — 파이썬 쓰기 길의 사건 전달(dedup_decision·remember_written)이 응답을 기다리지
    않는다. 느린 엔진 /events 싱크 앞에서도 인큐만 하고 돌아오고, 받은 순서대로 전달된다."""

    def test_enqueue_returns_before_slow_sink_and_delivers_in_order(self):
        delivered = []
        done = threading.Event()

        def slow_sink(component, event, status, **fields):
            time.sleep(0.05)
            delivered.append((component, event, status, fields))
            if len(delivered) == 2:
                done.set()
            return True

        with mock.patch.object(door.event_log, "try_append_event", side_effect=slow_sink):
            started = time.monotonic()
            self.assertTrue(door._enqueue_writer_event("door", "dedup_decision", "ok", a=1))
            self.assertTrue(door._enqueue_writer_event("door", "remember_written", "ok", b=2))
            queued = time.monotonic() - started
            self.assertTrue(done.wait(5), "백그라운드가 두 사건을 다 전달")
        self.assertLess(queued, 0.09, "두 사건의 인큐가 느린 싱크(50ms×2)를 기다리지 않는다")
        self.assertEqual(
            [(entry[0], entry[1]) for entry in delivered],
            [("door", "dedup_decision"), ("door", "remember_written")],
            "자료 순서 그대로 전달",
        )


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

    def test_samples_read_the_given_spellings(self):
        os.environ["DOOR_PG_DSN"] = "postgresql://boring:boring@127.0.0.1:5432/boring"
        asked: list[list[str]] = []
        orig = door._fetch_split_samples
        door._fetch_split_samples = lambda variants: asked.append(variants) or [("action", 268, "design x")]
        try:
            status, body, _ = _req(
                self.door_port, "GET", "/repairs/split-subjects/samples?variant=next%20step&variant=next-step"
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(body), {"samples": [{"predicate": "action", "rows": 268, "value": "design x"}]}
            )
            self.assertEqual(asked, [["next step", "next-step"]])
            for query in ("", "?variant=", "?" + "&".join(f"variant=v{i}" for i in range(11))):
                status, _, _ = _req(self.door_port, "GET", f"/repairs/split-subjects/samples{query}")
                self.assertEqual(status, 400, query)
        finally:
            door._fetch_split_samples = orig


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

    def test_the_repair_judge_route_runs_the_judge_program(self):
        """POST /run/repair-judge — morning-card 와 같은 {ok, exit, tail} 모양으로 판정
        실행을 돌린다. The mutation this kills: the route wired to the wrong program (or
        folded into the card's own run) — the judge must run as its own subprocess."""
        calls = []

        def fake_run(script, timeout_s):
            calls.append(script)
            return subprocess.CompletedProcess(
                ["x"], 0, stdout="[repair-judge] judged=2 failed=0\n", stderr=""
            )

        with mock.patch.object(door, "_run_card_subprocess", fake_run):
            status, body, _ = _req(self.door_port, "POST", "/run/repair-judge")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["exit"], 0)
        self.assertIn("judged=2", payload["tail"])
        self.assertTrue(
            str(calls[0]).endswith(os.path.join("agents", "slack", "card_repair_judge.py")),
            "the repair-judge route must execute the judge, not the morning card",
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


class _RegisterTestHarness:
    """E4-1 시험의 공통 바탕 — 스텁 엔진+문(uvicorn 스레드) 한 쌍과 환경 저장·복원.

    RememberWriterSwitchTests 와 같은 모양이라 두 시험 다움에 붙인다(다중 상속은 안 쓴다 —
    unittest setUpClass 훅을 그대로 따라가게 하나의 부모로)."""

    _ENV_NAMES = (
        "DOOR_UPSTREAM",
        "DOOR_PG_DSN",
        "DOOR_REGISTER_READER",
        "DOOR_RECALL_READER",
        "BORING_EVENT_SINK",
        "BORING_EVENT_LOG",
    )

    @classmethod
    def _start(cls):
        cls._saved = {name: os.environ.get(name) for name in cls._ENV_NAMES}
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
    def _stop(cls):
        cls.door_server.should_exit = True
        cls.stub.shutdown()
        cls.stub.server_close()
        cls._tmp.cleanup()
        for name, value in cls._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _setUp(self, register_mode: bool = False):
        StubHandler.seen_bodies.clear()
        StubHandler.hits = 0
        StubHandler.register_mode = register_mode
        os.environ["BORING_EVENT_SINK"] = "spool"
        os.environ["BORING_EVENT_LOG"] = str(Path(self._tmp.name) / f"events-{time.time_ns()}.ndjson")
        os.environ["DOOR_PG_DSN"] = "postgresql://unused/unused"  # 읽기 길은 목으로 둔다
        # 그림자는 응답 뒤(백그라운드)에 돌아 — mock 은 _req 블록이 끝나기 전에 풀리면 안 된다.
        # 테스트마다 덮어쓰는 self._shadow_query_impl 을 경유하게 핀다(지금 값이 아니라 호출 시점을 본다).
        self._shadow_query_impl = lambda surface, transport, arguments: door.Ok({})
        self._shadow_query_patch = mock.patch.object(
            door,
            "_register_shadow_query",
            side_effect=lambda surface, transport, arguments: self._shadow_query_impl(
                surface, transport, arguments
            ),
        )
        self._shadow_query_patch.start()

    def _tearDown(self):
        self._shadow_query_patch.stop()
        StubHandler.register_mode = False
        os.environ.pop("DOOR_REGISTER_READER", None)
        os.environ.pop("DOOR_RECALL_READER", None)

    def _read_shadow_events(self) -> list[dict]:
        path = Path(os.environ["BORING_EVENT_LOG"])
        if not path.exists():
            return []
        return [
            parsed
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and (parsed := json.loads(line)).get("event") == "read_shadow"
        ]

    def _wait_for_shadow(self, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self._read_shadow_events()
            if events:
                return events[-1]
            time.sleep(0.05)
        self.fail("read_shadow event not written within timeout")


class RegisterShadowTests(_RegisterTestHarness, unittest.TestCase):
    """E4-1 읽기 그림자 — 스위치 기본(engine)에서 일곱 레지스터 경로는 엔진 답 바이트
    그대로 가고, 응답 뒤 그림자가 같은 요청을 파이썬 읽기 길에 태워 read_shadow 사건
    한 줄을 남긴다. 사건의 사유는 (가)/(나)/(다) 갈래로 남는다."""

    @classmethod
    def setUpClass(cls):
        cls._start()

    @classmethod
    def tearDownClass(cls):
        cls._stop()

    def setUp(self):
        self._setUp(register_mode=True)

    def tearDown(self):
        self._tearDown()

    def _mcp_call(self, tool: str, arguments: dict, request_id: int = 5) -> bytes:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        ).encode()

    def test_http_register_answer_is_byte_identical_and_shadow_ok(self):
        engine_payload = json.loads(REGISTER_HTTP_BODY)
        self._shadow_query_impl = lambda surface, transport, arguments: door.Ok(engine_payload)
        status, body, _ = _req(
            self.door_port,
            "POST",
            "/decisions",
            body=json.dumps({"project": "omb"}).encode(),
            headers={"content-type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, REGISTER_HTTP_BODY, "기본은 엔진 답 바이트 그대로")
        event = self._wait_for_shadow()
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["event"], "read_shadow")
        self.assertEqual(event["surface"], "decisions")
        self.assertEqual(event["transport"], "http")

    def test_mcp_register_tool_shadow_uses_structured_content(self):
        self._shadow_query_impl = lambda surface, transport, arguments: door.Ok(REGISTER_MCP_PAYLOAD)
        status, body, _ = _req(
            self.door_port,
            "POST",
            "/mcp",
            body=self._mcp_call("risks", {"project": "omb", "limit": 5}),
            headers={"content-type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, REGISTER_MCP_BODY, "MCP 도구 답도 바이트 그대로")
        event = self._wait_for_shadow()
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["surface"], "risks")
        self.assertEqual(event["transport"], "mcp")
        self.assertEqual(event["engine_rows"], 1)
        self.assertEqual(event["python_rows"], 1)

    def test_shadow_mismatch_is_classified_python_defect(self):
        self._shadow_query_impl = lambda surface, transport, arguments: door.Ok(
            {"answer": "No decisions recorded yet.", "sources": []}
        )
        _req(
            self.door_port,
            "POST",
            "/decisions",
            body=json.dumps({"project": "omb"}).encode(),
            headers={"content-type": "application/json"},
        )
        event = self._wait_for_shadow()
        self.assertEqual(event["status"], "mismatch")
        self.assertGreaterEqual(event["python_defect"], 1)
        self.assertIn("(가)", event["reason"])

    def test_shadow_race_rerun_match_stays_ok_with_unknown_reason(self):
        first = {"answer": "No decisions recorded yet.", "sources": []}
        calls = {"n": 0}

        def racy(surface, transport, arguments):
            calls["n"] += 1
            return door.Ok(first if calls["n"] == 1 else json.loads(REGISTER_HTTP_BODY))

        self._shadow_query_impl = racy
        _req(
            self.door_port,
            "POST",
            "/decisions",
            body=json.dumps({"project": "omb"}).encode(),
            headers={"content-type": "application/json"},
        )
        event = self._wait_for_shadow()
        self.assertEqual(event["status"], "ok")
        self.assertGreaterEqual(event["unknown"], 1, "어긋난 칸마다 레이스 사유가 남는다")
        self.assertIn("race (다)", event["reason"])

    def test_shadow_raising_cannot_change_the_answer(self):
        def boom(surface, transport, arguments):
            raise RuntimeError("boom")

        self._shadow_query_impl = boom
        status, body, _ = _req(
            self.door_port,
            "POST",
            "/next_actions",
            body=json.dumps({"project": ""}).encode(),
            headers={"content-type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, REGISTER_HTTP_BODY)
        event = self._wait_for_shadow()
        self.assertEqual(event["status"], "error")
        self.assertIn("RuntimeError", event["reason"])

    def test_status_shadow_compares_sources_only_when_generated(self):
        engine_payload = {
            "answer": "## Status\n\n- Done: one thing\n- Next: another",
            "sources": ["wiki/wiki-0001.md"],
        }
        original = _REGISTER_STUB_PATHS["/status"]
        _REGISTER_STUB_PATHS["/status"] = json.dumps(engine_payload, ensure_ascii=False).encode()
        try:
            self._shadow_query_impl = lambda surface, transport, arguments: door.Ok(engine_payload)
            status, body, _ = _req(
                self.door_port,
                "POST",
                "/status",
                body=json.dumps({"project": "omb"}).encode(),
                headers={"content-type": "application/json"},
            )
        finally:
            _REGISTER_STUB_PATHS["/status"] = original
        self.assertEqual(json.loads(body), engine_payload)
        event = self._wait_for_shadow()
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["surface"], "status")
        self.assertFalse(event["answer_compared"], "생성 답은 대조 안 함 — sources 만 맞춘다")


class RegisterReaderSwitchTests(_RegisterTestHarness, unittest.TestCase):
    """E4-1 읽기 주인 스위치 (DOOR_REGISTER_READER).

    기본(engine)이면 일곱 읽기는 프록시 그대로(바이트+그림자). python 이면 문이 엔진에
    넘기지 않고 문 안의 읽기 길로 답한다 — _register_answer 는 목으로 두고 선로(스위치·
    봉투·상태 부호·거절 문구)만 본다. 진짜 읽기 길은 registers/test_pg.py 가 본다."""

    @classmethod
    def setUpClass(cls):
        cls._start()

    @classmethod
    def tearDownClass(cls):
        cls._stop()

    def setUp(self):
        self._setUp()

    def tearDown(self):
        self._tearDown()

    def test_default_reader_is_engine_and_register_proxies(self):
        os.environ.pop("DOOR_REGISTER_READER", None)
        self.assertEqual(door._register_reader(), "engine")
        payload = json.dumps({"project": "omb"}).encode()
        status, body, _ = _req(
            self.door_port, "POST", "/decisions", body=payload, headers={"content-type": "application/json"}
        )
        self.assertEqual((status, body), (200, payload), "기본은 스텁 엔진(echo) 답 바이트 그대로")

    def test_unknown_switch_value_falls_back_to_engine(self):
        os.environ["DOOR_REGISTER_READER"] = "python2"
        self.assertEqual(door._register_reader(), "engine")

    def test_python_reader_http_answers_askresp_shape_without_upstream(self):
        os.environ["DOOR_REGISTER_READER"] = "python"
        hits_before = StubHandler.hits
        with mock.patch.object(
            door, "_register_answer", return_value=door.Ok({"answer": "digest", "sources": ["subj"]})
        ) as run:
            status, body, _ = _req(
                self.door_port,
                "POST",
                "/decisions",
                body=json.dumps({"project": "omb"}).encode(),
                headers={"content-type": "application/json"},
            )
        run.assert_called_once()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"answer": "digest", "sources": ["subj"]})
        self.assertEqual(StubHandler.hits, hits_before, "python 이면 엔진을 건드리지 않는다")

    def test_python_reader_mcp_structured_envelope_without_upstream(self):
        os.environ["DOOR_REGISTER_READER"] = "python"
        hits_before = StubHandler.hits
        payload = {
            "answer": "Showing 0 of 0 matching claims (limit_applied=false).",
            "sources": [],
            "items": [],
            "limit_applied": False,
            "total_matching": 0,
        }
        with mock.patch.object(door, "_register_answer", return_value=door.Ok(payload)):
            status, body, _ = _req(
                self.door_port,
                "POST",
                "/mcp",
                body=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {"name": "decisions", "arguments": {"limit": 3}},
                    }
                ).encode(),
                headers={"content-type": "application/json"},
            )
        self.assertEqual(StubHandler.hits, hits_before)
        self.assertEqual(status, 200)
        wire = json.loads(body)
        self.assertEqual(wire["id"], 7)
        self.assertEqual(wire["result"]["structuredContent"], payload)
        self.assertEqual(json.loads(wire["result"]["content"][0]["text"]), payload)
        self.assertFalse(wire["result"]["isError"])

    def test_python_reader_validation_rejection_matches_engine_message(self):
        os.environ["DOOR_REGISTER_READER"] = "python"
        hits_before = StubHandler.hits
        with mock.patch.object(door, "_register_answer") as run:
            status, body, _ = _req(
                self.door_port,
                "POST",
                "/decisions",
                body=json.dumps({"limit": 0}).encode(),
                headers={"content-type": "application/json"},
            )
        run.assert_not_called()
        self.assertEqual(StubHandler.hits, hits_before)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "limit must be an integer in 1..=50"})

    def test_python_reader_project_status_requires_project(self):
        os.environ["DOOR_REGISTER_READER"] = "python"
        status, body, _ = _req(
            self.door_port,
            "POST",
            "/mcp",
            body=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {"name": "project_status", "arguments": {}},
                }
            ).encode(),
            headers={"content-type": "application/json"},
        )
        self.assertEqual(status, 200)
        wire = json.loads(body)
        self.assertEqual(wire["error"]["code"], -32602)
        self.assertEqual(wire["error"]["message"], "missing argument: project")

    def test_python_reader_without_dsn_is_503(self):
        os.environ["DOOR_REGISTER_READER"] = "python"
        os.environ.pop("DOOR_PG_DSN", None)
        status, body, _ = _req(
            self.door_port,
            "POST",
            "/decisions",
            body=json.dumps({"project": "omb"}).encode(),
            headers={"content-type": "application/json"},
        )
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"error": "store not configured"})


class _RecallTestBase(_RegisterTestHarness):
    """회상 시험의 공통 바탕 — 엔진 스텁+문 한 쌍, 파이썬 길(_recall_python) 목, 사건 읽기."""

    ENGINE_TEXT = "- [wiki-2229.md] ..."

    @classmethod
    def setUpClass(cls):
        cls._start()

    @classmethod
    def tearDownClass(cls):
        cls._stop()

    def setUp(self):
        self._setUp()
        self._python_impl = lambda arguments: door.Ok(door.recall_answer.Recalled(self.ENGINE_TEXT, "wiki"))
        self.python_calls: list[dict] = []

        def python(arguments):
            self.python_calls.append(arguments)
            return self._python_impl(arguments)

        self._python_patch = mock.patch.object(door, "_recall_python", side_effect=python)
        self._python_patch.start()

    def tearDown(self):
        self._python_patch.stop()
        self._tearDown()

    def _recall(self, query: str, **extra) -> tuple[int, bytes, str | None]:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "recall", "arguments": {"query": query, **extra}},
            }
        ).encode()
        return _req(
            self.door_port, "POST", "/mcp", body=payload, headers={"content-type": "application/json"}
        )

    def _events(self) -> list[dict]:
        path = Path(os.environ["BORING_EVENT_LOG"])
        if not path.exists():
            return []
        rows = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        return [row for row in rows if row.get("event") == "recall_shadow"]

    def _wait_for_event(self, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if events := self._events():
                return events[-1]
            time.sleep(0.05)
        self.fail("recall_shadow event not written within timeout")


class RecallShadowTests(_RecallTestBase, unittest.TestCase):
    """E4-2 회상 그림자 — MCP recall 은 엔진 답 바이트(번호 보강만 예외) 그대로 가고, 응답 뒤
    파이썬 회상이 같은 인자로 돌아 recall_shadow 사건 한 줄을 남긴다. 파이썬 길(_recall_python)은
    목으로 두고 선로(대조 시점·응답 비대기·예외 접기)만 본다. 진짜 길은 recall/test_recall.py."""

    def test_same_text_is_ok_and_the_answer_is_the_engines_bytes(self):
        status, body, _ = self._recall("그냥 검색해줘", project="omb")
        self.assertEqual((status, body), (200, RECALL_BODY))
        event = self._wait_for_event()
        self.assertEqual((event["status"], event["path"]), ("ok", "wiki"))
        self.assertEqual(event["engine_lines"], 1)
        self.assertEqual(
            self.python_calls, [{"query": "그냥 검색해줘", "project": "omb"}], "인자는 요청 그대로"
        )

    def test_a_different_text_is_a_mismatch_with_the_first_differing_line(self):
        self._python_impl = lambda arguments: door.Ok(
            door.recall_answer.Recalled("- [other.md] 다름", "vector")
        )
        status, body, _ = self._recall("그냥 검색해줘")
        self.assertEqual((status, body), (200, RECALL_BODY), "어긋나도 응답은 엔진 그대로")
        event = self._wait_for_event()
        self.assertEqual((event["status"], event["path"]), ("mismatch", "vector"))
        self.assertEqual(
            event["first_diff"], {"line": 1, "engine": self.ENGINE_TEXT, "python": "- [other.md] 다름"}
        )
        self.assertNotIn("그냥 검색해줘", json.dumps(event, ensure_ascii=False), "사건에 질의를 싣지 않는다")

    def test_a_raising_shadow_costs_one_log_line_and_an_error_event_and_never_the_answer(self):
        def boom(arguments):
            raise RuntimeError("boom")

        self._python_impl = boom
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            status, body, _ = self._recall("그냥 검색해줘")
            event = self._wait_for_event()
        self.assertEqual((status, body), (200, RECALL_BODY))
        self.assertEqual(event["status"], "error")
        self.assertIn("RuntimeError", event["reason"])
        lines = [ln for ln in buf.getvalue().splitlines() if "recall_shadow" in ln]
        self.assertEqual(len(lines), 1, buf.getvalue())

    def test_the_answer_does_not_wait_for_the_shadow(self):
        def slow(arguments):
            time.sleep(0.6)
            return door.Ok(door.recall_answer.Recalled(self.ENGINE_TEXT, "wiki"))

        self._python_impl = slow
        started = time.monotonic()
        status, body, _ = self._recall("그냥 검색해줘")
        elapsed = time.monotonic() - started
        event = self._wait_for_event()
        self.assertEqual((status, body), (200, RECALL_BODY))
        self.assertEqual(event["status"], "ok", "자고 난 그림자의 사건은 ok — 목 깨짐을 덮지 않는다")
        self.assertLess(elapsed, 0.6, "응답이 그림자를 기다리면 이 값이 0.6s 를 넘는다")

    def test_the_shadow_compares_the_engine_text_from_before_the_note_blocks_were_prepended(self):
        with tempfile.TemporaryDirectory() as vault, mock.patch.dict(os.environ, {"BORING_VAULT_DIR": vault}):
            status, body, _ = self._recall("wiki-2229 봐")
        text = json.loads(body)["result"]["content"][0]["text"]
        self.assertEqual(status, 200)
        self.assertTrue(text.startswith("- wiki-2229 은 볼트에 없음\n\n"), "통제군: 응답은 보강된다")
        self.assertTrue(text.endswith(self.ENGINE_TEXT))
        event = self._wait_for_event()
        self.assertEqual(event["status"], "ok", "보강 뒤 텍스트로 대조하면 줄 수가 달라 mismatch 가 된다")
        self.assertEqual(event["engine_lines"], 1)

    def test_an_engine_error_answer_leaves_an_error_event_and_the_same_bytes(self):
        status, body, _ = self._recall("boom")
        self.assertEqual((status, body), (200, RECALL_ERROR_BODY))
        event = self._wait_for_event()
        self.assertEqual(event["status"], "error")
        self.assertIn("engine unreadable", event["reason"])

    def test_other_tools_leave_no_recall_shadow(self):
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "x"}},
            }
        ).encode()
        _req(self.door_port, "POST", "/mcp", body=payload, headers={"content-type": "application/json"})
        time.sleep(0.3)
        self.assertEqual(self._events(), [])
        self.assertEqual(self.python_calls, [])


_REAL_RECALL_PYTHON = door._recall_python


class RecallReaderSwitchTests(_RecallTestBase, unittest.TestCase):
    """E4-2 회상 읽기 스위치 (DOOR_RECALL_READER). python 이면 recall 을 문이 답하고(번호 노트 블록·
    JSON-RPC 오류 봉투·session_id handover 한 벌), 엔진은 배경 그림자로만 불린다 — session_id 는 뺀다.
    _recall_python·_search_handover 는 목: 선로만 본다. 진짜 길은 recall/test_recall.py·search/test_pg.py."""

    PYTHON_TEXT = "- [wiki-0001.md] 파이썬 답"

    def setUp(self):
        super().setUp()
        self._python_impl = lambda arguments: door.Ok(
            door.recall_answer.Recalled(self.PYTHON_TEXT, "wiki", shown=("wiki/wiki-0001.md",))
        )
        self.handovers: list[tuple[str, list[str]]] = []
        self.handover_outcome = lambda: door.Ok(None)

        def handover(session_id, paths):
            self.handovers.append((session_id, paths))
            return self.handover_outcome()

        self._handover_patch = mock.patch.object(door, "_search_handover", side_effect=handover)
        self._handover_patch.start()
        os.environ["DOOR_RECALL_READER"] = "python"

    def tearDown(self):
        self._handover_patch.stop()
        super().tearDown()

    def _text(self, body: bytes) -> str:
        return json.loads(body)["result"]["content"][0]["text"]

    def test_one_handover_per_session_and_the_engine_shadow_gets_no_session_id(self):
        status, body, _ = self._recall("그냥 검색해줘", session_id="sess-1", project="omb")
        event = self._wait_for_event()
        self.assertEqual(self.handovers, [("sess-1", ["wiki/wiki-0001.md"])], "한 벌 — 2 를 막는 선")
        self.assertEqual(status, 200)
        self.assertEqual(self._text(body), self.PYTHON_TEXT)
        self.assertEqual(len(StubHandler.seen_bodies), 1, "엔진은 그림자 호출 한 번만 받는다")
        shadow_call = json.loads(StubHandler.seen_bodies[0])
        self.assertEqual(shadow_call["params"]["arguments"], {"query": "그냥 검색해줘", "project": "omb"})
        self.assertEqual(event["reader"], "python")

    def test_an_empty_recall_writes_no_handover_even_with_a_session_id(self):
        self._python_impl = lambda arguments: door.Ok(
            door.recall_answer.Recalled(door.recall_answer.NO_EXPERIENCE, "vector")
        )
        status, body, _ = self._recall("그냥 검색해줘", session_id="sess-1")
        self._wait_for_event()
        self.assertEqual((status, self._text(body)), (200, "(no experience recalled)"))
        self.assertEqual(self.handovers, [])

    def test_a_blank_session_id_writes_no_handover(self):
        status, body, _ = self._recall("그냥 검색해줘", session_id="  ")
        self._wait_for_event()
        self.assertEqual((status, self._text(body)), (200, self.PYTHON_TEXT))
        self.assertEqual(self.handovers, [])

    def test_python_reader_answers_from_the_door_and_the_event_says_so(self):
        status, body, _ = self._recall("그냥 검색해줘")
        event = self._wait_for_event()
        self.assertEqual((status, self._text(body)), (200, self.PYTHON_TEXT), "엔진 답(RECALL_BODY)이 아니다")
        self.assertEqual(json.loads(body)["id"], 2)
        self.assertFalse(json.loads(body)["result"]["isError"])
        self.assertEqual((event["reader"], event["status"]), ("python", "mismatch"))
        self.assertEqual(self.handovers, [], "session_id 가 없으면 handover 도 없다")

    def test_agreeing_texts_are_ok_under_the_python_reader(self):
        self._python_impl = lambda arguments: door.Ok(door.recall_answer.Recalled(self.ENGINE_TEXT, "wiki"))
        self._recall("그냥 검색해줘")
        event = self._wait_for_event()
        self.assertEqual((event["reader"], event["status"]), ("python", "ok"))

    def test_default_and_unknown_values_leave_the_answer_to_the_engine(self):
        for value in (None, "engine", "python2"):
            with self.subTest(value=value):
                os.environ.pop("DOOR_RECALL_READER", None)
                if value is not None:
                    os.environ["DOOR_RECALL_READER"] = value
                self.assertEqual(door._recall_reader(), "engine")
                status, body, _ = self._recall("그냥 검색해줘", session_id="sess-1")
                event = self._wait_for_event()
                self.assertEqual((status, body), (200, RECALL_BODY))
                self.assertEqual(event["reader"], "engine")
                self.assertEqual(self.handovers, [], "엔진이 답하면 문은 handover 를 안 쓴다")
                Path(os.environ["BORING_EVENT_LOG"]).unlink()

    def test_empty_query_is_the_engines_32602(self):
        self._python_impl = _REAL_RECALL_PYTHON
        status, body, _ = self._recall("   ")
        self._wait_for_event()
        wire = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(wire["id"], 2)
        self.assertEqual(wire["error"], {"code": -32602, "message": "missing argument: query"})

    def test_a_failed_python_recall_is_32603_and_the_event_is_an_error(self):
        self._python_impl = lambda arguments: door.Err(door.recall_answer.Failed("retrieve: pg down"))
        status, body, _ = self._recall("그냥 검색해줘", session_id="sess-1")
        event = self._wait_for_event()
        self.assertEqual(json.loads(body)["error"], {"code": -32603, "message": "retrieve: pg down"})
        self.assertEqual((event["reader"], event["status"]), ("python", "error"))
        self.assertEqual(self.handovers, [])

    def test_a_failing_handover_costs_one_log_line_and_never_the_answer(self):
        self.handover_outcome = lambda: door.Err("db down")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            status, body, _ = self._recall("그냥 검색해줘", session_id="sess-1")
            self._wait_for_event()
        self.assertEqual((status, self._text(body)), (200, self.PYTHON_TEXT))
        lines = [ln for ln in buf.getvalue().splitlines() if "handover" in ln]
        self.assertEqual(len(lines), 1, buf.getvalue())
        self.assertEqual(len(self.handovers), 1)

    def test_the_python_answer_gets_the_numbered_note_blocks(self):
        with tempfile.TemporaryDirectory() as vault, mock.patch.dict(os.environ, {"BORING_VAULT_DIR": vault}):
            status, body, _ = self._recall("wiki-2229 봐")
            self._wait_for_event()
        text = self._text(body)
        self.assertEqual(status, 200)
        self.assertTrue(text.startswith("- wiki-2229 은 볼트에 없음\n\n"), text)
        self.assertTrue(text.endswith(self.PYTHON_TEXT))

    def test_an_unreachable_engine_leaves_an_error_event_and_the_python_answer(self):
        os.environ["DOOR_UPSTREAM"] = f"http://127.0.0.1:{_free_port()}"
        try:
            status, body, _ = self._recall("그냥 검색해줘")
            event = self._wait_for_event()
        finally:
            os.environ["DOOR_UPSTREAM"] = f"http://127.0.0.1:{self.stub.server_address[1]}"
        self.assertEqual((status, self._text(body)), (200, self.PYTHON_TEXT))
        self.assertEqual((event["reader"], event["status"]), ("python", "error"))


class GapRouteTests(unittest.TestCase):
    """빈자리 문 — HTTP POST /gap 와 MCP tools/call gap 은 같은 인자에 같은 저장 호출을 낸다.
    저장은 문 경계(_gap_record)를 스텁해 받은 인자로 본다. 엔진 스텁은 안 쓴다 — gap 은 늘 문 안."""

    def setUp(self):
        from fastapi.testclient import TestClient

        self._saved_dsn = os.environ.get("DOOR_PG_DSN")
        os.environ["DOOR_PG_DSN"] = "postgresql://boring:boring@127.0.0.1:9/none"
        self.client = TestClient(door.app)
        self.recorded: list = []
        self.outcome = lambda args: door.Ok(door.gap_pg.GapReport("gap:abc", args.kind.value, 1, 1))

        def record(args):
            self.recorded.append(args)
            return self.outcome(args)

        patcher = mock.patch.object(door, "_gap_record", side_effect=record)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        if self._saved_dsn is None:
            os.environ.pop("DOOR_PG_DSN", None)
        else:
            os.environ["DOOR_PG_DSN"] = self._saved_dsn

    def http(self, arguments):
        return self.client.post("/gap", json=arguments)

    def mcp(self, arguments):
        return self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "gap", "arguments": arguments},
            },
        )

    ARGS = {"session_id": "s-1", "query": "how do we deploy", "kind": "stale", "handed": ["/a.md"]}

    def test_http_and_mcp_make_the_same_store_call_and_answer(self):
        http = self.http(self.ARGS)
        mcp = self.mcp(self.ARGS)
        self.assertEqual(http.status_code, 200)
        self.assertEqual(len(self.recorded), 2)
        self.assertEqual(self.recorded[0], self.recorded[1], "같은 인자 → 같은 저장 호출")
        answer = {"gap": "gap:abc", "kind": "stale", "handed": 1, "unknown": 1}
        self.assertEqual(http.json(), answer)
        wire = mcp.json()
        self.assertEqual(wire["id"], 7)
        self.assertEqual(wire["result"]["structuredContent"], answer)
        self.assertFalse(wire["result"]["isError"])

    def test_mcp_gap_never_reaches_the_engine(self):
        with mock.patch.object(door, "_fetch") as fetch:
            self.mcp(self.ARGS)
        fetch.assert_not_called()

    def test_rejections_are_400_and_minus_32602_and_never_store(self):
        cases = [
            ({**self.ARGS, "kind": "gone"}, "kind must be one of missing, stale, broken"),
            ({**self.ARGS, "handed": []}, "stale gap needs handed notes"),
            ({**self.ARGS, "session_id": " "}, "session_id is required"),
            ({**self.ARGS, "query": ""}, "query is required"),
        ]
        for arguments, message in cases:
            http = self.http(arguments)
            self.assertEqual((http.status_code, http.json()), (400, {"error": message}))
            error = self.mcp(arguments).json()["error"]
            self.assertEqual((error["code"], error["message"]), (-32602, message))
        self.assertEqual(self.recorded, [])

    def test_non_object_http_body_is_400(self):
        response = self.client.post("/gap", content=b"[1]", headers={"content-type": "application/json"})
        self.assertEqual(
            (response.status_code, response.json()), (400, {"error": "body must be a JSON object"})
        )

    def test_store_failure_is_visible_502_and_minus_32603(self):
        self.outcome = lambda args: door.Err("disk full")
        http = self.http(self.ARGS)
        self.assertEqual((http.status_code, http.json()), (502, {"error": "gap failed: disk full"}))
        error = self.mcp(self.ARGS).json()["error"]
        self.assertEqual((error["code"], error["message"]), (-32603, "gap failed: disk full"))

    def test_without_dsn_it_says_so(self):
        os.environ.pop("DOOR_PG_DSN", None)
        self.assertEqual(self.http(self.ARGS).status_code, 503)
        self.assertEqual(self.mcp(self.ARGS).json()["error"]["code"], -32603)
        self.assertEqual(self.recorded, [])


if __name__ == "__main__":
    unittest.main()
