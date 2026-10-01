#!/usr/bin/env python3
"""그림자 시험 — 엔진이 실제로 쓴 노트와 파이썬 렌더를 칸별로 대조하는 계약을 못 박는다.

Run: python3 ohmyboring/remember/test_shadow.py   (no pytest dependency)

픽스처는 엔진이 방금 쓴 노트 파일(머리말+본문)과 그것을 낳은 remember 요청·응답의
한 쌍이다. 대조 규약: id·date 는 엔진 것을 그대로 받아 렌더에 넣고, relates_to 는
엔진이 쓴 뒤 다시 쓰므로 제외, PII 게이트(E3a-2) 가 붙인 pii-flag 태그는 사유
'pii_not_ported' 로 따로 센다. 본문 원문은 사건에 싣지 않는다.

Mutation targets: relates_to 제외를 빼는 변이, id·date 를 파이썬 것으로 바꾸는 변이,
엔진 응답에서 경로 뽑는 규칙을 흐리는 변이, pii 사유 매기기를 빼는 변이 각각 시험으로
사망 확인.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "src", ROOT / "agents" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from vault_note import split_frontmatter  # noqa: E402

from ohmyboring.remember.shadow import (  # noqa: E402
    REASON_PII_NOT_PORTED,
    RELATES_TO_EXCLUDED,
    ShadowRequest,
    extract_from_http_response,
    extract_from_mcp_text,
    run_shadow,
    wiki_stem,
)
from ohmyboring.result import Err, Ok  # noqa: E402

#: 엔진이 방금 쓴 노트 파일 — 그것을 낳은 요청은 ARGUMENTS 이고 MCP 답은 MCP_ANSWER.
ENGINE_NOTE = """---
id: wiki-2801
title: 배포 절차 정리
kind: note
origin: personal
project: kb-agent
date: 2026-10-01
tags:
- repo/kb-agent
- deploy
tools:
- kubectl
concepts:
- 배포
claims:
- subject: 배포
  predicate: 절차
  value: make deploy
  kind: fact
  confidence: certain
relates_to:
- "[[wiki-2700]]"
sources: []
omb_session_id: sess-1
author: agent:claude
---

배포는 make deploy 로 한다.
"""

ARGUMENTS = {
    "title": "배포 절차 정리",
    "body": "배포는 make deploy 로 한다.",
    "origin": "personal",
    "repo": "kb-agent",
    "tags": ["deploy"],
    "tools": ["kubectl"],
    "concepts": ["배포"],
    "claims": [
        {
            "subject": "배포",
            "predicate": "절차",
            "value": "make deploy",
            "kind": "fact",
            "confidence": "certain",
        }
    ],
    "omb_session_id": "sess-1",
    "author": "agent:claude",
}

MCP_ANSWER = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "content": [
            {
                "type": "text",
                "text": "remembered → wiki/wiki-2801.md · chunks 3 · graph(tools 1 concepts 1 claims 1) — recallable now",
            }
        ]
    },
}


def _read_note(files: dict[str, str]):
    """note_id -> text | None — 문 컨테이너의 /vault(ro) 읽기를 세는 가짜."""

    def read(vault_dir: str, note_id: str) -> str | None:
        assert vault_dir == "/vault"
        return files.get(note_id)

    return read


class WikiStemTests(unittest.TestCase):
    def test_stem_is_the_basename_without_md(self):
        self.assertEqual(wiki_stem("/vault/wiki/wiki-2801.md"), "wiki-2801")
        self.assertEqual(wiki_stem("wiki/wiki-12.md"), "wiki-12")
        self.assertIsNone(wiki_stem("/vault/wiki/note.md"))
        self.assertIsNone(wiki_stem("/vault/raw/x.md"))


class ExtractTests(unittest.TestCase):
    def test_mcp_remembered_answer_names_the_new_note(self):
        match extract_from_mcp_text("remembered → wiki/wiki-2801.md · chunks 3 — recallable now"):
            case Ok(extracted):
                self.assertEqual(
                    (extracted.note_id, extracted.source_path, extracted.duplicate),
                    ("wiki-2801", "/vault/wiki/wiki-2801.md", False),
                )
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_mcp_duplicate_answer_names_the_existing_note(self):
        match extract_from_mcp_text("skipped — duplicate of /vault/wiki/wiki-2769.md"):
            case Ok(extracted):
                self.assertEqual(
                    (extracted.note_id, extracted.source_path, extracted.duplicate),
                    ("wiki-2769", "/vault/wiki/wiki-2769.md", True),
                )
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_mcp_unrecognized_answer_is_an_error(self):
        match extract_from_mcp_text("-32000 boom"):
            case Err(reason):
                self.assertIn("unrecognized", reason)
            case other:
                self.fail(f"expected Err, got {other!r}")

    def test_http_answer_reads_source_path_and_duplicate(self):
        body = json.dumps(
            {
                "source_path": "/vault/wiki/wiki-2801.md",
                "wiki_id": "wiki-2801",
                "duplicate": None,
                "supersedes": 0,
                "unknown": 0,
            }
        ).encode()
        match extract_from_http_response(200, body):
            case Ok(extracted):
                self.assertEqual(extracted.duplicate, False)
                self.assertEqual(extracted.note_id, "wiki-2801")
            case other:
                self.fail(f"expected Ok, got {other!r}")
        dup = json.dumps(
            {
                "source_path": "/vault/wiki/wiki-2769.md",
                "wiki_id": "wiki-2769",
                "duplicate": "/vault/wiki/wiki-2769.md",
                "supersedes": 0,
                "unknown": 0,
            }
        ).encode()
        match extract_from_http_response(200, dup):
            case Ok(extracted):
                self.assertEqual(extracted.duplicate, True)
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_http_refusal_is_an_error_not_a_note(self):
        match extract_from_http_response(400, b'{"error":"missing argument: title"}'):
            case Err(reason):
                self.assertIn("400", reason)
            case other:
                self.fail(f"expected Err, got {other!r}")


class RunShadowTests(unittest.TestCase):
    def _run(self, files: dict[str, str], arguments=ARGUMENTS, answer=MCP_ANSWER, route="mcp"):
        body = json.dumps(answer, ensure_ascii=False).encode()
        return run_shadow(
            ShadowRequest(
                route=route,
                arguments=arguments,
                engine_status=200,
                engine_body=body,
                vault_dir="/vault",
                read_note=_read_note(files),
                split_frontmatter=split_frontmatter,
            )
        )

    def test_matching_note_is_ok_field_by_field(self):
        event = self._run({"wiki-2801": ENGINE_NOTE})
        self.assertEqual(event.status, "ok")
        self.assertEqual(event.fields, ())
        self.assertEqual(event.source_path, "/vault/wiki/wiki-2801.md")
        self.assertEqual(event.omb_session_id, "sess-1")
        self.assertFalse(event.duplicate)

    def test_engine_date_and_id_are_taken_as_written(self):
        # date/id 대조는 E3a-2 이후 — 파이썬 렌더는 엔진 것을 그대로 받아 넣어 이 칸은
        # 어긋날 수가 없다. 엔진 날짜를 어제로 바꿔도 ok 가 나와야 한다.
        note = ENGINE_NOTE.replace("date: 2026-10-01", "date: 2026-09-30")
        event = self._run({"wiki-2801": note})
        self.assertEqual(event.status, "ok")
        self.assertNotIn("date", event.fields)
        self.assertNotIn("id", event.fields)

    def test_engine_filled_relates_to_is_excluded_with_reason(self):
        # relates_to 는 엔진이 쓴 뒤 그래프 투영이 다시 쓴다 — 불일치로 안 잡힌다.
        event = self._run({"wiki-2801": ENGINE_NOTE})
        self.assertEqual(event.status, "ok")

    def test_a_changed_title_is_a_mismatch_named_by_field(self):
        note = ENGINE_NOTE.replace("title: 배포 절차 정리", "title: 배포 절차 정리 (고침)")
        event = self._run({"wiki-2801": note})
        self.assertEqual(event.status, "mismatch")
        self.assertEqual(event.fields, ("title",))
        self.assertIsNone(event.reason)

    def test_a_changed_body_is_a_mismatch_named_by_field(self):
        note = ENGINE_NOTE.replace("배포는 make deploy 로 한다.", "배포는 수동으로 한다.")
        event = self._run({"wiki-2801": note})
        self.assertEqual(event.fields, ("body",))

    def test_missing_note_in_vault_is_an_error(self):
        event = self._run({})
        self.assertEqual(event.status, "error")
        self.assertIn("note not in vault", event.reason)
        self.assertEqual(event.source_path, "/vault/wiki/wiki-2801.md")

    def test_engine_refusal_is_an_error_without_a_note(self):
        body = json.dumps({"error": {"code": -32602, "message": "missing argument: title"}}).encode()
        event = run_shadow(
            ShadowRequest(
                route="mcp",
                arguments=ARGUMENTS,
                engine_status=400,
                engine_body=body,
                vault_dir="/vault",
                read_note=_read_note({"wiki-2801": ENGINE_NOTE}),
                split_frontmatter=split_frontmatter,
            )
        )
        self.assertEqual(event.status, "error")
        self.assertIn("400", event.reason)

    def test_pii_flagged_engine_note_is_counted_separately(self):
        note = ENGINE_NOTE.replace("- deploy\n", "- deploy\n- pii-flag\n")
        event = self._run({"wiki-2801": note})
        self.assertEqual(event.status, "mismatch")
        self.assertEqual(event.fields, ("tags",))
        self.assertEqual(event.reason, REASON_PII_NOT_PORTED)

    def test_http_route_compares_the_source_path_note(self):
        answer = {
            "source_path": "/vault/wiki/wiki-2801.md",
            "wiki_id": "wiki-2801",
            "duplicate": None,
            "supersedes": 0,
            "unknown": 0,
        }
        event = self._run({"wiki-2801": ENGINE_NOTE}, answer=answer, route="remember")
        self.assertEqual(event.status, "ok")

    def test_duplicate_branch_compares_the_existing_note(self):
        answer = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "skipped — duplicate of /vault/wiki/wiki-2769.md"}]
            },
        }
        # 중복 갈래도 칸별 대조는 그대로 돈다 — 기존 노트의 제목이 달라 mismatch.
        existing = ENGINE_NOTE.replace("title: 배포 절차 정리", "title: 예전 배포 노트")
        event = self._run({"wiki-2769": existing}, answer=answer)
        self.assertTrue(event.duplicate)
        self.assertEqual(event.source_path, "/vault/wiki/wiki-2769.md")
        self.assertEqual(event.status, "mismatch")
        self.assertEqual(event.fields, ("title",))

    def test_duplicate_branch_against_an_identical_note_is_ok(self):
        answer = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "skipped — duplicate of /vault/wiki/wiki-2769.md"}]
            },
        }
        event = self._run({"wiki-2769": ENGINE_NOTE}, answer=answer)
        self.assertEqual(event.status, "ok")
        self.assertTrue(event.duplicate)

    def test_event_payload_carries_no_note_body(self):
        event = self._run({"wiki-2801": ENGINE_NOTE})
        from ohmyboring.remember.shadow import event_payload

        payload = event_payload(event)
        self.assertEqual(payload["fields"], [])
        self.assertEqual(payload["relates_to"], RELATES_TO_EXCLUDED)
        self.assertEqual(payload["source_path"], "/vault/wiki/wiki-2801.md")
        self.assertEqual(payload["omb_session_id"], "sess-1")
        self.assertNotIn("배포는 make deploy", json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
