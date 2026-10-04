#!/usr/bin/env python3
"""parse 시험 — drudge 의 Rust 사례(remember.rs tests)로 고정한다.

Run: python3 ohmyboring/remember/test_parse.py   (no pytest dependency)

normalize_body·sanitize_tag 는 drudge/src/vault/remember.rs 의 테스트 케이스를
대안 하나 빠짐없이 옮긴다 — 그래서 이 파일의 한글이나 백슬래시 하나까지 정본이다.
곁들여 parse_remember_note 의 계약(태그 여섯·repo 태그 맨 앞·claims 빈 칸 버림·
said_by=owner 만·author 어휘·비밀 가림)을 못 박는다.

Mutation targets: sanitize 규약(허용 집합·쪼개기 붙임·숫자 태그 거절)을 흐리는 변이,
normalize 의 이스케이프 표를 빠뜨리는 변이, repo 태그 삽입을 빼는 변이, claims 의
said_by=owner 검사를 빼는 변이 각각 시험으로 사망 확인.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "src", ROOT / "agents" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ohmyboring.remember.parse import (  # noqa: E402
    normalize_body,
    parse_remember_note,
    sanitize_tag,
)
from ohmyboring.result import Err, Ok  # noqa: E402


class NormalizeBodyTests(unittest.TestCase):
    """Rust remember.rs::tests 의 normalize_body 사례 그대로."""

    def test_decodes_literal_newline(self):
        self.assertEqual(
            normalize_body("### A\\n1. x\\n\\n### B\\n2. y"),
            "### A\n1. x\n\n### B\n2. y",
        )

    def test_unescapes_stray_markdown_punctuation(self):
        self.assertEqual(normalize_body("use \\`corpus_status\\`"), "use `corpus_status`")
        self.assertEqual(normalize_body('fix \\#59 \\"claim\\"'), 'fix #59 "claim"')

    def test_keeps_genuine_backslashes(self):
        self.assertEqual(normalize_body("path C:\\Users and re \\d+"), "path C:\\Users and re \\d+")

    def test_leaves_clean_markdown_unchanged(self):
        clean = "## 배경\n결정함.\n\n## 결과\n- 끝"
        self.assertEqual(normalize_body(clean), clean)

    def test_strips_bare_trailing_heading(self):
        self.assertEqual(normalize_body("## 배경\n결정함.\n\n## 남은 일\n"), "## 배경\n결정함.")

    def test_strips_stacked_empty_trailing_headings(self):
        self.assertEqual(
            normalize_body("## 결과\n- 끝\n\n## 남은 일\n\n## 다음 단계\n"),
            "## 결과\n- 끝",
        )

    def test_keeps_heading_with_content(self):
        kept = "## 배경\n결정함.\n\n## 남은 일\n- 후속 PR"
        self.assertEqual(normalize_body(kept), kept)

    def test_keeps_hashtag_reference(self):
        self.assertEqual(normalize_body("작업 요약.\n\n관련 #59"), "작업 요약.\n\n관련 #59")

    def test_strips_trailing_heading_after_escape_decode(self):
        self.assertEqual(normalize_body("## 배경\\n결정함.\\n\\n## 남은 일"), "## 배경\n결정함.")


class SanitizeTagTests(unittest.TestCase):
    """Rust remember.rs::tests 의 sanitize_tag 사례 그대로."""

    def test_space_to_hyphen(self):
        self.assertEqual(sanitize_tag("claude code"), "claude-code")
        self.assertEqual(sanitize_tag("data management"), "data-management")
        self.assertEqual(sanitize_tag("session hook"), "session-hook")

    def test_keeps_valid(self):
        self.assertEqual(sanitize_tag("rag"), "rag")
        self.assertEqual(sanitize_tag("pre-commit"), "pre-commit")
        self.assertEqual(sanitize_tag("repo/oh-my-boring"), "repo/oh-my-boring")

    def test_strips_and_collapses(self):
        self.assertEqual(sanitize_tag("  Rust!! Style  "), "rust-style")
        self.assertEqual(sanitize_tag("-leading-trailing-"), "leading-trailing")

    def test_empty_and_pure_number_are_none(self):
        self.assertIsNone(sanitize_tag(""))
        self.assertIsNone(sanitize_tag("!!!"))
        self.assertIsNone(sanitize_tag("59"))
        self.assertIsNone(sanitize_tag("--"))


class ParseRememberNoteTests(unittest.TestCase):
    """parse_remember_note 계약 — mcp.rs:2087-2192 가 하는 일을 못 박는다."""

    def setUp(self):
        # canonical_repo 는 boring.json 규칙을 읽는다 — 빈 설정으로 고정해
        # 이 기계의 live 규칙(oh-my-boring→ohmyboring 같은 이름 붙이기)이
        # 결과를 흔들지 않게 한다.
        self._tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "omb-remember-test-config"
        self._tmp.mkdir(parents=True, exist_ok=True)
        (self._tmp / "boring.json").write_text("{}", encoding="utf-8")
        self._env = mock.patch.dict(os.environ, {"BORING_CONFIG": str(self._tmp / "boring.json")})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _note(self, **over):
        args = {
            "title": "제목",
            "body": "본문",
            "origin": "personal",
            "tags": ["slack", "claude code"],
            "tools": ["postgres"],
            "concepts": ["그래프"],
            "claims": [
                {"subject": "s", "predicate": "p", "value": "v"},
                {"subject": "", "predicate": "p", "value": "v"},
            ],
        }
        args.update(over)
        match parse_remember_note(args):
            case Ok(note):
                return note
            case Err(reason):
                self.fail(f"expected Ok, got Err({reason!r})")

    def test_missing_title_and_body_are_errors(self):
        match parse_remember_note({"body": "b"}):
            case Err("missing argument: title"):
                pass
            case other:
                self.fail(f"expected missing title, got {other!r}")
        match parse_remember_note({"title": "t", "body": " \\n "}):
            case Err("missing argument: body"):
                pass
            case other:
                self.fail(f"expected missing body, got {other!r}")

    def test_origin_vocab_is_the_engines(self):
        self.assertEqual(self._note(origin="company").front.origin, "company")
        self.assertEqual(self._note(origin="").front.origin, "personal")
        match parse_remember_note({"title": "t", "body": "b", "origin": "work"}):
            case Err(reason):
                self.assertIn("invalid origin", reason)
            case other:
                self.fail(f"expected Err, got {other!r}")

    def test_repo_becomes_project_and_leading_tag(self):
        note = self._note(repo="Org/oh-my-boring.git")
        self.assertEqual(note.front.project, "oh-my-boring")
        self.assertEqual(note.front.tags[0], "repo/oh-my-boring")
        self.assertEqual(note.front.tags[1:], ("slack", "claude-code"))

    def test_tags_capped_at_six_before_repo_tag(self):
        note = self._note(tags=["a", "b", "c", "d", "e", "f", "g"], repo="x")
        self.assertEqual(len(note.front.tags), 7)  # 여섯 + repo 태그(맨 앞 삽입)
        self.assertEqual(note.front.tags[0], "repo/x")
        self.assertNotIn("g", note.front.tags)

    def test_empty_claims_are_dropped_and_said_by_owner_only(self):
        note = self._note(
            claims=[
                {"subject": "s", "predicate": "p", "value": "v", "said_by": "owner"},
                {"subject": "s", "predicate": "p", "value": "", "said_by": "owner"},
            ]
        )
        self.assertEqual(len(note.front.claims), 1)
        self.assertEqual(note.front.claims[0].said_by, "owner")
        match parse_remember_note(
            {
                "title": "t",
                "body": "b",
                "claims": [{"subject": "s", "predicate": "p", "value": "v", "said_by": "agent"}],
            }
        ):
            case Err(reason):
                self.assertIn("said_by", reason)
            case other:
                self.fail(f"expected Err, got {other!r}")

    def test_author_vocab_and_default(self):
        self.assertEqual(self._note().front.author, "unknown")
        self.assertEqual(self._note(author="owner").front.author, "owner")
        self.assertEqual(self._note(author="agent:hermes").front.author, "agent:hermes")
        match parse_remember_note({"title": "t", "body": "b", "author": "admin"}):
            case Err(reason):
                self.assertIn("author must be", reason)
            case other:
                self.fail(f"expected Err, got {other!r}")

    def test_secret_redaction_applies_to_title_body_and_claims(self):
        # 슬랙 토큰 형식의 더러운 값 하나 — 리터럴이 `token = "…"` 모양이면 시크릿 스캐너가
        # 집어 삼키니(가짜라도) 둘로 나눠 만든다.
        dirty = "xoxb-" + "1234567890abcdef"
        note = self._note(title=f"키 {dirty}", body=f"값 {dirty}")
        self.assertNotIn(dirty, note.front.title)
        self.assertNotIn(dirty, note.body)
        self.assertIn("‹REDACTED›", note.body)

    def test_body_is_normalized_before_redaction(self):
        note = self._note(body="### A\\n1. x\\n\\n## 남은 일")
        self.assertEqual(note.body, "### A\n1. x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
