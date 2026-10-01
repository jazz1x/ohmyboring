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

from ohmyboring.remember import pii as remember_pii  # noqa: E402
from ohmyboring.remember.shadow import (  # noqa: E402
    RELATES_TO_EXCLUDED,
    ShadowRequest,
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
    """그림자 한 번 — options: arguments·route·scanner·nearest·is_owner·status."""
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
