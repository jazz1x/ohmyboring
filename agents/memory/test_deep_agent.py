#!/usr/bin/env python3
"""boring_deep_agent against test_store's stub — a scripted model, no LLM, loopback only.

Run: python3 -m unittest agents.memory.test_deep_agent
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from deep_agent import boring_deep_agent  # noqa: E402
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from store import _subject_for  # noqa: E402
from test_store import _Handler  # noqa: E402

AGENT = "r8"
SESSION = "r8-session"
BODY = "우리 순위는 엔진이 매긴다"
HITS = [
    {
        "id": "note-b",
        "origin": "wiki",
        "project": "omb",
        "source_path": "/vault/wiki/b.md",
        "snippet": "B 노트",
        "used_count": 0,
        "contested_count": 2,
    },
    {
        "id": "note-a",
        "origin": "wiki",
        "project": "omb",
        "source_path": "/vault/wiki/a.md",
        "snippet": "A 노트",
        "used_count": 3,
        "contested_count": 0,
    },
]


class _ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class _RememberingHandler(_Handler):
    """test_store's stub, plus: a /remember becomes the current claim its subject reads back."""

    def do_POST(self) -> None:
        super().do_POST()
        method, path, body = self.server.requests[-1]
        if path == "/remember":
            claim = body["claims"][0]
            note = f"/vault/wiki/remembered-{len(self.server.requests)}.md"
            self.server.claim_sources[claim["subject"]] = {
                "subject": claim["subject"],
                "note": note,
                "valid_from": "2026-09-24T01:00:00+00:00",
                "value": claim["value"],
                "candidates": [{"note": note, "valid_from": "2026-09-24T01:00:00+00:00"}],
            }


def _call(name: str, args: dict, n: int) -> dict:
    return {"name": name, "args": args, "id": f"call-{n}", "type": "tool_call"}


class DeepAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _RememberingHandler)
        self.server.requests = []
        self.server.claim_sources = {}
        self.server.listed = []
        self.server.hits = HITS
        self.server.remember_response = {"source_path": "/vault/wiki/x.md", "duplicate": None}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        url = f"http://127.0.0.1:{self.server.server_port}"
        script = iter(
            [
                AIMessage(content="", tool_calls=[_call("recall", {"query": "우리 순위"}, 1)]),
                AIMessage(
                    content="",
                    tool_calls=[
                        _call("write_file", {"file_path": "/memories/learned.md", "content": BODY}, 2),
                        _call("write_file", {"file_path": "/tmp/scratch.md", "content": "scratch"}, 3),
                    ],
                ),
                AIMessage(
                    content="", tool_calls=[_call("read_file", {"file_path": "/memories/learned.md"}, 4)]
                ),
                AIMessage(content="끝"),
            ]
        )
        agent = boring_deep_agent(
            _ScriptedModel(messages=script),
            engine_url=url,
            door_url=url,
            agent_name=AGENT,
            session_id=SESSION,
        )
        self.messages = agent.invoke({"messages": [{"role": "user", "content": "배운 것을 적어 둬"}]})[
            "messages"
        ]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _posts(self, path: str) -> list[dict]:
        return [body for method, p, body in self.server.requests if method == "POST" and p == path]

    def _tool_answer(self, call_id: str) -> str:
        (msg,) = [m for m in self.messages if isinstance(m, ToolMessage) and m.tool_call_id == call_id]
        return msg.content

    def test_memories_write_is_one_remember_and_reads_back(self) -> None:
        self.assertIn("/tmp/scratch.md", self._tool_answer("call-3"))
        (body,) = self._posts("/remember")
        self.assertIn("langgraph-store", body["tags"])
        self.assertEqual(
            body["claims"][0]["subject"], _subject_for(("agent", AGENT, "memories"), "/learned.md")
        )
        self.assertEqual(json.loads(body["claims"][0]["value"])["value"]["content"], BODY)
        self.assertIn(BODY, self._tool_answer("call-4"))
        claim_source_gets = [
            p for m, p, _ in self.server.requests if m == "GET" and p.startswith("/claim-source?")
        ]
        self.assertEqual(len(claim_source_gets), 3, "no memory= — nothing reads /memories/ into the prompt")

    def test_recall_hands_over_to_the_session_in_the_engine_order(self) -> None:
        self.assertEqual(
            self._posts("/search"), [{"query": "우리 순위", "max_results": 5, "session_id": SESSION}]
        )
        self.assertEqual(
            self._tool_answer("call-1").splitlines(),
            ["note-b · used=0 contested=2 · B 노트", "note-a · used=3 contested=0 · A 노트"],
        )


if __name__ == "__main__":
    unittest.main()
