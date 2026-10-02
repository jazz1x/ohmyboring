#!/usr/bin/env python3
"""그림자 시험 — 엔진이 한 결정과 파이썬이 한 결정을 맞추고, 저장·대체에서는 칸별 대조까지
하는 계약(E3a-2)을 못 박는다.

Run: python3 ohmyboring/remember/test_shadow.py   (no pytest dependency)

픽스처는 엔진이 방금 쓴 노트 파일(머리말+본문)과 그것을 낳은 remember 요청·응답의 한 쌍,
그리고 그림자 주변 장치(볼트 목록·PII 규칙·임베딩 가짜)다. 대조 규약:
  결정 대조 — PII 게이트(block)와 중복 문 갈래 다섯(same_session·probable_session·
  exact_title·embedding, 대체는 점수 판정)과 새 노트를 엔진 응답의 결정과 맞춘다.
  걸러진 요청은 결정만 비교한다 — 기존 노트와 칸을 맞추는 일은 없다(E3a-1 이 여기서
  거짓 어긋남 하나를 낸 사례가 regression 사례로 박혀 있다).
  칸 대조 — 저장·대체에서만: id·date 는 엔진 것을 받고 relates_to 는 제외.
  어긋남마다 사유 — decision … / fields … / pii-rules … / pii-gate-missing.

Mutation targets: 걸러둘 결정만 비교하는 규약을 빼는 변이(칸 대조로 돌아가는 변이),
PII 게이트를 빼는 변이, 대체 점수 +8 을 빼는 변이, 엔진이 방금 쓴 노트를 후보에서
빼는 규칙을 빼는 변이 각각 시험으로 사망 확인.

기존 어긋남 8건의 원인 재현(2026-10-01, 66건 중 8):
  PII 미이식 7 — 태그 pii-flag 와 [NAME]/[IP] 가림. 게이트 이식 뒤 규칙이 같으면 ok,
  규칙 파일이 없으면 pii-gate-missing, 갈리면 pii-rules <이름> 이 사유로 남는다.
  그중 3건은 태그가 같아 사유가 비어 있었음 — 태그 불일치에만 사유를 붙이던 옛 방식의
  빈틈이다. 지금은 칸 불일치면 언제나 사유를 붙인다(fields …).
  걸러둘 칸 대조 1 — dedup_decision skipped(probable_session) 요청을 기존 노트와 칸별로
  맞춘 것. 지금은 걸러진 요청은 결정만 비교해 ok 가 나온다(regression 사례).

E3b — 세 번째 대조(그래프)와 사건의 소요 시간 칸:
  간선 집합 — 실제 그래프가 노트의 투영(uses·about·claims·is_a·said·tagged·
  in_project·claim_of_project·supersedes)과 같아야 ok — 하나 빠지거나 생기면
  (가) 사유가 된다. 간선 하나를 빼는 변이는 여기서 빨갛게 끝난다.
  claim 봉인 — 대체는 새 노트가 다시 말한 (subject, predicate) 슬롯만 닫는다
  (wiki-2757/2758 모양: 옛 노트 사실 셋, 새 노트가 하나만 다시 말함). 엔진이 통째로
  닫으면(store.rs:2898) 그 어긋남은 (나) 「의도한 차이 — 부분 닫기」로 남는다 —
  통째 봉인으로 바꾸는 변이는 (나) 단언에서 빨갛게 끝난다.
  owner 거절 — 오너가 쓴 노트를 오너 아닌 호출이 대체하려 하면 양쪽 다 거절
  (owner.rs:64-80, 엔진 사건 owner_supersede_refused). 거절을 빼는 변이는 여기서
  빨갛게 끝난다. 운영 DB 에 owner 작성 노트가 없으니(2026-10-02 document.author
  확인) 이 갈래는 픽스처가 덮는다 — wiki-2877 기준의 「없으면 픽스처」다.
  소요 시간 — elapsed_total_s·elapsed_embedding_s·elapsed_db_s 가 사건에 항상 찍힌다.
  칸을 빼는 변이는 여기서 빨갛게 끝난다.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "src", ROOT / "agents" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from vault_note import split_frontmatter  # noqa: E402

from ohmyboring.adapters import vault as vault_notes  # noqa: E402
from ohmyboring.remember import pii as remember_pii  # noqa: E402
from ohmyboring.remember.graph import ClaimRow  # noqa: E402
from ohmyboring.remember.index import DiskSeams, NoteIndex  # noqa: E402
from ohmyboring.remember.shadow import (  # noqa: E402
    RELATES_TO_EXCLUDED,
    ShadowRequest,
    event_payload,
    extract_from_http_response,
    extract_from_mcp_text,
    run_shadow,
    wiki_stem,
)
from ohmyboring.result import Err, Ok  # noqa: E402

#: 그림자 시험용 PII 규칙 — vault/rules/pii.yaml 의 실제 규칙 모양을 본따 축소한 것.
PII_YAML = """
version: "1.0"
policy:
  exemption_marker: "<!-- pii-allow -->"
rules:
  - name: ipv4_any
    regex: '\\b(?:(?:25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)\\.){3}(?:25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)\\b'
    action: redact
    replacement: "[IP]"
    severity: warning
    reason: IPv4
  - name: name_after_author_label
    regex: '((?:작성자|보고자)\\s*[:：]\\s*)([김이박][가-힣]{1,2}(?![가-힣]))'
    action: redact
    replacement: "$1[NAME]"
    severity: warning
    reason: 라벨 뒤 실명
  - name: rrn
    regex: '\\b\\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\\d|3[01])-[1-4]\\d{6}\\b'
    action: block
    severity: critical
    reason: rrn
  - name: ticket
    regex: '\\b[A-Z]{2,5}-\\d+\\b'
    action: flag
    severity: warning
    reason: ticket
"""

#: 로컬 오버레이에만 있는 규칙 — 규칙 파일이 갈라진 운영 사례(엔진은 가림, 그림자는 모름).
LOCAL_ONLY_YAML = """
version: "1.0"
rules:
  - name: name_after_author_label
    regex: '((?:작성자|보고자)\\s*[:：]\\s*)([김이박][가-힣]{1,2}(?![가-힣]))'
    action: redact
    replacement: "$1[NAME]"
    severity: warning
    reason: 라벨 뒤 실명
"""


def _scanner(yaml_text: str = PII_YAML):
    import tempfile

    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name) / "pii.yaml"
    base.write_text(yaml_text, encoding="utf-8")
    match remember_pii.load(base, Path(tmp.name) / "missing.yaml"):
        case Ok(scanner):
            return tmp, scanner
        case other:
            raise AssertionError(f"fixture scanner: {other!r}")


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

SKIPPED_ANSWER = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {"content": [{"type": "text", "text": "skipped — duplicate of /vault/wiki/wiki-2769.md"}]},
}


def _read_note(files: dict[str, str]):
    """note_id -> text | None — 문 컨테이너의 /vault(ro) 읽기를 세는 가짜."""

    def read(vault_dir: str, note_id: str) -> str | None:
        assert vault_dir == "/vault"
        return files.get(note_id)

    return read


def _no_nearest(text: str, exclude: str | None):
    return Ok(None)


def _skipped_answer(note_id: str):
    """걸러진 답 — 이름을 밝히는 경로는 볼트에 있는 기존 노트여야 양쪽이 맞는다."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": f"skipped — duplicate of /vault/wiki/{note_id}.md"}]},
    }


def _run(files: dict[str, str], answer, **options):
    """그림자 한 번 — options: arguments·route·scanner·nearest·is_owner·status·graph."""
    body = json.dumps(answer, ensure_ascii=False).encode()
    return run_shadow(
        ShadowRequest(
            route=options.get("route", "mcp"),
            arguments=options.get("arguments", ARGUMENTS),
            engine_status=options.get("status", 200),
            engine_body=body,
            vault_dir="/vault",
            read_note=_read_note(files),
            split_frontmatter=split_frontmatter,
            list_notes=lambda: sorted(files),
            pii_scanner=options.get("scanner"),
            is_owner=options.get("is_owner", False),
            nearest_document=options.get("nearest", _no_nearest),
            read_graph=options.get("graph"),
        )
    )


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
                self.assertEqual((extracted.duplicate, extracted.note_id), (False, "wiki-2801"))
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
                self.assertTrue(extracted.duplicate)
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_http_refusal_is_an_error_not_a_note(self):
        match extract_from_http_response(400, b'{"error":"missing argument: title"}'):
            case Err(reason):
                self.assertIn("400", reason)
            case other:
                self.fail(f"expected Err, got {other!r}")


class StoredFieldCompareTests(unittest.TestCase):
    def test_matching_note_is_ok_field_by_field(self):
        event = _run({"wiki-2801": ENGINE_NOTE}, MCP_ANSWER)
        self.assertEqual(event.status, "ok")
        self.assertEqual(event.fields, ())
        self.assertEqual(event.source_path, "/vault/wiki/wiki-2801.md")
        self.assertEqual(event.omb_session_id, "sess-1")
        self.assertFalse(event.duplicate)
        self.assertEqual(event.decision, "stored")
        self.assertEqual(event.engine_decision, "stored:/vault/wiki/wiki-2801.md")

    def test_engine_date_and_id_are_taken_as_written(self):
        # date/id 대조는 번호·날짜 대조 이후 과제 — 파이썬 렌더는 엔진 것을 그대로 받아 넣어
        # 이 칸은 어긋날 수가 없다. 엔진 날짜를 어제로 바꿔도 ok 가 나와야 한다.
        note = ENGINE_NOTE.replace("date: 2026-10-01", "date: 2026-09-30")
        event = _run({"wiki-2801": note}, MCP_ANSWER)
        self.assertEqual(event.status, "ok")
        self.assertNotIn("date", event.fields)
        self.assertNotIn("id", event.fields)

    def test_engine_filled_relates_to_is_excluded_with_reason(self):
        # relates_to 는 엔진이 쓴 뒤 그래프 투영이 다시 쓴다 — 불일치로 안 잡힌다.
        event = _run({"wiki-2801": ENGINE_NOTE}, MCP_ANSWER)
        self.assertEqual(event.status, "ok")

    def test_a_changed_title_is_a_mismatch_named_by_field_with_reason(self):
        note = ENGINE_NOTE.replace("title: 배포 절차 정리", "title: 배포 절차 정리 (고침)")
        event = _run({"wiki-2801": note}, MCP_ANSWER)
        self.assertEqual(event.status, "mismatch")
        self.assertEqual(event.fields, ("title",))
        self.assertEqual(event.reason, "fields title")

    def test_a_changed_body_is_a_mismatch_named_by_field(self):
        note = ENGINE_NOTE.replace("배포는 make deploy 로 한다.", "배포는 수동으로 한다.")
        event = _run({"wiki-2801": note}, MCP_ANSWER)
        self.assertEqual(event.fields, ("body",))
        self.assertEqual(event.reason, "fields body")

    def test_missing_note_in_vault_is_an_error(self):
        event = _run({}, MCP_ANSWER)
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
                list_notes=lambda: ["wiki-2801"],
                pii_scanner=None,
                nearest_document=_no_nearest,
            )
        )
        self.assertEqual(event.status, "error")
        self.assertIn("400", event.reason)

    def test_http_route_compares_the_source_path_note(self):
        answer = {
            "source_path": "/vault/wiki/wiki-2801.md",
            "wiki_id": "wiki-2801",
            "duplicate": None,
            "supersedes": 0,
            "unknown": 0,
        }
        event = _run({"wiki-2801": ENGINE_NOTE}, answer, route="remember")
        self.assertEqual(event.status, "ok")

    def test_event_payload_carries_no_note_body(self):
        event = _run({"wiki-2801": ENGINE_NOTE}, MCP_ANSWER)
        from ohmyboring.remember.shadow import event_payload

        payload = event_payload(event)
        self.assertEqual(payload["fields"], [])
        self.assertEqual(payload["relates_to"], RELATES_TO_EXCLUDED)
        self.assertEqual(payload["source_path"], "/vault/wiki/wiki-2801.md")
        self.assertEqual(payload["omb_session_id"], "sess-1")
        self.assertEqual(payload["decision"], "stored")
        self.assertNotIn("배포는 make deploy", json.dumps(payload, ensure_ascii=False))


class PiiDecisionTests(unittest.TestCase):
    """PII 게이트 결정 — 표시(가림)와 차단, 그리고 기존 어긋남 원인의 사유 재현."""

    def test_masked_engine_note_matches_gated_python_render(self):
        # 운영 사례: 요청에는 원문이 있고 엔진 노트에는 가린 글과 pii-flag 가 있다.
        # 게이트를 이식했으니 파이썬 렌더도 같은 가림을 낸다 — ok.
        tmp, scanner = _scanner()
        self.addCleanup(tmp.cleanup)
        args = dict(ARGUMENTS)
        args["title"] = "작성자: 김철수 결재"
        args["body"] = "배포는 10.1.2.3 에서 한다. 관련 FDS-12345."
        note = (
            ENGINE_NOTE.replace("title: 배포 절차 정리", 'title: "작성자: [NAME] 결재"')
            .replace("배포는 make deploy 로 한다.", "배포는 [IP] 에서 한다. 관련 FDS-12345.")
            .replace("- deploy\n", "- deploy\n- pii-flag\n")
        )
        event = _run({"wiki-2801": note}, MCP_ANSWER, arguments=args, scanner=scanner)
        self.assertEqual(event.status, "ok", event.reason)

    def test_no_scanner_leaves_pii_mismatch_with_gate_missing_reason(self):
        # 기존 어긋남 원인 재현 1·2 — 그림자에 규칙 파일이 없으면 사유가 남는다.
        args = dict(ARGUMENTS)
        args["title"] = "작성자: 김철수 결재"
        args["body"] = "배포는 make deploy 로 한다. 관련 FDS-12345."
        note = (
            ENGINE_NOTE.replace("title: 배포 절차 정리", 'title: "작성자: [NAME] 결재"')
            .replace("배포는 make deploy 로 한다.", "배포는 make deploy 로 한다. 관련 FDS-12345.")
            .replace("- deploy\n", "- deploy\n- pii-flag\n")
        )
        event = _run({"wiki-2801": note}, MCP_ANSWER, arguments=args)
        self.assertEqual(event.status, "mismatch")
        self.assertIn("title", event.fields)
        self.assertIn("pii-gate-missing", event.reason)

    def test_rules_drift_is_named_even_when_tags_agree(self):
        # 기존 어긋남 원인 재현 3 — 태그는 양쪽 같고(사유가 비어 있던 3건) 칸만 갈릴 때도
        # 사유가 남는다: 엔진의 로컬 오버레이에만 이름 가림이 있으면 그림자 규칙으로는
        # 차이를 설명할 수 없어 pii-flag-drift 로 남는다.
        without_name = PII_YAML.replace(
            """  - name: name_after_author_label
    regex: '((?:작성자|보고자)\\s*[:：]\\s*)([김이박][가-힣]{1,2}(?![가-힣]))'
    action: redact
    replacement: "$1[NAME]"
    severity: warning
    reason: 라벨 뒤 실명
""",
            "",
        )
        tmp, scanner = _scanner(without_name)
        self.addCleanup(tmp.cleanup)
        args = dict(ARGUMENTS)
        args["title"] = "작성자: 김철수 결재"
        args["body"] = "배포는 make deploy 로 한다. 관련 FDS-12345."
        note = (
            ENGINE_NOTE.replace("title: 배포 절차 정리", 'title: "작성자: [NAME] 결재"')
            .replace("배포는 make deploy 로 한다.", "배포는 make deploy 로 한다. 관련 FDS-12345.")
            .replace("- deploy\n", "- deploy\n- pii-flag\n")
        )
        event = _run({"wiki-2801": note}, MCP_ANSWER, arguments=args, scanner=scanner)
        self.assertEqual(event.status, "mismatch")
        self.assertEqual(event.fields, ("title",))
        self.assertIn("pii-flag-drift", event.reason)

    def test_pii_block_is_a_decision_not_an_error(self):
        # 차단은 결정이다: 엔진 -32603, 파이썬 게이트도 막으면 ok.
        tmp, scanner = _scanner()
        self.addCleanup(tmp.cleanup)
        args = dict(ARGUMENTS)
        args["body"] = "주민번호 900101-1234567"
        blocked = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32603,
                "message": "PII gate blocked by rule 'rrn' (critical): rrn — matched sensitive text omitted",
            },
        }
        event = _run({"wiki-2801": ENGINE_NOTE}, blocked, arguments=args, scanner=scanner)
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.decision, "blocked:rrn")
        self.assertEqual(event.engine_decision, "blocked:rrn")

    def test_pii_block_mismatch_when_python_would_store(self):
        # 틀린 구현(게이트 없음): 엔진은 막았는데 파이썬은 저장 — 결정 어긋남.
        args = dict(ARGUMENTS)
        args["body"] = "주민번호 900101-1234567"
        blocked = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "PII gate blocked by rule 'rrn'"},
        }
        event = _run({}, blocked, arguments=args)
        self.assertEqual(event.status, "mismatch")
        self.assertIn("decision engine=blocked", event.reason)
        self.assertIn("python=stored", event.reason)

    def test_pii_block_over_http_error_status(self):
        tmp, scanner = _scanner()
        self.addCleanup(tmp.cleanup)
        args = dict(ARGUMENTS)
        args["body"] = "주민번호 900101-1234567"
        event = _run(
            {"wiki-2801": ENGINE_NOTE},
            {"error": "PII gate blocked by rule 'rrn' (critical): rrn — omitted"},
            arguments=args,
            route="remember",
            scanner=scanner,
            status=500,
        )
        self.assertEqual(event.status, "ok", event.reason)


class DedupBranchTests(unittest.TestCase):
    """중복 문 갈래 다섯과 새 노트 — 각 갈래가 결정 대조에 잡히는지 본다."""

    def _session_note(self, wiki_id: str, title: str, body: str, session: str) -> str:
        return (
            ENGINE_NOTE.replace("wiki-2801", wiki_id)
            .replace("title: 배포 절차 정리", f"title: {title}")
            .replace("배포는 make deploy 로 한다.", body)
            .replace("sess-1", session)
        )

    def test_same_session_skip_is_decision_only(self):
        # 기존 노트와 요청이 칸으로는 완전히 다르다 — 걸러진 요청은 결정만 비교해 ok.
        existing = self._session_note("wiki-2769", "아무 상관 없는 제목", "아무 상관 없는 본문.", "sess-1")
        event = _run({"wiki-2769": existing}, _skipped_answer("wiki-2769"))
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.decision, "skipped:/vault/wiki/wiki-2769.md")
        self.assertEqual(event.engine_decision, "skipped:/vault/wiki/wiki-2769.md")
        self.assertEqual(event.branch, "same_session")

    def test_probable_session_skip_matches(self):
        # 세션 추정: 세션 id 는 다르지만 의미 칸과 제목이 문지방을 넘는다.
        existing = self._session_note("wiki-2769", "배포 절차", "배포는 수동으로 한다.", "sess-2")
        event = _run({"wiki-2769": existing}, _skipped_answer("wiki-2769"))
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.branch, "probable_session")

    def test_exact_title_skip_matches(self):
        existing = self._session_note("wiki-2769", "배포 절차 정리", "전혀 다른 내용의 본문.", "sess-9")
        args = dict(ARGUMENTS)
        args["omb_session_id"] = None
        event = _run({"wiki-2769": existing}, _skipped_answer("wiki-2769"), arguments=args)
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.branch, "exact_title")

    def test_embedding_skip_matches(self):
        # 파일 갈래는 안 걸리고 임베딩 갈래가 잡는다 — 엔진 답도 그 경로로 걸러짐.
        seen: list[str] = []

        def nearest(text: str, exclude: str | None):
            seen.append(text)
            return Ok("/vault/wiki/wiki-2600.md")

        unrelated = self._session_note("wiki-2700", "회의 일정", "주간 회의가 목요일에 있다.", "sess-9")
        event = _run({"wiki-2700": unrelated}, _skipped_answer("wiki-2600"), nearest=nearest)
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.branch, "embedding")
        self.assertEqual(seen, ["배포 절차 정리\n\n배포는 make deploy 로 한다."])

    def test_embedding_unchecked_is_named_when_engine_skipped(self):
        # (다) 사례 — 그림자가 임베딩 갈래를 못 볼 때(프로브 없음): 엔진이 걸러뒀다면
        # 결정 어긋남 사유에 「임베딩 미확인」이 남는다.
        event = _run({}, _skipped_answer("wiki-2769"), nearest=None)
        self.assertEqual(event.status, "mismatch")
        self.assertIn("decision engine=skipped", event.reason)
        self.assertIn("python=stored", event.reason)
        self.assertIn("embedding-unchecked", event.branch)

    def test_score_override_supersede_compares_the_new_note(self):
        # 같은 세션에 옛 노트 — 들어오는 노트가 훨씬 자세하면 엔진은 대체로 저장한다.
        old = (
            ENGINE_NOTE.replace("wiki-2801", "wiki-2700")
            .replace("title: 배포 절차 정리", "title: 배포")
            .replace("배포는 make deploy 로 한다.", "배포는 한다.")
            .replace(
                "claims:\n- subject: 배포\n  predicate: 절차\n  value: make deploy\n  kind: fact\n  confidence: certain\n",
                "claims: []\n",
            )
            .replace("tools:\n- kubectl\n", "tools: []\n")
            .replace("concepts:\n- 배포\n", "concepts: []\n")
        )
        supersedes_answer = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "remembered → wiki/wiki-2801.md (supersedes wiki/wiki-2700.md) · chunks 3 — recallable now",
                    }
                ]
            },
        }
        files = {"wiki-2801": ENGINE_NOTE, "wiki-2700": old}
        event = _run(files, supersedes_answer)
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.decision, "superseded:/vault/wiki/wiki-2700.md")
        self.assertEqual(event.engine_decision, "superseded:/vault/wiki/wiki-2700.md")
        self.assertEqual(event.branch, "same_session")
        self.assertEqual(event.fields, (), "칸 대조는 새 노트(2801)와 — ok")

    def test_same_session_not_richer_is_a_skip(self):
        old = self._session_note("wiki-2700", "배포 절차 정리", "배포는 make deploy 로 한다.", "sess-1")
        event = _run({"wiki-2700": old}, _skipped_answer("wiki-2700"))
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.branch, "same_session")
        self.assertEqual(event.decision, "skipped:/vault/wiki/wiki-2700.md")

    def test_new_note_when_nothing_matches(self):
        unrelated = self._session_note("wiki-2700", "회의 일정", "주간 회의가 목요일에 있다.", "sess-9")
        event = _run({"wiki-2700": unrelated, "wiki-2801": ENGINE_NOTE}, MCP_ANSWER)
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.decision, "stored")
        self.assertIsNone(event.branch)

    def test_engine_note_is_excluded_from_its_own_candidates(self):
        # 엔진이 방금 쓴 노트(이미 볼트에 있다)를 후보에서 빼지 않으면 자기 자신과
        # 중복으로 걸러 거짓 어긋남이 난다 — stored 답의 추출 경로만 뺀다.
        event = _run({"wiki-2801": ENGINE_NOTE}, MCP_ANSWER)
        self.assertEqual(event.status, "ok", event.reason)

    def test_decision_mismatch_names_both_sides(self):
        # 엔진은 wiki-2769 로 걸러뒀는데 파이썬이 같은 세션 최신을 wiki-2800 으로 본다.
        newer = self._session_note("wiki-2800", "아무 제목", "아무 본문.", "sess-1")
        event = _run({"wiki-2800": newer}, SKIPPED_ANSWER)
        self.assertEqual(event.status, "mismatch")
        self.assertIn("decision engine=skipped:/vault/wiki/wiki-2769.md", event.reason)
        self.assertIn("python=skipped:/vault/wiki/wiki-2800.md", event.reason)
        payload_reason = event.reason
        self.assertTrue(payload_reason.startswith("decision "))

    def test_owner_author_without_token_is_a_refusal_decision(self):
        args = dict(ARGUMENTS)
        args["author"] = "owner"
        refused = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32602,
                "message": "owner (as author or judge) needs the owner door token in x-boring-owner-token",
            },
        }
        event = _run({"wiki-2801": ENGINE_NOTE}, refused, arguments=args)
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.decision, "refused:-32602 owner door token")
        self.assertIn("refused:owner (as author or judge)", event.engine_decision)

    def test_owner_with_token_proceeds_to_store(self):
        args = dict(ARGUMENTS)
        args["author"] = "owner"
        note = ENGINE_NOTE.replace("author: agent:claude", "author: owner")
        event = _run({"wiki-2801": note}, MCP_ANSWER, arguments=args, is_owner=True)
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.decision, "stored")

    def test_embedding_dist_check_uses_engine_max(self):
        # 변이 감시 — 임베딩 상한이 엔진 것(0.07)인지: 프로브가 None 을 돌려주는
        # 지금은 직접 값을 못 본다. 최소한 상수가 이 모듈에서 함께 오는지로 묶어 둔다.
        from ohmyboring.remember import dedup

        self.assertEqual(dedup.DUPLICATE_MAX_DIST, 0.07)


#: E3b 그림자 시험용 엔진 노트 — 투영 부류가 고르게 든 노트. 그것을 낳은 요청은
#: GRAPH_ARGUMENTS 이고 MCP 답은 GRAPH_MCP_ANSWER.
GRAPH_NOTE = """---
id: wiki-2810
title: 배포 절차 정리
kind: note
origin: personal
project: kb-agent
date: 2026-10-02
tags:
- repo/kb-agent
- deploy
tools:
- kubectl
- Docker Compose
concepts:
- deploy pipeline
claims:
- subject: 배포
  predicate: 절차
  value: make deploy
  kind: fact
  confidence: certain
- subject: 릴리스
  predicate: 주기
  value: 매주 목요일
  kind: decision
  confidence: likely
  said_by: owner
relates_to: []
sources: []
omb_session_id: sess-g1
author: agent:claude
---

배포는 make deploy 로 한다.
"""

GRAPH_ARGUMENTS = {
    "title": "배포 절차 정리",
    "body": "배포는 make deploy 로 한다.",
    "origin": "personal",
    "repo": "kb-agent",
    "tags": ["deploy"],
    "tools": ["kubectl", "Docker Compose"],
    "concepts": ["deploy pipeline"],
    "claims": [
        {
            "subject": "배포",
            "predicate": "절차",
            "value": "make deploy",
            "kind": "fact",
            "confidence": "certain",
        },
        {
            "subject": "릴리스",
            "predicate": "주기",
            "value": "매주 목요일",
            "kind": "decision",
            "confidence": "likely",
            "said_by": "owner",
        },
    ],
    "omb_session_id": "sess-g1",
    "author": "agent:claude",
}

GRAPH_MCP_ANSWER = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "content": [
            {
                "type": "text",
                "text": "remembered → wiki/wiki-2810.md · chunks 2 · graph(tools 2 concepts 1 claims 2) — recallable now",
            }
        ]
    },
}

GRAPH_NEW_PATH = "/vault/wiki/wiki-2810.md"

#: GRAPH_NOTE 의 투영 — 그래프 대조의 목표 간선 집합. 그래프 모듈을 다시 부르지 않고
#: 여기에 글자로 박아 두니, 투영이 간선 하나를 빼먹는 변이는 어긋남으로 잡힌다.
GRAPH_EXPECTED_EDGES = frozenset(
    {
        ("doc:/vault/wiki/wiki-2810.md", "in_project", "project:kb-agent"),
        ("doc:/vault/wiki/wiki-2810.md", "tagged", "topic:repo/kb-agent"),
        ("doc:/vault/wiki/wiki-2810.md", "tagged", "topic:deploy"),
        ("doc:/vault/wiki/wiki-2810.md", "uses", "tool:kubectl"),
        ("doc:/vault/wiki/wiki-2810.md", "uses", "tool:dockercompose"),
        ("doc:/vault/wiki/wiki-2810.md", "about", "concept:deploypipeline"),
        ("doc:/vault/wiki/wiki-2810.md", "claims", "claim:배포:절차"),
        ("doc:/vault/wiki/wiki-2810.md", "claims", "claim:릴리스:주기"),
        ("claim:릴리스:주기", "is_a", "decision:릴리스:주기"),
        ("claim:배포:절차", "claim_of_project", "project:kb-agent"),
        ("claim:릴리스:주기", "claim_of_project", "project:kb-agent"),
        ("person:owner", "said", "doc:/vault/wiki/wiki-2810.md"),
        ("person:owner", "said", "claim:릴리스:주기"),
    }
)

#: 새 노트 두 claim 행(DB 가 canon 슬롯으로 쓴 모양) — 둘 다 살아 있어야 한다.
GRAPH_NEW_CLAIMS = (
    ClaimRow(GRAPH_NEW_PATH, "배포", "절차", "fact", False),
    ClaimRow(GRAPH_NEW_PATH, "릴리스", "주기", "decision", False),
)

#: wiki-2757/2758 모양의 옛 노트 — 사실 셋, 새 노트(GRAPH_NOTE)는 그중 (배포,절차)만
#: 다시 말한다. 대체 목표: 그 슬롯 하나만 닫히고 둘은 산다.
OLD_NOTE = """---
id: wiki-2757
title: 배포 절차 초안
kind: note
origin: personal
project: kb-agent
date: 2026-09-28
tags:
- repo/kb-agent
tools: []
concepts: []
claims:
- subject: 배포
  predicate: 절차
  value: 수동으로 한다
  kind: fact
  confidence: certain
- subject: 모니터링
  predicate: 대상
  value: 전 서비스
  kind: fact
  confidence: certain
- subject: 알림
  predicate: 채널
  value: 슬랙
  kind: fact
  confidence: certain
relates_to: []
sources: []
omb_session_id: sess-old
author: agent:claude
---

배포는 수동으로 한다.
"""

OWNER_OLD_NOTE = OLD_NOTE.replace("author: agent:claude", "author: owner")

OLD_PATH = "/vault/wiki/wiki-2757.md"
OLD_CLAIMS_ALL_SEALED = (
    ClaimRow(OLD_PATH, "배포", "절차", "fact", True),
    ClaimRow(OLD_PATH, "모니터링", "대상", "fact", True),
    ClaimRow(OLD_PATH, "알림", "채널", "fact", True),
)


def _supersede_answer():
    """교정(인자 supersedes)이 성공한 엔진 답 — 답 본문에는 대체 접미사만 있다."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": "remembered → wiki/wiki-2810.md · chunks 2 · graph(tools 2 concepts 1 claims 2) — recallable now · supersedes linked 1",
                }
            ]
        },
    }


def _snapshot(edges, claims, documents=None):
    from ohmyboring.remember.graph import GraphSnapshot

    return GraphSnapshot(
        edges=frozenset(edges),
        documents=frozenset(documents if documents is not None else {GRAPH_NEW_PATH}),
        claims=tuple(claims),
    )


class GraphCompareTests(unittest.TestCase):
    """E3b — 간선 집합 대조: 실제 그래프가 노트의 투영과 같아야 ok, 하나 빠지거나
    생기면 (가) 사유. 간선 하나를 빼는 변이는 edges ok 단언에서 빨갛게 끝난다."""

    def test_matching_graph_is_ok(self):
        event = _run(
            {"wiki-2810": GRAPH_NOTE},
            GRAPH_MCP_ANSWER,
            arguments=GRAPH_ARGUMENTS,
            graph=lambda paths: _snapshot(GRAPH_EXPECTED_EDGES, GRAPH_NEW_CLAIMS),
        )
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.edges, "ok")
        self.assertEqual(event.seal, "ok")
        self.assertIsNone(event.reason)

    def test_unrelated_edges_are_out_of_scope(self):
        # 함께 조회된 다른 노트의 잔여 간선·비투영 부류는 이 노트의 어긋남이 아니다.
        stray = set(GRAPH_EXPECTED_EDGES)
        stray.add(("doc:/vault/wiki/wiki-2700.md", "uses", "tool:stray"))
        stray.add(("doc:/vault/wiki/wiki-2700.md", "handed", "doc:/vault/wiki/wiki-2810.md"))
        stray.add(("doc:/vault/wiki/wiki-2700.md", "tagged", "topic:무관"))
        event = _run(
            {"wiki-2810": GRAPH_NOTE},
            GRAPH_MCP_ANSWER,
            arguments=GRAPH_ARGUMENTS,
            graph=lambda paths: _snapshot(stray, GRAPH_NEW_CLAIMS),
        )
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.edges, "ok")

    def test_a_missing_edge_is_a_mismatch_named_by_kind(self):
        broken = set(GRAPH_EXPECTED_EDGES)
        broken.remove(("doc:/vault/wiki/wiki-2810.md", "uses", "tool:kubectl"))
        event = _run(
            {"wiki-2810": GRAPH_NOTE},
            GRAPH_MCP_ANSWER,
            arguments=GRAPH_ARGUMENTS,
            graph=lambda paths: _snapshot(broken, GRAPH_NEW_CLAIMS),
        )
        self.assertEqual(event.status, "mismatch")
        self.assertEqual(event.edges, "missing=uses:1")
        self.assertIn("edges missing=uses:1 (가)", event.reason)

    def test_an_extra_edge_is_a_mismatch(self):
        # 투영이 is_a 를 빼먹는 변이의 모양 — 실제 그래프에만 is_a 가 있으면 extra 로 남는다.
        bloated = set(GRAPH_EXPECTED_EDGES)
        bloated.add(("doc:/vault/wiki/wiki-2810.md", "uses", "tool:helm"))
        event = _run(
            {"wiki-2810": GRAPH_NOTE},
            GRAPH_MCP_ANSWER,
            arguments=GRAPH_ARGUMENTS,
            graph=lambda paths: _snapshot(bloated, GRAPH_NEW_CLAIMS),
        )
        self.assertEqual(event.status, "mismatch")
        self.assertEqual(event.edges, "extra=uses:1")
        self.assertIn("edges extra=uses:1 (가)", event.reason)

    def test_unknown_supersedes_target_writes_no_edge(self):
        # 문서 행이 없는 교정 대상은 엔진이 unknown 으로 세고 간선을 안 쓴다 — 그림자도
        # 목표에서 빼야 어긋남이 안 난다.
        args = dict(GRAPH_ARGUMENTS, supersedes=["wiki/wiki-2757.md", "wiki/wiki-2760.md"])
        edges = set(GRAPH_EXPECTED_EDGES)
        edges.add(("doc:/vault/wiki/wiki-2810.md", "supersedes", "doc:/vault/wiki/wiki-2757.md"))
        event = _run(
            {"wiki-2810": GRAPH_NOTE, "wiki-2757": OLD_NOTE},
            _supersede_answer(),
            arguments=args,
            graph=lambda paths: _snapshot(
                edges,
                GRAPH_NEW_CLAIMS,
                documents={GRAPH_NEW_PATH, OLD_PATH},
            ),
        )
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.edges, "ok")

    def test_graph_read_failure_leaves_an_unchecked_reason(self):
        # 조회 자체를 못 하면 (다) 사유만 남긴다 — 없는 걸 본 것처럼 속이지 않는다.
        event = _run(
            {"wiki-2810": GRAPH_NOTE},
            GRAPH_MCP_ANSWER,
            arguments=GRAPH_ARGUMENTS,
            graph=lambda paths: None,
        )
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.edges, "unchecked")
        self.assertEqual(event.seal, "unchecked")
        self.assertIn("graph-unchecked (다)", event.reason)

    def test_no_reader_is_unchecked_too(self):
        # read_graph 를 싣지 않은 환경(옛 문 wiring·단위 시험) — 칸만 unchecked 로 남긴다.
        event = _run({"wiki-2810": GRAPH_NOTE}, GRAPH_MCP_ANSWER, arguments=GRAPH_ARGUMENTS)
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.edges, "unchecked")
        self.assertEqual(event.seal, "unchecked")


class SealCompareTests(unittest.TestCase):
    """E3b — 대체 봉인 대조: 새 노트가 다시 말한 슬롯만 닫는 게 목표(부분 닫기).
    엔진이 통째로 닫으면 그 어긋남은 (나) 「의도한 차이 — 부분 닫기」다(wiki-2855).
    통째 봉인으로 바꾸는 변이는 (나) 단언에서 빨갛게 끝난다."""

    def _supersede_run(self, claims, files=None, **options):
        args = dict(GRAPH_ARGUMENTS, supersedes=["wiki/wiki-2757.md"])
        files = files or {"wiki-2810": GRAPH_NOTE, "wiki-2757": OLD_NOTE}
        edges = set(GRAPH_EXPECTED_EDGES)
        edges.add(("doc:/vault/wiki/wiki-2810.md", "supersedes", f"doc:{OLD_PATH}"))
        return _run(
            files,
            _supersede_answer(),
            arguments=options.pop("arguments", args),
            graph=options.pop("graph", lambda paths: _snapshot(edges, claims, {GRAPH_NEW_PATH, OLD_PATH})),
            **options,
        )

    def test_wholesale_seal_is_the_intended_difference_reason_nah(self):
        # 엔진이 옛 노트의 현재 claim 을 통째로 닫았다(store.rs:2898). 목표는 다시 말한
        # (배포,절차) 하나뿐 — 모니터링·알림 둘은 산다. 어긋남 둘은 사유 (나)로 남는다.
        claims = OLD_CLAIMS_ALL_SEALED + GRAPH_NEW_CLAIMS
        event = self._supersede_run(claims)
        self.assertEqual(event.status, "mismatch")
        self.assertEqual(event.edges, "ok", "supersedes 간선은 목표에 있고 실제에도 있다")
        self.assertEqual(event.seal, "engine-only=2")
        self.assertIn("seal engine-only=2 (나 intended-diff partial-close)", event.reason)
        self.assertNotIn("python-only", event.reason)

    def test_partial_close_match_is_ok(self):
        # 엔진이 (가능성은 희박하지만) 부분 닫기를 했다면 어긋남이 없다 — 대조 자체의 시험.
        claims = (
            ClaimRow(OLD_PATH, "배포", "절차", "fact", True),
            ClaimRow(OLD_PATH, "모니터링", "대상", "fact", False),
            ClaimRow(OLD_PATH, "알림", "채널", "fact", False),
        ) + GRAPH_NEW_CLAIMS
        event = self._supersede_run(claims)
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.seal, "ok")

    def test_engine_leaving_a_restated_slot_open_is_python_only(self):
        # 다시 말한 슬롯마저 엔진이 살린 것 — 목표보다 덜 닫은 것이라 사유 (가)다.
        claims = (
            ClaimRow(OLD_PATH, "배포", "절차", "fact", False),
            ClaimRow(OLD_PATH, "모니터링", "대상", "fact", False),
            ClaimRow(OLD_PATH, "알림", "채널", "fact", False),
        ) + GRAPH_NEW_CLAIMS
        event = self._supersede_run(claims)
        self.assertEqual(event.status, "mismatch")
        self.assertEqual(event.seal, "python-only=1")
        self.assertIn("seal python-only=1 (가)", event.reason)

    def test_fresh_note_rows_are_expected_alive(self):
        # 대체가 아닌 저장 — 새 노트 행이 살아 있으면 봉인 대조는 ok.
        event = _run(
            {"wiki-2810": GRAPH_NOTE},
            GRAPH_MCP_ANSWER,
            arguments=GRAPH_ARGUMENTS,
            graph=lambda paths: _snapshot(GRAPH_EXPECTED_EDGES, GRAPH_NEW_CLAIMS),
        )
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.seal, "ok")


class OwnerSupersedeRefusalTests(unittest.TestCase):
    """E3b — 오너가 쓴 노트를 오너 아닌 호출이 대체하려 하면 양쪽 다 거절한다
    (owner.rs:64-80; 엔진 사건 owner_supersede_refused). 운영 DB 에 owner 작성 노트가
    없으니(2026-10-02 확인) 이 갈래는 픽스처가 덮는다 — wiki-2877 의 「없으면 픽스처」."""

    def test_non_owner_superseding_an_owner_note_is_refused_on_both_sides(self):
        args = dict(GRAPH_ARGUMENTS, supersedes=["wiki/wiki-2757.md"])
        refused = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32602,
                "message": "only the owner may supersede an owner-written note: /vault/wiki/wiki-2757.md",
            },
        }
        files = {"wiki-2810": GRAPH_NOTE, "wiki-2757": OWNER_OLD_NOTE}
        event = _run(files, refused, arguments=args)
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(
            event.decision,
            "refused:only the owner may supersede an owner-written note",
        )
        self.assertEqual(event.engine_decision, event.decision)

    def test_owner_may_supersede_an_owner_note(self):
        # 오너 호출은 오너 노트를 대체할 수 있다 — 게이트를 지나 저장으로 떨어진다.
        args = dict(GRAPH_ARGUMENTS, supersedes=["wiki/wiki-2757.md"])
        claims = (
            ClaimRow(OLD_PATH, "배포", "절차", "fact", True),  # 다시 말한 슬롯만 닫힘
            ClaimRow(OLD_PATH, "모니터링", "대상", "fact", False),
            ClaimRow(OLD_PATH, "알림", "채널", "fact", False),
        ) + GRAPH_NEW_CLAIMS
        edges = set(GRAPH_EXPECTED_EDGES)
        edges.add(("doc:/vault/wiki/wiki-2810.md", "supersedes", f"doc:{OLD_PATH}"))
        files = {"wiki-2810": GRAPH_NOTE, "wiki-2757": OWNER_OLD_NOTE}
        event = _run(
            files,
            _supersede_answer(),
            arguments=args,
            is_owner=True,
            graph=lambda paths: _snapshot(edges, claims, {GRAPH_NEW_PATH, OLD_PATH}),
        )
        self.assertEqual(event.status, "ok", event.reason)
        self.assertEqual(event.decision, "stored")
        self.assertEqual(event.seal, "ok")

    def test_refusal_direction_names_engine_and_python(self):
        # 엔진은 저장했는데 그림자가 거절 — document.author 와 볼트 author 가 갈린 드리프트.
        # 사유는 결정 어긋남으로 남고 어느 쪽이 거절인지 적힌다.
        args = dict(GRAPH_ARGUMENTS, supersedes=["wiki/wiki-2757.md"])
        files = {"wiki-2810": GRAPH_NOTE, "wiki-2757": OWNER_OLD_NOTE}
        event = _run(files, GRAPH_MCP_ANSWER, arguments=args)
        self.assertEqual(event.status, "mismatch")
        self.assertIn("decision engine=stored", event.reason)
        self.assertIn("python=refused", event.reason)


class ElapsedFieldsTests(unittest.TestCase):
    """E3b — 사건의 소요 시간 칸: 전체·임베딩·DB 읽기가 항상 찍힌다. 칸을 빼는 변이는
    여기서 빨갛게 끝난다."""

    def test_elapsed_fields_are_recorded(self):
        def nearest(text: str, exclude: str | None):
            time.sleep(0.02)
            return Ok(None)

        def read_graph(paths):
            time.sleep(0.01)
            return _snapshot(GRAPH_EXPECTED_EDGES, GRAPH_NEW_CLAIMS)

        event = _run(
            {"wiki-2810": GRAPH_NOTE},
            GRAPH_MCP_ANSWER,
            arguments=GRAPH_ARGUMENTS,
            nearest=nearest,
            graph=read_graph,
        )
        self.assertEqual(event.status, "ok", event.reason)
        self.assertGreaterEqual(event.elapsed_embedding_s, 0.015)
        self.assertGreaterEqual(event.elapsed_db_s, 0.005)
        self.assertGreaterEqual(event.elapsed_total_s, event.elapsed_embedding_s + event.elapsed_db_s)

    def test_payload_carries_elapsed_and_graph_fields(self):
        from ohmyboring.remember.shadow import event_payload

        event = _run(
            {"wiki-2810": GRAPH_NOTE},
            GRAPH_MCP_ANSWER,
            arguments=GRAPH_ARGUMENTS,
            graph=lambda paths: _snapshot(GRAPH_EXPECTED_EDGES, GRAPH_NEW_CLAIMS),
        )
        payload = event_payload(event)
        for key in (
            "elapsed_total_s",
            "elapsed_embedding_s",
            "elapsed_db_s",
            "elapsed_vault_s",
            "elapsed_parse_s",
            "edges",
            "seal",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["edges"], "ok")
        self.assertEqual(payload["seal"], "ok")


class NoteIndexShadowTests(unittest.TestCase):
    """E3b-2 — 문 안의 노트 색인이 중복 문의 볼트 스캔을 대신하는 계약.

    같은 픽스처 볼트에서 색인 경로와 디스크 훑기 경로의 결정이 같고, 사건에는 파싱
    칸(elapsed_parse_s)이 찍혀 전체가 칸들의 합으로 설명된다. 채우기가 끝나기 전에
    들어온 쓰기는 디스크 훑기로 떨어지는데 — 그 결정이 엔진과 같은 결정이라는 계약은
    색인 있음·없음 어느 쪽에서도 갈리지 않는다(엔진은 쓰기 순간 디스크를 훑는다).

    Mutation targets: 채우기 도중의 sync 를 빈 색인으로 바꾸는 변이(걸러둘 결정이
    저장으로 뒤집힘), 색인 경로에서 엔진 신규 노트 제외를 빼는 변이 각각 여기서
    빨갛게 끝난다."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        wiki = Path(self._tmp.name) / "wiki"
        wiki.mkdir()
        # 엔진이 방금 쓴 노트 — 요청 ARGUMENTS 의 세션(sess-1)을 그대로 갖는다.
        (wiki / "wiki-2811.md").write_text(ENGINE_NOTE.replace("wiki-2801", "wiki-2811"), encoding="utf-8")
        # 다른 세션·다른 제목·다른 본문의 옛 노트 — 이 세션의 요청과는 갈래가 안 걸린다.
        (wiki / "wiki-100.md").write_text(
            """---
id: wiki-100
title: 옛날 노트
kind: note
origin: personal
project: ''
date: "2026-09-20"
tags: []
tools: []
concepts: []
claims: []
relates_to: []
sources: []
omb_session_id: sess-old
author: unknown
---

옛날 회의 내용을 적어 둔 노트다.
""",
            encoding="utf-8",
        )
        self.index = NoteIndex(
            DiskSeams(
                vault_dir=self._tmp.name,
                list_notes=self._list_notes,
                read_note=vault_notes.read_note,
                split_frontmatter=split_frontmatter,
            )
        )

    def _list_notes(self) -> list[str]:
        return sorted(
            p.name[: -len(".md")] for p in (Path(self._tmp.name) / "wiki").iterdir() if p.name.endswith(".md")
        )

    def _answer(self, note_id: str = "2811") -> dict:
        answer = json.loads(json.dumps(MCP_ANSWER))
        answer["result"]["content"][0]["text"] = (
            f"remembered → wiki/wiki-{note_id}.md · chunks 3 · graph(tools 1 concepts 1 claims 1) — recallable now"
        )
        return answer

    def _run(self, note_index=None, answer=None):
        return run_shadow(
            ShadowRequest(
                route="mcp",
                arguments=ARGUMENTS,
                engine_status=200,
                engine_body=json.dumps(
                    answer if answer is not None else self._answer(), ensure_ascii=False
                ).encode(),
                vault_dir=self._tmp.name,
                read_note=vault_notes.read_note,
                split_frontmatter=split_frontmatter,
                list_notes=self._list_notes,
                pii_scanner=None,
                is_owner=False,
                nearest_document=_no_nearest,
                read_graph=None,
                note_index=note_index,
            )
        )

    def test_indexed_shadow_agrees_with_disk_scan(self):
        direct = self._run()
        self.assertEqual(direct.status, "ok", direct.reason)
        self.index.prefill()
        self.assertTrue(self.index.usable)
        indexed = self._run(note_index=self.index)
        self.assertEqual(indexed.status, "ok", indexed.reason)
        self.assertEqual(indexed.decision, direct.decision, "색인 있음·없음이 같은 결정")
        self.assertEqual(indexed.fields, direct.fields)
        self.assertEqual(indexed.engine_decision, direct.engine_decision)

    def test_write_during_prefill_matches_disk_scan(self):
        # 같은 세션의 옛 노트를 하나 더 놓으면 디스크 훑기는 걸러둘 결정을 내린다 —
        # 채우기 도중(sync() → None)에 들어온 쓰기도 그 결정을 그대로 내야 한다.
        wiki = Path(self._tmp.name) / "wiki"
        (wiki / "wiki-2999.md").write_text(ENGINE_NOTE.replace("wiki-2801", "wiki-2999"), encoding="utf-8")
        answer = _skipped_answer("wiki-2999")
        before = self._run(answer=answer)
        self.assertEqual(before.status, "ok", before.reason)
        self.assertEqual(before.decision, "skipped:/vault/wiki/wiki-2999.md", before.decision)
        self.index.prefill()
        after = self._run(note_index=self.index, answer=answer)
        self.assertEqual(after.status, "ok", after.reason)
        self.assertEqual(after.decision, before.decision, "채운 뒤 색인 경로도 같은 걸러둘 결정")

    def test_elapsed_parse_recorded_with_index(self):
        self.index.prefill()
        event = self._run(note_index=self.index)
        payload = event_payload(event)
        self.assertIn("elapsed_parse_s", payload)
        # 칸들의 합이 전체를 넘을 수 없다 — 잰 것들은 전부 전체 안에 있다.
        columns = (
            event.elapsed_embedding_s + event.elapsed_db_s + event.elapsed_vault_s + event.elapsed_parse_s
        )
        self.assertGreaterEqual(
            event.elapsed_total_s + 0.005, columns, "칸들의 합이 전체를 설명한다(반올림 여유 5ms)"
        )
        # 색인이 이미 채워져 있으니 중복 문 스캔은 파싱을 거의 안 한다.
        self.assertLess(event.elapsed_parse_s, 0.5, "미리 채운 뒤에는 볼트 통째 파싱이 없다")


if __name__ == "__main__":
    unittest.main(verbosity=2)
