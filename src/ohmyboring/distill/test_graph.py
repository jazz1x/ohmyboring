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
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ohmyboring.adapters import llm as llm_adapter  # noqa: E402
from ohmyboring.distill import polish as polish_engine  # noqa: E402
from ohmyboring.distill.graph import Steps, build  # noqa: E402
from ohmyboring.distill.nodes import polish as polish_node  # noqa: E402
from ohmyboring.distill.test_polish import TIDY, WALL  # noqa: E402

NOTE = {"title": "t"}


def run(outcomes: dict[str, dict], steps_overrides: dict | None = None, trace: list | None = None):
    """outcomes: 노드 이름 → 그 노드가 돌려줄 상태 조각. 지난 노드를 순서대로 돌려준다.

    steps_overrides: 이름 → 실제 노드 함수. 나머지는 trace 만 남기는 가짜.
    trace: 바깥에서 넘기면 그 리스트에도 적는다(진짜 노드에 얹은 얇은 자루와 합친다)."""
    trace = trace if trace is not None else []

    def node(name: str):
        def step(_state):
            trace.append(name)
            return outcomes.get(name, {})

        return step

    fields = {field: node(field) for field in Steps.__dataclass_fields__}
    fields.update(steps_overrides or {})
    final = build(Steps(**fields)).invoke({"text": "", "ok": False})
    return trace, final


CLEAN = {
    "draft": {"parsed": {"title": "t"}, "wants_language_retry": False},
    "prepare": {"note": NOTE},
    "verify": {"verified": True},
    "polish": {"polish_outcome": polish_engine.Polished(body="본문\n")},
}

REPAIR_OUTCOMES = {
    **CLEAN,
    "verify": {"verified": False},
    "repair_call": {"repaired": {"title": "t"}},
    "repair_prepare": {"repaired_note": NOTE},
}


class DistillEdgesTest(unittest.TestCase):
    def test_draft_none_ends_at_once(self):
        trace, _ = run({"draft": {"parsed": None}})
        self.assertEqual(trace, ["draft"])

    def test_skip_goes_to_skip_and_ends(self):
        trace, _ = run({"draft": {"parsed": {"skip": True}}})
        self.assertEqual(trace, ["draft", "skip"])

    def test_language_retry_is_an_edge_before_prepare(self):
        outcomes = {**CLEAN, "draft": {"parsed": {"title": "t"}, "wants_language_retry": True}}
        trace, _ = run(outcomes)
        self.assertEqual(trace, ["draft", "retry_language", "prepare", "verify", "polish", "remember"])

    def test_clean_path_passes_polish_before_remember(self):
        trace, _ = run(CLEAN)
        self.assertEqual(trace, ["draft", "prepare", "verify", "polish", "remember"])

    def test_polished_outcome_routes_straight_to_remember(self):
        outcomes = {**CLEAN, "polish": {"polish_outcome": polish_engine.Polished(body="새 본문\n")}}
        trace, _ = run(outcomes)
        self.assertEqual(trace, ["draft", "prepare", "verify", "polish", "remember"])

    def test_kept_lost_facts_routes_to_polish_retry_then_remember(self):
        outcomes = {
            **CLEAN,
            "polish": {"polish_outcome": polish_engine.LostFacts(reason="rewrite lost 1 fact(s): :7710")},
        }
        trace, _ = run(outcomes)
        self.assertEqual(trace, ["draft", "prepare", "verify", "polish", "polish_retry", "remember"])

    def test_kept_missed_shape_also_routes_to_retry(self):
        outcomes = {
            **CLEAN,
            "polish": {
                "polish_outcome": polish_engine.MissedShape(reason="rewrite misses the markdown shape")
            },
        }
        trace, _ = run(outcomes)
        self.assertEqual(trace, ["draft", "prepare", "verify", "polish", "polish_retry", "remember"])

    def test_kept_no_better_goes_straight_to_remember(self):
        outcomes = {**CLEAN, "polish": {"polish_outcome": polish_engine.NoBetter(reason="no better")}}
        trace, _ = run(outcomes)
        self.assertEqual(trace, ["draft", "prepare", "verify", "polish", "remember"])

    def test_repair_success_passes_polish_before_remember(self):
        outcomes = {**REPAIR_OUTCOMES, "repair_verify": {"repaired_verified": True}}
        trace, _ = run(outcomes)
        self.assertEqual(
            trace,
            [
                "draft",
                "prepare",
                "verify",
                "repair_call",
                "repair_prepare",
                "repair_verify",
                "repair_passed",
                "polish",
                "remember",
            ],
        )

    def test_repair_verify_failure_ends_without_polish(self):
        outcomes = {**REPAIR_OUTCOMES, "repair_verify": {"repaired_verified": False}}
        trace, _ = run(outcomes)
        self.assertEqual(
            trace,
            ["draft", "prepare", "verify", "repair_call", "repair_prepare", "repair_verify", "repair_failed"],
        )

    def test_repair_that_returns_nothing_gives_up_without_polish(self):
        outcomes = {**CLEAN, "verify": {"verified": False}, "repair_call": {"repaired": None}}
        trace, _ = run(outcomes)
        self.assertEqual(trace, ["draft", "prepare", "verify", "repair_call", "give_up"])


def _run_real_polish(answers, outcomes=None, note_body=WALL):
    """진짜 polish·polish_retry 노드 + 가짜 call_llm. (remember 에 도착한 본문, trace, 호출된 프롬프트, 최종 상태) 돌려준다."""
    calls: list[str] = []
    remembered: dict[str, str] = {}
    trace: list[str] = []

    def fake_call(prompt):
        calls.append(prompt)
        if len(calls) > len(answers):
            raise AssertionError(
                f"LLM called {len(calls)} times — more than the {len(answers)} scripted answers"
            )
        return answers[len(calls) - 1]

    def remember_node(state):
        remembered["body"] = state["note"]["body"]
        return {"ok": True}

    def traced(name, fn):
        def step(state):
            trace.append(name)
            return fn(state)

        return step

    base = {
        **CLEAN,
        **(outcomes or {}),
        "prepare": {"note": {"title": "t", "body": note_body}},
    }
    overrides = {
        "polish": traced("polish", polish_node.polish),
        "polish_retry": traced("polish_retry", polish_node.polish_retry),
        "remember": traced("remember", remember_node),
    }
    with mock.patch.object(llm_adapter, "call_llm", fake_call):
        trace, final = run(base, overrides, trace)
    return remembered.get("body"), trace, calls, final


class PolishNodeRoutingTests(unittest.TestCase):
    """진짜 polish 노드 + 가짜 LLM — 재시도 간선과 호출 횟수를 본다."""

    def test_first_polish_replaces_body_and_remembers_it(self):
        body, trace, calls, final = _run_real_polish([{"body": TIDY}])
        self.assertEqual(len(calls), 1)
        self.assertEqual(body, TIDY)
        self.assertEqual(trace, ["draft", "prepare", "verify", "polish", "remember"])
        self.assertIsInstance(final["polish_outcome"], polish_engine.Polished)

    def test_first_kept_lost_facts_retries_once_and_polished_body_reaches_remember(self):
        body, trace, calls, final = _run_real_polish([{"body": TIDY.replace(":7710", "")}, {"body": TIDY}])
        self.assertEqual(len(calls), 2, "재시도는 정확히 한 번")
        self.assertEqual(body, TIDY, "재시도로 다듬은 본문이 remember 에 들어간다")
        self.assertEqual(trace, ["draft", "prepare", "verify", "polish", "polish_retry", "remember"])
        self.assertIsInstance(final["polish_outcome"], polish_engine.Polished)

    def test_retry_reason_from_the_first_kept_reaches_the_second_prompt_verbatim(self):
        _, _, calls, _ = _run_real_polish(
            [{"body": TIDY.replace(":7710", "")}, {"body": TIDY}], note_body=WALL
        )
        self.assertEqual(len(calls), 2)
        self.assertNotIn("rejected by the checker", calls[0], "첫 프롬프트에는 거절 사유가 없다")
        self.assertIn("rejected by the checker", calls[1])
        self.assertIn(
            "rewrite lost 1 fact(s): 7710", calls[1], "빠진 사실 목록이 재시도 프롬프트에 그대로 붙는다"
        )

    def test_first_kept_lost_facts_still_kept_after_one_retry_keeps_original(self):
        original = WALL
        body, trace, calls, final = _run_real_polish(
            [{"body": TIDY.replace(":7710", "")}, {"body": TIDY.replace("wiki-2278", "")}]
        )
        self.assertEqual(len(calls), 2, "재시도는 최대 1회 — 셋째 호출은 없다")
        self.assertEqual(body, original, "재시도도 Kept 면 원본이 remember 에 들어간다")
        self.assertEqual(trace, ["draft", "prepare", "verify", "polish", "polish_retry", "remember"])
        self.assertIsInstance(final["polish_outcome"], polish_engine.LostFacts)

    def test_kept_no_better_does_not_retry(self):
        body, trace, calls, _ = _run_real_polish([{"body": WALL + "추가"}])
        self.assertEqual(len(calls), 1)
        self.assertEqual(body, WALL)
        self.assertEqual(trace, ["draft", "prepare", "verify", "polish", "remember"])

    def test_kept_too_long_does_not_retry(self):
        over = TIDY + "## 여유\n- " + "가" * 1500 + "\n"
        body, trace, calls, final = _run_real_polish([{"body": over}])
        self.assertEqual(len(calls), 1)
        self.assertEqual(body, WALL)
        self.assertIsInstance(final["polish_outcome"], polish_engine.TooLong)
        self.assertEqual(trace, ["draft", "prepare", "verify", "polish", "remember"])

    def test_already_readable_body_never_calls_the_model(self):
        body, trace, calls, final = _run_real_polish([], note_body=TIDY)
        self.assertEqual(len(calls), 0)
        self.assertEqual(body, TIDY)
        self.assertIsInstance(final["polish_outcome"], polish_engine.AlreadyReadable)
        self.assertEqual(trace, ["draft", "prepare", "verify", "polish", "remember"])

    def test_polish_runs_on_the_repair_passed_path_too(self):
        outcomes = {
            **REPAIR_OUTCOMES,
            "prepare": {"note": {"title": "t", "body": WALL}},
            "repair_prepare": {"repaired_note": {"title": "t", "body": WALL}},
            "repair_verify": {"repaired_verified": True},
        }
        body, trace, calls, _ = _run_real_polish([{"body": TIDY}], outcomes=outcomes)
        self.assertEqual(len(calls), 1)
        self.assertEqual(body, TIDY)
        self.assertEqual(
            trace,
            [
                "draft",
                "prepare",
                "verify",
                "repair_call",
                "repair_prepare",
                "repair_verify",
                "repair_passed",
                "polish",
                "remember",
            ],
        )

    def test_give_up_path_never_reaches_polish(self):
        def no_call(prompt):
            raise AssertionError("polish 경로가 아니면 LLM 을 부르지 않는다")

        outcomes = {**CLEAN, "verify": {"verified": False}, "repair_call": {"repaired": None}}
        overrides = {"polish": polish_node.polish, "polish_retry": polish_node.polish_retry}
        with mock.patch.object(llm_adapter, "call_llm", no_call):
            trace, _ = run(outcomes, overrides)
        self.assertEqual(trace, ["draft", "prepare", "verify", "repair_call", "give_up"])


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
