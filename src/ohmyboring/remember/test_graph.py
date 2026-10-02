#!/usr/bin/env python3
"""그래프 투영 시험 — 한 노트가 엔진 그래프에 남길 간선 집합과 대체 봉인의 목표 상태를
정하는 계약(E3b)을 못 박는다. 포트 대상 — drudge/src/ingest.rs(슬러그·canon·한자),
frontmatter.rs(kind 기본값·WORK_DENIALS), store.rs(노드 모양·부분 닫기의 반대).

Run: python3 ohmyboring/remember/test_graph.py   (no pytest dependency)

Mutation targets: 부분 닫기를 통째 봉인으로 바꾸는 변이(expected_seal_states 가 옛 노트
행을 전부 닫으려 함), 간선 부류 하나를 빼는 변이(is_a·said·claim_of_project…), 슬러그/
canon 규칙을 푸는 변이 각각 시험으로 사망 확인.
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


def _front(**overrides):
    """대조용 뷰 모양의 머리말 — 기본은 단순 노트."""
    front = {
        "title": "시험",
        "kind": "note",
        "origin": "personal",
        "project": "",
        "author": "agent:claude",
        "omb_session_id": None,
        "tags": [],
        "tools": [],
        "concepts": [],
        "claims": [],
        "sources": [],
    }
    front.update(overrides)
    return front


def _claim(subject, predicate, value, **rest):
    out = {
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "kind": "",
        "confidence": "",
        "said_by": None,
    }
    out.update(rest)
    return out


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


class ExpectedEdgesTests(unittest.TestCase):
    """투영 한 벌 — 엔진이 노트를 그래프에 옮길 (src, kind, dst) 집합."""

    def test_project_tags_tools_concepts_claims(self):
        front = _front(
            project="kb-agent",
            tags=["repo/kb-agent", "deploy"],
            tools=["kubectl", "Docker Compose"],
            concepts=["deploy pipeline"],
            claims=[
                _claim("배포", "절차", "make deploy", kind="fact", confidence="certain"),
                _claim(
                    "릴리스", "주기", "매주 목요일", kind="decision", confidence="likely", said_by="owner"
                ),
            ],
        )
        self.assertEqual(
            graph.expected_edges(front, "/vault/wiki/wiki-2810.md"),
            frozenset(
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
            ),
        )

    def test_fact_claim_has_no_is_a_edge(self):
        front = _front(claims=[_claim("배포", "절차", "make deploy", kind="fact")])
        edges = graph.expected_edges(front, "/vault/wiki/wiki-1.md")
        self.assertNotIn(("claim:배포:절차", "is_a", "fact:배포:절차"), edges)
        self.assertIn(("doc:/vault/wiki/wiki-1.md", "claims", "claim:배포:절차"), edges)

    def test_empty_han_and_blank_claims_are_not_written(self):
        front = _front(
            project="kb-agent",
            claims=[
                _claim("", "절차", "make deploy"),  # 빈 주체
                _claim("배포", "", "make deploy"),  # 빈 술어
                _claim("배포", "절차", ""),  # 빈 값
                _claim("개념概念", "절차", "make deploy"),  # 주체에 한자
                _claim("배포", "절차", "값 값"),  # 값에 한자
                _claim("배포", "절차", "make deploy"),  # 이건 쓰인다
            ],
        )
        edges = graph.expected_edges(front, "/vault/wiki/wiki-1.md")
        self.assertEqual(
            edges,
            frozenset(
                {
                    ("doc:/vault/wiki/wiki-1.md", "in_project", "project:kb-agent"),
                    ("doc:/vault/wiki/wiki-1.md", "claims", "claim:배포:절차"),
                    ("claim:배포:절차", "claim_of_project", "project:kb-agent"),
                }
            ),
        )

    def test_tools_cap_six_and_han_and_dedup(self):
        tools = ["t1", "t2", "t3", "t4", "t5", "t6", "t7", "개념", "t1"]
        front = _front(tools=tools)
        edges = graph.expected_edges(front, "/vault/wiki/wiki-1.md")
        uses = sorted(dst for _, kind, dst in edges if kind == "uses")
        # 앞 6개만 보고(t7 빠짐), 한자 도구는 빠지고, 중복 슬러그는 한 번만.
        self.assertEqual(uses, ["tool:t1", "tool:t2", "tool:t3", "tool:t4", "tool:t5", "tool:t6"])

    def test_no_project_no_project_edges(self):
        front = _front(claims=[_claim("배포", "절차", "make deploy", kind="decision")])
        edges = graph.expected_edges(front, "/vault/wiki/wiki-1.md")
        self.assertFalse([e for e in edges if e[1] in ("in_project", "claim_of_project")])

    def test_supersedes_edges_appended(self):
        front = _front()
        edges = graph.expected_edges(
            front,
            "/vault/wiki/wiki-2810.md",
            ("/vault/wiki/wiki-2757.md", "/vault/wiki/wiki-2758.md"),
        )
        self.assertEqual(
            edges,
            frozenset(
                {
                    ("doc:/vault/wiki/wiki-2810.md", "supersedes", "doc:/vault/wiki/wiki-2757.md"),
                    ("doc:/vault/wiki/wiki-2810.md", "supersedes", "doc:/vault/wiki/wiki-2758.md"),
                }
            ),
        )


class RestatedSlotsTests(unittest.TestCase):
    """부분 닫기의 「닫을 것」 — 새 노트가 다시 말한 슬롯만."""

    def test_only_written_claims_restate_slots(self):
        front = _front(
            claims=[
                _claim("배포", "절차", "make deploy"),
                _claim("", "절차", "make deploy"),
                _claim("알림", "채널", "슬랙"),
            ]
        )
        self.assertEqual(graph.restated_slots(front), frozenset({("배포", "절차"), ("알림", "채널")}))


class PartialCloseTests(unittest.TestCase):
    """부분 닫기 — wiki-2757/2758 모양: 옛 노트 사실 셋, 새 노트가 하나만 다시 말하고 대체.
    목표는 하나만 닫고 둘은 사는 것; 통째 봉인(store.rs:2898)은 이식하지 않는다(wiki-2855)."""

    def _rows(self, sealed_unrestated: bool = True):
        return (
            graph.ClaimRow("/vault/wiki/wiki-2757.md", "배포", "절차", "fact", True),
            graph.ClaimRow("/vault/wiki/wiki-2757.md", "모니터링", "대상", "fact", sealed_unrestated),
            graph.ClaimRow("/vault/wiki/wiki-2757.md", "알림", "채널", "fact", sealed_unrestated),
            graph.ClaimRow("/vault/wiki/wiki-2810.md", "배포", "절차", "fact", False),
        )

    def test_partial_close_intent_keeps_unrestated_facts_alive(self):
        rows = self._rows()
        slots = frozenset({("배포", "절차")})
        expected = graph.expected_seal_states(
            rows,
            "/vault/wiki/wiki-2810.md",
            slots,
            frozenset({"/vault/wiki/wiki-2757.md"}),
        )
        verdict = graph.compare_seals(expected, "/vault/wiki/wiki-2810.md")
        # 엔진이 통째로 닫은 상태를 읽으면: 다시 말한 슬롯은 일치, 나머지 둘이 engine_only.
        self.assertEqual(verdict.checked, 4)
        self.assertEqual(verdict.engine_only, 2)
        self.assertEqual(verdict.python_only, 0)
        self.assertEqual(verdict.engine_only_on_new_note, 0)

    def test_exact_partial_close_is_no_divergence(self):
        # 엔진도 부분 닫기를 했다면(또는 목표대로 맞춰졌다면) 어긋남이 없다.
        rows = self._rows(sealed_unrestated=False)
        slots = frozenset({("배포", "절차")})
        verdict = graph.compare_seals(
            graph.expected_seal_states(
                rows, "/vault/wiki/wiki-2810.md", slots, frozenset({"/vault/wiki/wiki-2757.md"})
            ),
            "/vault/wiki/wiki-2810.md",
        )
        self.assertEqual((verdict.engine_only, verdict.python_only), (0, 0))

    def test_wholesale_seal_intent_is_the_mutant_shape(self):
        # 통째 봉인 변이의 모양 — 옛 노트 행을 전부 닫겠다고 목표를 세우면 engine_only 가 0이
        # 되어 (나) 어긋남이 사라진다. 이 변이가 시험으로 사망하는 이유를 여기서 못 박는다.
        rows = self._rows()
        wholesale = [(row, True) for row in rows if row.source_path == "/vault/wiki/wiki-2757.md"]
        wholesale += [(row, False) for row in rows if row.source_path == "/vault/wiki/wiki-2810.md"]
        verdict = graph.compare_seals(wholesale, "/vault/wiki/wiki-2810.md")
        self.assertEqual(verdict.engine_only, 0)

    def test_unsealed_restated_slot_is_python_only(self):
        # 엔진이 다시 말한 슬롯마저 살린 채로 두면 python_only — 엔진이 목표보다 덜 닫은 것.
        rows = (
            graph.ClaimRow("/vault/wiki/wiki-2757.md", "배포", "절차", "fact", False),
            graph.ClaimRow("/vault/wiki/wiki-2810.md", "배포", "절차", "fact", False),
        )
        verdict = graph.compare_seals(
            graph.expected_seal_states(
                rows,
                "/vault/wiki/wiki-2810.md",
                frozenset({("배포", "절차")}),
                frozenset({"/vault/wiki/wiki-2757.md"}),
            ),
            "/vault/wiki/wiki-2810.md",
        )
        self.assertEqual((verdict.engine_only, verdict.python_only), (0, 1))

    def test_sealed_new_note_row_is_flagged_apart(self):
        # 새 노트 자신의 행이 닫힌 것은 통째 봉인 차이가 아니라 이상 현상 — 갈래를 달리 본다.
        rows = (
            graph.ClaimRow("/vault/wiki/wiki-2757.md", "모니터링", "대상", "fact", True),
            graph.ClaimRow("/vault/wiki/wiki-2810.md", "배포", "절차", "fact", True),
        )
        verdict = graph.compare_seals(
            graph.expected_seal_states(
                rows,
                "/vault/wiki/wiki-2810.md",
                frozenset({("배포", "절차")}),
                frozenset({"/vault/wiki/wiki-2757.md"}),
            ),
            "/vault/wiki/wiki-2810.md",
        )
        self.assertEqual(verdict.engine_only_on_new_note, 1)
        self.assertEqual(
            verdict.engine_only - verdict.engine_only_on_new_note, 1
        )  # 옛 노트 쪽도 세지만 갈래가 다르다


if __name__ == "__main__":
    unittest.main(verbosity=2)
