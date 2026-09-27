#!/usr/bin/env python3
"""BoringRetriever and record_verdict against a stub http.server — no engine, no network
beyond loopback. The stub stands where the engine would sit and remembers the last body
it was handed, so each test can read back exactly what the retriever sent.

Run: python3 -m unittest agents.memory.test_retriever
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from retriever import BoringRetriever, record_notes, record_verdict  # noqa: E402

HITS = [
    {
        "id": "note-1",
        "origin": "wiki",
        "project": "omb",
        "source_path": "/vault/wiki/wiki-0001.md",
        "snippet": "첫 번째 노트",
        "dist": 0.31,
        "dist_kind": "cosine",
        "used_count": 4,
        "contested_count": 1,
        "said_by_owner": 2,
        "superseded_by": ["wiki-0002"],
    },
    {
        "id": "note-2",
        "origin": "wiki",
        "project": "omb",
        "source_path": "/vault/wiki/wiki-0002.md",
        "snippet": "두 번째 노트",
        "used_count": 0,
        "contested_count": 0,
        "said_by_owner": 0,
    },
]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.last_body = body
        self.server.last_token = self.headers.get("X-Boring-Owner-Token")
        if self.server.status != 200:
            data = json.dumps({"error": "stub failure"}).encode("utf-8")
            self.send_response(self.server.status)
        elif self.path == "/search":
            data = json.dumps({"hits": self.server.hits}).encode("utf-8")
            self.send_response(200)
        else:  # /consumption — echo the verdict receipt the engine shape returns
            data = json.dumps(
                {
                    "session": body["session_id"],
                    "used": 0,
                    "contested": 1,
                    "supersedes": 0,
                    "unknown": 0,
                }
            ).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


class RetrieverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.server.hits = []
        self.server.status = 200
        self.server.last_body = None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_search_maps_hits_in_order_and_sends_session_id(self) -> None:
        self.server.hits = HITS
        retriever = BoringRetriever(base_url=self.base_url, session_id="sess-A", project="omb")
        docs = retriever.invoke("우리 순위")
        self.assertEqual([d.id for d in docs], ["note-1", "note-2"])
        self.assertEqual([d.page_content for d in docs], ["첫 번째 노트", "두 번째 노트"])
        self.assertEqual(
            docs[0].metadata,
            {
                "source_path": "/vault/wiki/wiki-0001.md",
                "project": "omb",
                "origin": "wiki",
                "used_count": 4,
                "contested_count": 1,
                "said_by_owner": 2,
                "superseded_by": ["wiki-0002"],
                "dist": 0.31,
                "dist_kind": "cosine",
            },
        )
        self.assertEqual(
            docs[1].metadata,
            {
                "source_path": "/vault/wiki/wiki-0002.md",
                "project": "omb",
                "origin": "wiki",
                "used_count": 0,
                "contested_count": 0,
                "said_by_owner": 0,
                "superseded_by": [],
            },
        )
        self.assertEqual(
            self.server.last_body,
            {"query": "우리 순위", "max_results": 5, "project": "omb", "session_id": "sess-A"},
        )

    def test_engine_failure_raises_and_empty_hits_is_empty(self) -> None:
        self.server.status = 500
        with self.assertRaises(urllib.error.HTTPError):
            BoringRetriever(base_url=self.base_url).invoke("q")
        self.server.status = 200
        self.server.hits = []
        self.assertEqual(BoringRetriever(base_url=self.base_url).invoke("q"), [])

    def test_record_verdict_sends_verdict_judge_and_timestamp(self) -> None:
        resp = record_verdict(self.base_url, "sess-B", "contested", "inferred")
        body = self.server.last_body
        self.assertEqual(set(body), {"session_id", "observed_at", "verdict", "judge"})
        self.assertEqual(body["session_id"], "sess-B")
        self.assertEqual(body["verdict"], "contested")
        self.assertEqual(body["judge"], "inferred")
        datetime.fromisoformat(body["observed_at"])  # raises if not RFC 3339
        self.assertEqual(
            resp, {"session": "sess-B", "used": 0, "contested": 1, "supersedes": 0, "unknown": 0}
        )

    def test_owner_judge_carries_the_owner_token_and_no_one_else_does(self) -> None:
        with mock.patch.dict(os.environ, {"BORING_OWNER_TOKEN": "door-1"}):
            record_verdict(self.base_url, "sess-B", "used", "owner")
            self.assertEqual(self.server.last_token, "door-1")
            record_notes(self.base_url, "sess-B", "owner", used=["/vault/wiki/wiki-0001.md"])
            self.assertEqual(self.server.last_token, "door-1")
            record_verdict(self.base_url, "sess-B", "used", "agent:r2-check")
            self.assertIsNone(self.server.last_token, "control: a non-owner judge carries no token")

    def test_record_notes_sends_lists_and_judge_without_verdict(self) -> None:
        record_notes(
            self.base_url,
            "sess-C",
            "agent:r2-check",
            used=["/vault/wiki/wiki-0001.md"],
            contested=["/vault/wiki/wiki-0002.md"],
        )
        body = self.server.last_body
        self.assertEqual(set(body), {"session_id", "observed_at", "judge", "used", "contested"})
        self.assertNotIn("verdict", body)
        self.assertEqual(body["session_id"], "sess-C")
        self.assertEqual(body["judge"], "agent:r2-check")
        self.assertEqual(body["used"], ["/vault/wiki/wiki-0001.md"])
        self.assertEqual(body["contested"], ["/vault/wiki/wiki-0002.md"])
        datetime.fromisoformat(body["observed_at"])  # raises if not RFC 3339

    def test_record_notes_rejects_judging_nothing(self) -> None:
        with self.assertRaises(ValueError):
            record_notes(self.base_url, "sess-C", "agent:r2-check")
        self.assertIsNone(self.server.last_body, "no request may leave for an empty judgement")

    def test_claims_round_trip_into_metadata(self) -> None:
        self.server.hits = [
            {
                **HITS[0],
                "claims": [
                    {
                        "subject": "omb",
                        "predicate": "status",
                        "value": "migrating",
                        "source_path": "/vault/wiki/wiki-0001.md",
                        "valid_from": "2026-09-23T00:00:00+00:00",
                    }
                ],
                "claims_total": 4,
            }
        ]
        retriever = BoringRetriever(base_url=self.base_url, claims=3)
        docs = retriever.invoke("omb 상태")
        self.assertEqual(self.server.last_body["claims"], 3)
        self.assertEqual(docs[0].metadata["claims"], self.server.hits[0]["claims"])
        self.assertEqual(docs[0].metadata["claims_total"], 4)


if __name__ == "__main__":
    unittest.main()
