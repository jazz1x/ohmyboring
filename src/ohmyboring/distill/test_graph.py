#!/usr/bin/env python3
"""증류 그래프의 간선 — 가짜 노드로 어느 경로를 어떤 순서로 지났는지 못 박는다.

Run: python3 src/ohmyboring/distill/test_graph.py   (no pytest dependency)
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ohmyboring.distill.graph import Steps, build  # noqa: E402

NOTE = {"title": "t"}


def run(outcomes: dict[str, dict]):
    """outcomes: 노드 이름 → 그 노드가 돌려줄 상태 조각. 지난 노드를 순서대로 돌려준다."""
    trace: list[str] = []

    def node(name: str):
        def step(_state):
            trace.append(name)
            return outcomes.get(name, {})

        return step

    steps = Steps(**{field: node(field) for field in Steps.__dataclass_fields__})
    build(steps).invoke({"text": "", "ok": False})
    return trace


CLEAN = {
    "draft": {"parsed": {"title": "t"}, "wants_language_retry": False},
    "prepare": {"note": NOTE},
    "verify": {"verified": True},
}


class DistillEdgesTest(unittest.TestCase):
    def test_draft_none_ends_at_once(self):
        self.assertEqual(run({"draft": {"parsed": None}}), ["draft"])

    def test_skip_goes_to_skip_and_ends(self):
        self.assertEqual(run({"draft": {"parsed": {"skip": True}}}), ["draft", "skip"])

    def test_language_retry_is_an_edge_before_prepare(self):
        outcomes = {**CLEAN, "draft": {"parsed": {"title": "t"}, "wants_language_retry": True}}
        self.assertEqual(run(outcomes), ["draft", "retry_language", "prepare", "verify", "remember"])

    def test_clean_path_skips_repair(self):
        self.assertEqual(run(CLEAN), ["draft", "prepare", "verify", "remember"])

    def test_repair_success_reaches_remember(self):
        outcomes = {
            **CLEAN,
            "verify": {"verified": False},
            "repair_call": {"repaired": {"title": "t"}},
            "repair_prepare": {"repaired_note": NOTE},
            "repair_verify": {"repaired_verified": True},
        }
        self.assertEqual(
            run(outcomes),
            [
                "draft",
                "prepare",
                "verify",
                "repair_call",
                "repair_prepare",
                "repair_verify",
                "repair_passed",
                "remember",
            ],
        )

    def test_repair_verify_failure_ends_without_remember(self):
        outcomes = {
            **CLEAN,
            "verify": {"verified": False},
            "repair_call": {"repaired": {"title": "t"}},
            "repair_prepare": {"repaired_note": NOTE},
            "repair_verify": {"repaired_verified": False},
        }
        self.assertEqual(
            run(outcomes),
            ["draft", "prepare", "verify", "repair_call", "repair_prepare", "repair_verify", "repair_failed"],
        )

    def test_repair_that_returns_nothing_gives_up(self):
        outcomes = {**CLEAN, "verify": {"verified": False}, "repair_call": {"repaired": None}}
        self.assertEqual(run(outcomes), ["draft", "prepare", "verify", "repair_call", "give_up"])


class LangGraphJsonTest(unittest.TestCase):
    def test_registered_path_loads_the_module_level_graph_with_every_node(self):
        entry = json.loads((ROOT / "langgraph.json").read_text(encoding="utf-8"))["graphs"]["distill"]
        path, attr = entry.split(":")
        spec = importlib.util.spec_from_file_location("distill_graph_from_langgraph_json", ROOT / path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        nodes = set(getattr(module, attr).get_graph().nodes)
        self.assertEqual(nodes - {"__start__", "__end__"}, set(Steps.__dataclass_fields__))


if __name__ == "__main__":
    unittest.main()
