#!/usr/bin/env python3
"""render 시험 — drudge 의 Rust 사례(remember.rs::tests::render_*)로 고정한다.

Run: python3 ohmyboring/remember/test_render.py   (no pytest dependency)

기준 사례는 remember.rs:357-386 — 세션 id 가 있을 때 `omb_session_id: sess-abc123` 가
머리말에 실리고 없을 때는 칸 자체가 안 나오며, 렌더가 YAML 로 되돌아(파싱이) 된다.
곁들여 칸 순서(Rust Fm 구조체 순서)와 기본값(title 없으면 wiki_id, kind 없으면 note)을
못 박는다.

Mutation targets: omb_session_id 생략 규칙을 빼는 변이, 칸 순서를 뒤집는 변이,
relates_to 시작값([])을 바꾸는 변이 각각 시험으로 사망 확인.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "src", ROOT / "agents" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import yaml  # noqa: E402
from vault_note import split_frontmatter  # noqa: E402

from ohmyboring.remember.parse import Claim, FrontMatter  # noqa: E402
from ohmyboring.remember.render import render_wiki_note  # noqa: E402


def _front(**over) -> FrontMatter:
    base = dict(
        title="제목",
        kind="note",
        origin="personal",
        project="olympus",
        date="2026-06-22",
        tags=("a", "b"),
        tools=(),
        concepts=(),
        claims=(),
        sources=(),
        omb_session_id=None,
        author="unknown",
    )
    base.update(over)
    return FrontMatter(**base)


class RenderWikiNoteTests(unittest.TestCase):
    def test_session_id_persisted_when_present_and_omitted_when_absent(self):
        # remember.rs:357 그대로 — 없으면 칸이 안 나오고(옛 노트를 어지럽히지 않음),
        # 있으면 증거로 실리고 YAML 이 되돌아간다.
        out = render_wiki_note("wiki-0042", _front(), "body")
        self.assertNotIn("omb_session_id", out)

        out = render_wiki_note(
            "wiki-0042",
            _front(omb_session_id="sess-abc123", sources=("raw/session-manifests/sess-abc123.md",)),
            "body",
        )
        self.assertIn("omb_session_id: sess-abc123", out)
        split = split_frontmatter(out)
        self.assertIsNotNone(split)
        parsed = yaml.safe_load(split[0])
        self.assertEqual(parsed["omb_session_id"], "sess-abc123")
        self.assertEqual(parsed["sources"][0], "raw/session-manifests/sess-abc123.md")

    def test_field_order_matches_the_rust_fm_struct(self):
        out = render_wiki_note("wiki-0042", _front(omb_session_id="s1"), "body")
        front = yaml.safe_load(split_frontmatter(out)[0])
        self.assertEqual(
            list(front),
            [
                "id",
                "title",
                "kind",
                "origin",
                "project",
                "date",
                "tags",
                "tools",
                "concepts",
                "claims",
                "relates_to",
                "sources",
                "omb_session_id",
                "author",
            ],
        )
        # 없으면 omb_session_id 칸 통째가 빠진다(순서는 나머지 그대로).
        front = yaml.safe_load(split_frontmatter(render_wiki_note("wiki-0042", _front(), "body"))[0])
        self.assertNotIn("omb_session_id", front)
        self.assertEqual(front["author"], "unknown")

    def test_relates_to_starts_empty_for_the_graph_projection_to_fill(self):
        front = yaml.safe_load(split_frontmatter(render_wiki_note("wiki-0042", _front(), "body"))[0])
        self.assertEqual(front["relates_to"], [])

    def test_defaults_title_and_kind(self):
        out = render_wiki_note("wiki-0042", _front(title="", kind=""), "body")
        front = yaml.safe_load(split_frontmatter(out)[0])
        self.assertEqual(front["title"], "wiki-0042")
        self.assertEqual(front["kind"], "note")

    def test_claim_fields_follow_the_rust_struct(self):
        front = _front(
            claims=(
                Claim(subject="s", predicate="p", value="v"),
                Claim(
                    subject="s2",
                    predicate="p2",
                    value="v2",
                    kind="next",
                    confidence="likely",
                    said_by="owner",
                ),
            )
        )
        parsed = yaml.safe_load(split_frontmatter(render_wiki_note("wiki-0042", front, "body"))[0])
        self.assertEqual(list(parsed["claims"][0]), ["subject", "predicate", "value", "kind", "confidence"])
        self.assertEqual(parsed["claims"][0]["kind"], "")
        self.assertEqual(
            list(parsed["claims"][1]),
            ["subject", "predicate", "value", "kind", "confidence", "said_by"],
        )
        self.assertEqual(parsed["claims"][1]["said_by"], "owner")

    def test_body_trailing_whitespace_trimmed_to_one_newline(self):
        out = render_wiki_note("wiki-0042", _front(), "body\n\n\n")
        split = split_frontmatter(out)
        self.assertEqual(split[1], "body\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
