#!/usr/bin/env python3
"""주간 그래프의 간선 — 가짜 노드로 어느 경로를 어떤 순서로 지났는지 못 박는다.

Run: python3 src/ohmyboring/weekly/test_graph.py   (no pytest dependency)
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ohmyboring.weekly.graph import Steps, build  # noqa: E402
from ohmyboring.weekly.state import AlreadyPosted, GenerationFailed, NothingToSay, Posted, PostFailed  # noqa: E402

TREND = {"read_week": {"projects": {"p": 1}}}
ENGINE = {"read_week": {"projects": {}}, "engine": {"answer": "a", "sources": []}}


def run(outcomes: dict[str, dict], deliver: bool) -> list[str]:
    """outcomes: 노드 이름 → 그 노드가 돌려줄 상태 조각. 지난 노드를 순서대로 돌려준다."""
    trace: list[str] = []

    def node(name: str):
        def step(_state):
            trace.append(name)
            return outcomes.get(name, {})

        return step

    steps = Steps(**{field: node(field) for field in Steps.__dataclass_fields__})
    build(steps).invoke({"deliver": deliver})
    return trace


class PreviewEdgesTest(unittest.TestCase):
    def test_enough_days_render_the_trend_and_stop(self):
        self.assertEqual(run(TREND, False), ["read_week", "render_trend"])

    def test_too_few_days_ask_the_engine_and_render_its_answer(self):
        self.assertEqual(run(ENGINE, False), ["read_week", "engine", "render_engine"])

    def test_an_engine_notice_is_already_the_output(self):
        outcomes = {"read_week": {"projects": {}}, "engine": {"stdout": "notice"}}
        self.assertEqual(run(outcomes, False), ["read_week", "engine"])

    def test_a_preview_never_consults_the_ledger_or_posts(self):
        trace = run(TREND, False) + run(ENGINE, False)
        self.assertFalse({"check_posted", "to_payload", "post", "record"} & set(trace))


class DeliverEdgesTest(unittest.TestCase):
    def test_already_posted_stops_at_the_ledger(self):
        outcomes = {"check_posted": {"outcome": AlreadyPosted("1.0")}}
        self.assertEqual(run(outcomes, True), ["check_posted"])

    def test_trend_then_payload_then_post_then_record(self):
        outcomes = {**TREND, "to_payload": {"payload": {"blocks": []}}}
        self.assertEqual(
            run(outcomes, True),
            ["check_posted", "read_week", "render_trend", "to_payload", "post", "record"],
        )

    def test_engine_answer_takes_the_same_road_to_slack(self):
        outcomes = {**ENGINE, "to_payload": {"payload": {"blocks": []}}}
        self.assertEqual(
            run(outcomes, True),
            ["check_posted", "read_week", "engine", "render_engine", "to_payload", "post", "record"],
        )

    def test_an_engine_notice_goes_to_the_payload_step_without_a_render(self):
        outcomes = {
            "read_week": {"projects": {}},
            "engine": {"stdout": "notice"},
            "to_payload": {"outcome": GenerationFailed("notice")},
        }
        self.assertEqual(run(outcomes, True), ["check_posted", "read_week", "engine", "to_payload"])

    def test_nothing_to_say_ends_before_the_post(self):
        outcomes = {**TREND, "to_payload": {"outcome": NothingToSay()}}
        self.assertEqual(run(outcomes, True), ["check_posted", "read_week", "render_trend", "to_payload"])

    def test_a_refused_post_is_not_recorded(self):
        outcomes = {
            **TREND,
            "to_payload": {"payload": {"blocks": []}},
            "post": {"outcome": PostFailed("no")},
        }
        self.assertEqual(
            run(outcomes, True), ["check_posted", "read_week", "render_trend", "to_payload", "post"]
        )

    def test_record_is_the_last_node_and_owns_the_outcome(self):
        outcomes = {
            **TREND,
            "to_payload": {"payload": {"blocks": []}},
            "record": {"outcome": Posted("1.0", None)},
        }
        self.assertEqual(run(outcomes, True)[-1], "record")


class LangGraphJsonTest(unittest.TestCase):
    def test_registered_path_loads_the_module_level_graph_with_every_node(self):
        entry = json.loads((ROOT / "langgraph.json").read_text(encoding="utf-8"))["graphs"]["weekly"]
        path, attr = entry.split(":")
        spec = importlib.util.spec_from_file_location("weekly_graph_from_langgraph_json", ROOT / path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        nodes = set(getattr(module, attr).get_graph().nodes)
        self.assertEqual(nodes - {"__start__", "__end__"}, set(Steps.__dataclass_fields__))


if __name__ == "__main__":
    unittest.main()
