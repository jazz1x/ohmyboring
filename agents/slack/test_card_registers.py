#!/usr/bin/env python3
"""card_registers — collect_registers, candidate_order, merge_project_candidates.

Run: python3 agents/slack/test_card_registers.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import card_registers as cr  # noqa: E402
import card_types as cc  # noqa: E402

FETCH_DATA = {
    "/next_actions": {"answer": "next: 정리하고 재작성할 것", "sources": ["next_action", "loop"]},
    "/risks": {"answer": "risk: 엔진 문 응답 지연", "sources": ["f64_risk", "essential_mode"]},
    "/stalled": {"answer": "stalled: 대기 중인 병합", "sources": ["draft_wiring"]},
    "/recurrences": {
        "rows": [
            {
                "newer": {
                    "source_path": "/vault/wiki/wiki-0576.md",
                    "subject": "relay sync",
                    "predicate": "incident",
                    "value": "relay synchronization issue",
                },
                "older": [{"source_path": "/vault/wiki/wiki-0536.md"}],
            }
        ],
        "days": 30,
        "max_distance": 0.2,
        "min_days_apart": 3,
    },
}


def _fetch(path: str, project: str = "") -> dict:
    return FETCH_DATA[path]


class CandidateOrderTests(unittest.TestCase):
    def setUp(self):
        self.registers = cr.collect_registers(_fetch)

    def test_priority_subjects_lead_in_their_own_register(self):
        order = cr.candidate_order(self.registers, priority=["f64_risk"])
        self.assertEqual(order[0], ("f64_risk", "risks"))

    def test_then_recurrences_then_risks_then_stalled(self):
        order = cr.candidate_order(self.registers, priority=["f64_risk"])
        subjects = [s for s, _ in order]
        self.assertEqual(
            subjects,
            [
                "f64_risk",
                "/vault/wiki/wiki-0536.md",
                "/vault/wiki/wiki-0576.md",
                "essential_mode",
                "draft_wiring",
            ],
        )

    def test_next_actions_never_appears(self):
        order = cr.candidate_order(self.registers)
        self.assertNotIn("next_action", [s for s, _ in order])
        self.assertNotIn("loop", [s for s, _ in order])

    def test_a_subject_appears_once_even_if_also_priority(self):
        order = cr.candidate_order(self.registers, priority=["essential_mode", "essential_mode"])
        subjects = [s for s, _ in order]
        self.assertEqual(subjects.count("essential_mode"), 1)


def _registers(sources: dict[str, list[str]]) -> cc.Registers:
    full = {name: sources.get(name, []) for name in cc.REGISTER_NAMES}
    return cc.Registers(texts={name: "" for name in cc.REGISTER_NAMES}, sources=full)


class MergeProjectCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.registers_a = _registers({"risks": ["f64_risk"], "stalled": ["a_only"]})
        self.registers_b = _registers({"risks": ["f64_risk"], "stalled": ["b_only"]})
        self.project_registers = {"proj-a": self.registers_a, "proj-b": self.registers_b}

    def test_priority_pair_leads_regardless_of_project_order(self):
        order = cr.merge_project_candidates(
            ["proj-a", "proj-b"], self.project_registers, priority=[("proj-b", "f64_risk")]
        )
        self.assertEqual(order[0], ("proj-b", "f64_risk", "risks"))

    def test_project_order_is_respected_after_priority(self):
        order = cr.merge_project_candidates(["proj-a", "proj-b"], self.project_registers)
        projects_in_order = [p for p, _, _ in order]
        # proj-a's own candidates all precede proj-b's — merge_project_candidates walks
        # project_order in sequence, each project's own candidate_order intact within it.
        first_b_index = projects_in_order.index("proj-b")
        self.assertTrue(all(p == "proj-a" for p in projects_in_order[:first_b_index]))

    def test_a_subject_is_deduplicated_across_projects(self):
        # f64_risk appears identically in both projects' registers (same fixture data) — it
        # must be spent once, not once per project, or the call budget scales with project
        # count instead of staying fixed at ADVISE_CALL_CAP.
        order = cr.merge_project_candidates(["proj-a", "proj-b"], self.project_registers)
        subjects = [s for _, s, _ in order]
        self.assertEqual(subjects.count("f64_risk"), 1)

    def test_unknown_project_in_the_order_is_skipped_not_raised(self):
        order = cr.merge_project_candidates(["proj-a", "ghost"], self.project_registers)
        self.assertTrue(all(p != "ghost" for p, _, _ in order))


if __name__ == "__main__":
    unittest.main()
