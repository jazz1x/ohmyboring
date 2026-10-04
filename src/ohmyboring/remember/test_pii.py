#!/usr/bin/env python3
"""PII 게이트 시험 — drudge pii.rs 와 mcp.rs:1597 apply_pii_gate 의 파이썬 이식분을 못 박는다.

Run: python3 ohmyboring/remember/test_pii.py   (no pytest dependency)

픽스처는 pii.rs 의 시험 스캐너(이메일·전화 redact, 주민등록번호 block, 티켓 flag,
git@ allow, 면제 마커)에 그룹 참조 치환($1 → [NAME]) 규칙 하나를 더한 것이다.
대조 규약: block 은 면제 마커를 무시하고 첫 적중에서 끝나고, flag 는 마커가 있는
줄을 면제하고, redact 는 allow 구간을 손대지 않는다. 게이트 전체는 엔진 순서
(제목→본문→태그→tools→concepts→sources→claims)대로 가리고 flag 가 하나라도 걸리면
pii-flag 태그를 붙인다.

Mutation targets: 면제 마커를 block 에도 적용하는 변이, allow 구간을 redact 가
가리는 변이, $1 그룹 참조를 빼는 변이, pii-flag 누적을 빼는 변이 각각 시험으로 사망 확인.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ohmyboring.remember.parse import Claim, FrontMatter, RememberNote  # noqa: E402
from ohmyboring.remember.pii import (  # noqa: E402
    apply_pii_gate,
    load,
    load_from_vault,
)
from ohmyboring.result import Err, Ok  # noqa: E402

#: pii.rs 시험 스캐너 + 라벨 뒤 이름 가림($1 그룹 참조) — vault/rules/pii.yaml 의 모양을 본따.
BASE_YAML = """
version: "1.0"
policy:
  exemption_marker: "<!-- pii-allow -->"
rules:
  - name: email
    regex: '(?i)\\b[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{2,}\\b'
    action: redact
    replacement: "[EMAIL]"
    severity: warning
    reason: email
  - name: phone
    regex: '\\b01[0-9][-\\s]?\\d{3,4}[-\\s]?\\d{4}\\b'
    action: redact
    replacement: "[PHONE]"
    severity: warning
    reason: phone
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
  - name: git_ssh_user
    regex: '\\bgit@[a-z0-9.-]+\\.[a-z]{2,}\\b'
    action: allow
    severity: info
    reason: git clone SSH 유저
  - name: name_after_author_label
    regex: '((?:작성자|보고자|기안자)\\s*[:：]\\s*)([김이박][가-힣]{1,2}(?![가-힣]))'
    action: redact
    replacement: "$1[NAME]"
    severity: warning
    reason: 라벨 뒤 실명
"""

OVERLAY_YAML = """
version: "1.0"
policy:
  default_action: block
rules:
  - name: implicit_secret
    regex: '\\bSECRET-\\d+\\b'
    reason: 생략된 action 은 정책 기본값을 받는다
"""


def _load_yaml(yaml_text: str, overlay: str | None = None):
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name) / "pii.yaml"
    base.write_text(yaml_text, encoding="utf-8")
    local = Path(tmp.name) / "pii.local.yaml"
    if overlay is not None:
        local.write_text(overlay, encoding="utf-8")
    else:
        local = Path(tmp.name) / "pii.missing.yaml"
    scanner = load(base, local)
    return tmp, scanner


class LoadTests(unittest.TestCase):
    def test_no_rule_files_disables_the_gate(self):
        tmp = tempfile.TemporaryDirectory()
        missing = Path(tmp.name) / "nope.yaml"
        match load(missing, Path(tmp.name) / "nope2.yaml"):
            case Ok(scanner):
                self.assertIsNone(scanner)
            case other:
                self.fail(f"expected Ok(None), got {other!r}")

    def test_local_overlay_without_base_is_an_error(self):
        tmp, scanner = _load_yaml(BASE_YAML, OVERLAY_YAML)
        tmp_path = Path(tmp.name)
        (tmp_path / "pii.local.yaml").write_text(OVERLAY_YAML, encoding="utf-8")
        (tmp_path / "pii.yaml").unlink()
        match load(tmp_path / "pii.yaml", tmp_path / "pii.local.yaml"):
            case Err(reason):
                self.assertIn("base rules missing", reason)
            case other:
                self.fail(f"expected Err, got {other!r}")

    def test_broken_regex_is_an_error_naming_the_rule(self):
        tmp, _ = _load_yaml(BASE_YAML.replace("\\d{3,4}", "([", 1))  # phone 규칙의 정규식을 깬다
        match load(Path(tmp.name) / "pii.yaml", Path(tmp.name) / "pii.missing.yaml"):
            case Err(reason):
                self.assertIn("phone", reason)
            case other:
                self.fail(f"expected Err naming the rule, got {other!r}")

    def test_overlay_extends_rules_and_policy_default(self):
        match _load_yaml(BASE_YAML, OVERLAY_YAML)[1]:
            case Ok(scanner):
                rules = {rule.name: rule for rule in scanner.rules}
                self.assertIn("implicit_secret", rules, "오버레이 규칙이 덧붙는다")
                self.assertIn("email", rules, "베이스 규칙은 유지된다")
                # 생략된 action 은 오버레이 정책의 기본값(block)을 받는다.
                self.assertEqual(rules["implicit_secret"].action, "block")
                # 면제 마커는 베이스 것이 유지된다(오버레이에 없으니).
                self.assertEqual(scanner.exemption_marker, "<!-- pii-allow -->")
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_unknown_policy_key_fails_closed(self):
        match _load_yaml(
            BASE_YAML.replace("exemption_marker:", "scan_scope: [wiki]\n  exemption_marker:", 1)
        )[1]:
            case Err(reason):
                self.assertIn("unknown policy keys", reason)
            case other:
                self.fail(f"expected Err, got {other!r}")

    def test_load_from_vault_uses_conventional_paths(self):
        tmp = tempfile.TemporaryDirectory()
        rules_dir = Path(tmp.name) / "rules"
        rules_dir.mkdir()
        (rules_dir / "pii.yaml").write_text(BASE_YAML, encoding="utf-8")
        match load_from_vault(tmp.name):
            case Ok(scanner):
                self.assertTrue(scanner.rules)
            case other:
                self.fail(f"expected Ok, got {other!r}")


class ScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp, scanner = _load_yaml(BASE_YAML)
        match scanner:
            case Ok(value):
                self.scanner = value
            case other:
                self.fail(f"fixture scanner: {other!r}")

    def tearDown(self):
        self._tmp.cleanup()

    def test_redacts_email_and_phone(self):
        out = self.scanner.scan("contact foo@example.com or 010-1234-5678")
        self.assertIsNone(out.block)
        self.assertEqual(out.redacted, "contact [EMAIL] or [PHONE]")
        self.assertGreaterEqual(out.redacted_count, 2)

    def test_group_reference_replacement_keeps_the_label(self):
        out = self.scanner.scan("작성자: 김철수 가 결재")
        self.assertEqual(out.redacted, "작성자: [NAME] 가 결재")

    def test_block_rule_hits_first_and_ignores_exemption(self):
        out = self.scanner.scan("주민 900101-1234567 <!-- pii-allow --> 끝")
        self.assertIsNotNone(out.block)
        self.assertEqual(out.block.rule, "rrn")

    def test_flag_rule_honors_exemption_marker(self):
        out = self.scanner.scan("see FDS-12345 for context")
        self.assertEqual([f.rule for f in out.flags], ["ticket"])
        exempt = self.scanner.scan("see FDS-12345 <!-- pii-allow --> for context")
        self.assertEqual(exempt.flags, ())

    def test_allow_span_protects_git_ssh_user_from_redact(self):
        out = self.scanner.scan("clone git@github.com then mail foo@example.com")
        self.assertEqual(out.redacted, "clone git@github.com then mail [EMAIL]")

    def test_block_stops_further_flag_scanning(self):
        out = self.scanner.scan("FDS-1 and 900101-1234567 and ABC-2")
        self.assertIsNotNone(out.block)
        self.assertEqual(out.flags, (), "block 이 첫 적중에서 끝난다")


def _note(title: str, body: str, tags: tuple[str, ...] = ()) -> RememberNote:
    front = FrontMatter(
        title=title,
        kind="note",
        origin="personal",
        project="",
        date="",
        tags=tags,
        tools=(),
        concepts=(),
        claims=(),
        sources=(),
        omb_session_id=None,
        author="unknown",
    )
    return RememberNote(front=front, body=body)


class ApplyGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp, scanner = _load_yaml(BASE_YAML)
        match scanner:
            case Ok(value):
                self.scanner = value
            case other:
                self.fail(f"fixture scanner: {other!r}")

    def tearDown(self):
        self._tmp.cleanup()

    def test_blocked_field_is_an_error_with_the_rule_name(self):
        note = _note("배포", "주민번호 900101-1234567")
        match apply_pii_gate(self.scanner, note):
            case Err(reason):
                self.assertIn("PII gate blocked by rule 'rrn'", reason)
            case other:
                self.fail(f"expected Err, got {other!r}")

    def test_redacted_note_keeps_facts_and_masks_pii(self):
        note = _note("작성자: 김철수 결재", "연락 foo@example.com / 010-1234-5678", tags=("deploy",))
        match apply_pii_gate(self.scanner, note):
            case Ok(out):
                self.assertEqual(out.front.title, "작성자: [NAME] 결재")
                self.assertEqual(out.body, "연락 [EMAIL] / [PHONE]")
                self.assertEqual(out.front.tags, ("deploy",), "깨끗한 태그는 그대로")
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_flag_match_adds_pii_flag_tag(self):
        note = _note("배포", "관련 티켓 FDS-12345")
        match apply_pii_gate(self.scanner, note):
            case Ok(out):
                self.assertIn("pii-flag", out.front.tags)
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_no_match_leaves_tags_alone(self):
        note = _note("배포", "평범한 내용", tags=("deploy",))
        match apply_pii_gate(self.scanner, note):
            case Ok(out):
                self.assertEqual(out.front.tags, ("deploy",))
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_exemption_marker_skips_flag_tag(self):
        note = _note("배포", "see FDS-12345 <!-- pii-allow --> 끝")
        match apply_pii_gate(self.scanner, note):
            case Ok(out):
                self.assertNotIn("pii-flag", out.front.tags)
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_claim_fields_are_masked(self):
        front = FrontMatter(
            title="배포",
            kind="note",
            origin="personal",
            project="",
            date="",
            tags=(),
            tools=(),
            concepts=(),
            claims=(Claim("주제", "담당", "작성자: 김철수"),),
            sources=("foo@example.com",),
            omb_session_id=None,
            author="unknown",
        )
        match apply_pii_gate(self.scanner, RememberNote(front=front, body="본문")):
            case Ok(out):
                self.assertEqual(out.front.claims[0].value, "작성자: [NAME]")
                self.assertEqual(out.front.sources, ("[EMAIL]",))
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_pii_flag_tag_is_sanitized_not_duplicated(self):
        note = _note("배포", "관련 티켓 FDS-12345", tags=("pii-flag", "pii-flag"))
        match apply_pii_gate(self.scanner, note):
            case Ok(out):
                self.assertEqual(list(out.front.tags).count("pii-flag"), 1)
            case other:
                self.fail(f"expected Ok, got {other!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
