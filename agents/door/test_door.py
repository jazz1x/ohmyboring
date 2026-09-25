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
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

# Loaded as a package (the way uvicorn and the container load it) because door.py
# imports its sibling with `from . import approved`; test_python_deps.py has no
# notion of repo-local packages, so the import goes through importlib.
import importlib  # noqa: E402

import fastapi  # noqa: E402
import uvicorn  # noqa: E402

door = importlib.import_module("agents.door.door")
approved = importlib.import_module("agents.door.approved")
claim_source = importlib.import_module("agents.door.claim_source")

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
        if isinstance(parsed, dict) and parsed.get("method") == "tools/list":
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

        self._sync_result = {"ok": True, "summary": {"ingest_new": 0, "ingest_repaired": 2}}
        door._split_subjects_connect = fake_connect
        door._fetch_split_subject_rows = fake_fetch
        door._call_engine_sync = fake_sync
        door._emit_subject_merged_event = fake_emit

    def tearDown(self):
        door._split_subjects_connect = self._orig_connect
        door._fetch_split_subject_rows = self._orig_fetch
        door._call_engine_sync = self._orig_sync
        door._emit_subject_merged_event = self._orig_emit
        if self._saved_dsn is None:
            os.environ.pop("DOOR_PG_DSN", None)
        else:
            os.environ["DOOR_PG_DSN"] = self._saved_dsn
        os.environ.pop("BORING_OWNER_TOKEN", None)
        if self._saved_token is not None:
            os.environ["BORING_OWNER_TOKEN"] = self._saved_token

    def _post(self, subject: str, token: str | None = None):
        payload = json.dumps({"subject": subject}).encode()
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
            self.assertEqual(status, 200)
            out = json.loads(body)
            self.assertEqual(self.touched["delete"], ["/b.md"], f"token={token}")
            self.assertEqual(self.touched["update"], ["/b.md"], f"token={token}")
            self.assertEqual(out["owner_held"], ["/a.md"])
            self.assertEqual(out["deleted_rows"], 1)
            self.assertEqual(out["remaining_variants"], 2)
            self.assertEqual(self.touched["event_owner_held"], ["/a.md"])

        self._fetch_calls = 0
        status, body, _ = self._post("foodspring-front", "tok-owner")
        self.assertEqual(status, 200)
        self.assertEqual(
            self.touched["delete"], ["/a.md", "/b.md"], "control: the owner token merges owner rows"
        )
        self.assertEqual(json.loads(body)["owner_held"], [])

    def test_order_unknown_subject_and_sync_failure(self):
        # (a) unknown canon form — 404, nothing was touched
        status, body, _ = self._post("nobody-canonical")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))
        self.assertEqual(self.order, [])

        # (b) success — the stub cursor records DELETE, UPDATE, commit, the sync call and the
        # recount, in that order
        self._fetch_calls = 0
        status, body, _ = self._post("foodspring-front")
        self.assertEqual(status, 200)
        self.assertEqual(self.order, ["delete", "update", "commit", "sync", "event"])
        out = json.loads(body)
        self.assertEqual(out["deleted_rows"], 2)
        self.assertEqual(out["reread_notes"], 2)
        self.assertEqual(out["remaining_variants"], 1)
        self.assertEqual(out["sync"], {"ingest_new": 0, "ingest_repaired": 2})
        self.assertEqual(out["event"], "delivered")
        self.assertEqual(self.touched["delete"], ["/a.md", "/b.md"])
        self.assertEqual(out["owner_held"], [])

        # (c) a failing sync is a 502 whose body says so — but the delete/update/commit already
        # ran, so the response still carries the (already-committed) row counts
        self.order.clear()
        self._fetch_calls = 0
        self._sync_result = {"ok": False, "error": "engine unreachable: connection refused"}
        status, body, _ = self._post("foodspring-front")
        self.assertEqual(status, 502)
        out = json.loads(body)
        self.assertEqual(out["deleted_rows"], 2)
        self.assertEqual(out["reread_notes"], 2)
        self.assertEqual(out["sync"], {"error": "engine unreachable: connection refused"})
        self.assertEqual(self.order, ["delete", "update", "commit", "sync"])


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


if __name__ == "__main__":
    unittest.main()
