#!/usr/bin/env python3
"""Unit tests for the door — stdlib only, no live engine, no network beyond localhost.

A stub engine (http.server, random port) stands in for the Rust engine and the
real door runs under uvicorn in a thread against it. Covers:
  (a) GET /health body bytes are identical to the engine's — sha256 equal (AC2)
  (b) POST /mcp forwards the request body untouched and returns the response
      byte-for-byte, tool order included (AC3)
  (c) with the engine down the door answers 502 and the ROP JSON body,
      never a 200 with an empty body (AC4)
  (d) unregistered paths (GET /nope, POST /search) get FastAPI's 404 and never
      reach the stub — the door is not a catch-all proxy (AC6)
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
    """The engine's stand-in: fixed /health, tools/list answer, otherwise echo."""

    seen_bodies: list[bytes] = []
    seen_content_types: list[str] = []
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
            method = json.loads(body).get("method")
        except (ValueError, AttributeError):
            method = None
        if method == "tools/list":
            self._reply(200, TOOLS_LIST_BODY)
        else:
            self._reply(200, body)

    def _reply(self, code: int, body: bytes) -> None:
        type(self).hits += 1
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
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
        status, body, _ = _req(self.door_port, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, HEALTH_BODY)
        self.assertEqual(hashlib.sha256(body).hexdigest(), hashlib.sha256(HEALTH_BODY).hexdigest())

    def test_mcp_post_passthrough(self):
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
        status, body, _ = _req(
            self.door_port, "POST", "/mcp", body=payload, headers={"content-type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(StubHandler.seen_bodies[-1], payload)
        self.assertEqual(StubHandler.seen_content_types[-1], "application/json")
        self.assertEqual(body, TOOLS_LIST_BODY)
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
            self.door_port, "POST", "/search", body=b"{}", headers={"content-type": "application/json"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(StubHandler.hits, hits_before, "unregistered paths must not reach the stub")


if __name__ == "__main__":
    unittest.main()
