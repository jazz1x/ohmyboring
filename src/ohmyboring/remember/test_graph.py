#!/usr/bin/env python3
"""그래프 투영 시험 — 대체 봉인의 목표 상태를 정하는 계약(E3b)을 못 박는다.
포트 대상 — drudge/src/ingest.rs(슬러그·canon·한자), frontmatter.rs(kind 기본값·WORK_DENIALS),
store.rs(부분 닫기의 반대).

Run: python3 ohmyboring/remember/test_graph.py   (no pytest dependency)

Mutation targets: 부분 닫기를 통째 봉인으로 바꾸는 변이(expected_seal_states 가 옛 노트
행을 전부 닫으려 함), 슬러그/canon 규칙을 푸는 변이 각각 시험으로 사망 확인.
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


class PartialCloseTests(unittest.TestCase):
    """부분 닫기 — wiki-2757/2758 모양: 옛 노트 사실 셋, 새 노트가 하나만 다시 말하고 대체.
    목표는 하나만 닫고 둘은 사는 것; 통째 봉인(store.rs:2898)은 이식하지 않는다(wiki-2855)."""

    def test_a_restated_subject_keeps_its_other_predicates_alive(self):
        rows = (
            graph.ClaimRow("/vault/wiki/wiki-2757.md", "배포", "절차", "fact", True),
            graph.ClaimRow("/vault/wiki/wiki-2757.md", "배포", "담당", "fact", False),
        )
        expected = dict(
            ((row.subject, row.predicate), close)
            for row, close in graph.expected_seal_states(
                rows,
                "/vault/wiki/wiki-2810.md",
                frozenset({("배포", "절차")}),
                frozenset({"/vault/wiki/wiki-2757.md"}),
            )
        )
        self.assertEqual(expected, {("배포", "절차"): True, ("배포", "담당"): False})


if __name__ == "__main__":
    unittest.main(verbosity=2)
