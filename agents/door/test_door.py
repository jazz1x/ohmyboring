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
  (e) the route table is read from the contract snapshot: all 24 (method, path)
      pairs registered, nothing beyond (AC1)
  (f) only content-type, accept, mcp-session-id, x-request-id travel upstream;
      authorization and x-forwarded-for do not (AC4)
  (g) a non-JSON upstream content-type passes through untouched (AC3)
  (h) an upstream response without content-type stays without one (AC3)
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import door  # noqa: E402
import uvicorn  # noqa: E402

STUB_CONTENT_TYPE = "application/json; charset=utf-8"

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
    hits = 0

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, HEALTH_BODY)
        else:
            self._reply(404, b'{"error":"stub 404"}')

    def do_POST(self):
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
        deadline = 10.0
        import time

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
        entries = json.loads(door._CONTRACT.read_text(encoding="utf-8"))["http_routes"]
        expected = set()
        for entry in entries:
            method, path = entry.split(" ", 1)
            expected.add((method, path))
        actual = {(method, route.path) for route in door.app.routes for method in route.methods}
        self.assertEqual(actual, expected)
        self.assertEqual(len(expected), 24)

    def test_request_header_policy(self):
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "mcp-session-id": "sess-123",
            "x-request-id": "req-abc",
            "authorization": "Bearer secret",
            "x-forwarded-for": "1.2.3.4",
        }
        status, _, _ = _req(self.door_port, "POST", "/mcp", body=payload, headers=headers)
        self.assertEqual(status, 200)
        seen = StubHandler.seen_headers[-1]
        for name in ("content-type", "accept", "mcp-session-id", "x-request-id"):
            self.assertEqual(seen.get(name), headers[name])
        self.assertNotIn("authorization", seen)
        self.assertNotIn("x-forwarded-for", seen)

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


if __name__ == "__main__":
    unittest.main()
