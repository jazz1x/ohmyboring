#!/usr/bin/env python3
"""그래프 규칙 시험 — 슬러그·canon·한자·claim kind 가 엔진과 같은지 못 박는다 (E3b).
포트 대상 — drudge/src/ingest.rs(슬러그·canon·한자), frontmatter.rs(kind 기본값·WORK_DENIALS).

Run: python3 ohmyboring/remember/test_graph.py   (no pytest dependency)

Mutation targets: 슬러그/canon 규칙을 푸는 변이 각각 시험으로 사망 확인.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "src", ROOT / "agents" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ohmyboring.remember import graph  # noqa: E402


class SlugCanonTests(unittest.TestCase):
    """ingest.rs:319-356 포트 — 슬러그·canon·한자 규칙이 엔진과 같아야 투영이 같다."""

    def test_slugify_aliases_and_ascii_only(self):
        self.assertEqual(graph.slugify("C++"), "cpp")
        self.assertEqual(graph.slugify("c#"), "csharp")
        self.assertEqual(graph.slugify(".NET"), "dotnet")
        self.assertEqual(graph.slugify("Docker Compose"), "dockercompose")
        self.assertEqual(graph.slugify("kubectl"), "kubectl")
        self.assertEqual(graph.slugify("!!!"), "")

    def test_canon_collapses_separator_runs(self):
        self.assertEqual(graph.canon("Foo Bar_baz--qux"), "foo-bar-baz-qux")
        self.assertEqual(graph.canon("배포 절차"), "배포-절차")
        self.assertEqual(graph.canon("-edge-"), "edge")

    def test_han_filters_chinese_not_korean(self):
        # 모형이 중국어로 새어 나간 것만 걸러낸다 — 한글은 한자가 아니다(ingest.rs:317-321).
        self.assertTrue(graph.has_han("概念"))
        self.assertFalse(graph.has_han("배포"))
        self.assertFalse(graph.has_han("コンセプト"))  # 가타칸아도 한자(한자 영역)가 아니다

    def test_claim_kind_defaults_and_work_denial_downgrade(self):
        self.assertEqual(graph.claim_kind("", "어떤 값"), "fact")
        self.assertEqual(graph.claim_kind("decision", "go"), "decision")
        # next/blocked 가 「할 일 없음」을 쥐면 fact 로 강등(frontmatter.rs:135-144).
        self.assertEqual(graph.claim_kind("next", "없음"), "fact")
        self.assertEqual(graph.claim_kind("blocked", "N/A"), "fact")
        self.assertEqual(graph.claim_kind("next", "추가"), "next")
        self.assertEqual(graph.claim_kind("risk", "해당없음"), "risk")


if __name__ == "__main__":
    unittest.main(verbosity=2)
