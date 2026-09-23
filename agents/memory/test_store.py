#!/usr/bin/env python3
"""BoringStore against a stub http.server — no engine, no door, no network beyond loopback.

The stub stands where the engine and the door would sit (one process, paths split the
roles) and records every request it is handed, so each test can read back exactly what
the store sent and count what it must never send.

Run: python3 -m unittest agents.memory.test_store
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import unittest
import urllib.parse
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from langgraph.store.base import GetOp, ListNamespacesOp, PutOp  # noqa: E402
from store import BoringStore, _subject_for  # noqa: E402

NS = ("users", "u1", "memories")

HIT = {
    "id": "wiki-0001",
    "origin": "wiki",
    "project": "omb",
    "source_path": "/vault/wiki/wiki-0001.md",
    "snippet": "문서 정리 원칙 노트",
    "used_count": 2,
    "contested_count": 0,
    "superseded_by": [],
}


def _claim_payload(namespace: tuple[str, ...], key: str, value: dict, note: str, at: str):
    return {
        "subject": _subject_for(namespace, key),
        "note": note,
        "valid_from": at,
        "value": json.dumps(
            {"namespace": list(namespace), "key": key, "value": value},
            ensure_ascii=False,
            sort_keys=True,
        ),
        "candidates": [{"note": note, "valid_from": at}],
    }


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.requests.append(("GET", self.path, None))
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/claim-source":
            self._send(404, {"error": "unknown"})
            return
        subject = urllib.parse.parse_qs(parsed.query)["subject"][0]
        payload = self.server.claim_sources.get(subject)
        if payload is None:
            self._send(404, {"error": "no current claim for subject"})
        else:
            self._send(200, payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(("POST", self.path, body))
        if self.path == "/remember":
            self._send(200, self.server.remember_response)
        elif self.path == "/search":
            self._send(200, {"hits": self.server.hits})
        else:
            self._send(404, {"error": "unknown"})

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.claim_sources = {}
        self.server.hits = []
        self.server.remember_response = {
            "source_path": "/vault/wiki/wiki-9000.md",
            "wiki_id": "wiki-9000",
            "duplicate": None,
            "supersedes": 0,
            "unknown": 0,
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.store = BoringStore(engine_url=self.base_url, door_url=self.base_url)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _remember_bodies(self) -> list[dict]:
        return [
            body for method, path, body in self.server.requests if method == "POST" and path == "/remember"
        ]

    def test_put_new_key_sends_remember_without_supersedes(self) -> None:
        self.assertIsNone(self.store.put(NS, "prefs", {"lang": "ko"}))

        (body,) = self._remember_bodies()
        subject = _subject_for(NS, "prefs")
        revision = hashlib.sha256(
            json.dumps({"lang": "ko"}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:8]
        self.assertEqual(body["title"], f"langgraph store users/u1/memories/prefs {revision}")
        self.assertEqual(
            body["body"],
            "```json\n" + json.dumps({"lang": "ko"}, ensure_ascii=False, sort_keys=True) + "\n```",
        )
        self.assertEqual(
            body["claims"],
            [
                {
                    "subject": subject,
                    "predicate": "langgraph-store-value",
                    "value": json.dumps(
                        {"namespace": list(NS), "key": "prefs", "value": {"lang": "ko"}},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                }
            ],
        )
        self.assertEqual(body["tags"], ["langgraph-store"])
        self.assertEqual(body["origin"], "personal")
        self.assertNotIn("supersedes", body, "a new key supersedes nothing")

        # The subject is a canon-inert hash: raw-key spellings never fold into one slot.
        stable = re.compile(r"^langgraph-store-[0-9a-f]{32}$")
        for key in ("user_prefs", "User Prefs"):
            self.assertRegex(_subject_for(("users",), key), stable)
        self.assertNotEqual(_subject_for(("users",), "user_prefs"), _subject_for(("users",), "User Prefs"))

    def test_put_existing_key_supersedes_and_same_value_reput_is_a_noop(self) -> None:
        subject = _subject_for(NS, "prefs")
        self.server.claim_sources[subject] = _claim_payload(
            NS, "prefs", {"lang": "ko"}, "/vault/wiki/wiki-9001.md", "2026-09-23T10:00:00+00:00"
        )
        self.assertIsNone(self.store.put(NS, "prefs", {"lang": "ja"}))
        (body1,) = self._remember_bodies()
        self.assertEqual(body1["supersedes"], ["/vault/wiki/wiki-9001.md"])

        # A→B→A: the head moved on, so the old value comes back under a fresh title —
        # the engine's title-duplicate gate must not swallow it as a seen title.
        self.server.claim_sources[subject] = _claim_payload(
            NS, "prefs", {"lang": "ja"}, "/vault/wiki/wiki-9002.md", "2026-09-23T10:05:00+00:00"
        )
        self.assertIsNone(self.store.put(NS, "prefs", {"lang": "ko"}))
        body1, body2 = self._remember_bodies()
        self.assertEqual(body2["supersedes"], ["/vault/wiki/wiki-9002.md"])
        self.assertNotEqual(body1["title"], body2["title"])

        # Same value already current: the GET happens, no /remember leaves.
        self.server.claim_sources[subject] = _claim_payload(
            NS, "prefs", {"lang": "ko"}, "/vault/wiki/wiki-9003.md", "2026-09-23T10:10:00+00:00"
        )
        self.assertIsNone(self.store.put(NS, "prefs", {"lang": "ko"}))
        self.assertEqual(len(self._remember_bodies()), 2, "a same-value re-put writes nothing")

    def test_get_404_is_none_200_roundtrips_and_missing_value_is_keyerror(self) -> None:
        self.assertIsNone(self.store.get(NS, "prefs"))

        subject = _subject_for(NS, "prefs")
        payload = _claim_payload(
            NS, "prefs", {"lang": "ja"}, "/vault/wiki/wiki-9002.md", "2026-09-23T11:00:00+00:00"
        )
        payload["candidates"] = [
            {"note": "/vault/wiki/wiki-9001.md", "valid_from": "2026-09-23T10:00:00+00:00"},
            {"note": "/vault/wiki/wiki-9002.md", "valid_from": "2026-09-23T11:00:00+00:00"},
        ]
        self.server.claim_sources[subject] = payload
        item = self.store.get(NS, "prefs")
        self.assertEqual(item.value, {"lang": "ja"})
        self.assertEqual(item.key, "prefs")
        self.assertEqual(item.namespace, NS)
        self.assertEqual(item.created_at, datetime(2026, 9, 23, 10, 0, tzinfo=UTC))
        self.assertEqual(item.updated_at, datetime(2026, 9, 23, 11, 0, tzinfo=UTC))

        (item_via_abatch,) = asyncio.run(self.store.abatch([GetOp(NS, "prefs")]))
        self.assertEqual(item_via_abatch.value, {"lang": "ja"})

        del payload["value"]
        with self.assertRaises(KeyError):
            self.store.get(NS, "prefs")

    def test_delete_index_and_ttl_are_refused_before_any_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "not deleted"):
            self.store.delete(NS, "prefs")
        with self.assertRaises(ValueError):
            self.store.batch([PutOp(NS, "prefs", None)])
        with self.assertRaises(ValueError):
            self.store.batch([PutOp(NS, "prefs", {"lang": "ko"}, index=["lang"])])
        with self.assertRaises(ValueError):
            self.store.batch([PutOp(NS, "prefs", {"lang": "ko"}, ttl=5.0)])
        self.assertEqual(self.server.requests, [], "a refused op must send nothing")

    def test_search_boring_prefix_maps_hits_and_anything_else_is_next_wheel(self) -> None:
        self.server.hits = [HIT]
        items = self.store.search(("boring",), query="문서 정리")
        (item,) = items
        self.assertEqual(item.namespace, ("boring", "omb"))
        self.assertEqual(item.key, "wiki-0001")
        self.assertEqual(item.value["content"], "문서 정리 원칙 노트")
        self.assertEqual(item.value["source_path"], "/vault/wiki/wiki-0001.md")
        self.assertIsNone(item.score)
        self.assertEqual(self.server.requests[-1][2], {"query": "문서 정리", "max_results": 10})

        for bad_prefix in (("agents",), ("boring-agent",)):
            with self.assertRaises(NotImplementedError):
                self.store.search(bad_prefix, query="q")
        with self.assertRaises(ValueError):
            self.store.search(("boring",), query=None)
        with self.assertRaises(ValueError):
            self.store.search(("boring",), query="q", offset=1)
        with self.assertRaises(ValueError):
            self.store.search(("boring",), query="q", filter={"k": "v"})
        with self.assertRaises(NotImplementedError):
            self.store.batch([ListNamespacesOp()])


if __name__ == "__main__":
    unittest.main()
