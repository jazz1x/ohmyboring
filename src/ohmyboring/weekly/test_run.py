#!/usr/bin/env python3
"""주간 입구의 접기 — 결과 하나가 종료 코드와 한 줄로 접히고, 시간 초과·예외가 값이 된다.

Run: python3 src/ohmyboring/weekly/test_run.py   (no pytest dependency)
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ohmyboring.result import Err, Ok  # noqa: E402
from ohmyboring.weekly import run  # noqa: E402
from ohmyboring.weekly.nodes.payload import build_payload  # noqa: E402
from ohmyboring.weekly.report import report_of  # noqa: E402
from ohmyboring.weekly.state import (  # noqa: E402
    AlreadyPosted,
    GenerationFailed,
    NothingToSay,
    Posted,
    PostFailed,
    TimedOut,
)


class ReportTest(unittest.TestCase):
    def test_every_outcome_folds_to_its_code_and_lines(self):
        cases = [
            (AlreadyPosted("1.6"), 0, ("[weekly] already posted this week (ts=1.6)",), ()),
            (
                NothingToSay(),
                0,
                ("[weekly] 올릴 브리핑 없음 — 이번 주는 새로 짚을 진행/막힘 항목이 회수되지 않았어요",),
                (),
            ),
            (GenerationFailed("x"), 3, (), ("[weekly] 주간 브리핑 생성 실패: x",)),
            (TimedOut(300), 3, (), ("[weekly] 주간 브리핑 생성 시간 초과 (300초)",)),
            (PostFailed("no"), 1, (), ("[weekly] 슬랙 전송 실패: no",)),
            (Posted("1.0", None), 0, ("[weekly] posted ts=1.0",), ()),
            (
                Posted("1.0", "[weekly] weekly_card event not recorded: x"),
                0,
                ("[weekly] posted ts=1.0",),
                ("[weekly] weekly_card event not recorded: x",),
            ),
        ]
        for outcome, code, out, err in cases:
            report = report_of(outcome)
            self.assertEqual((report.code, report.out, report.err), (code, out, err), outcome)


class PayloadTest(unittest.TestCase):
    def test_json_with_blocks_is_a_payload(self):
        self.assertEqual(build_payload('{"blocks": []}'), Ok({"blocks": []}))

    def test_the_empty_message_is_nothing_to_say(self):
        text = "*t*\n\n이번 주는 새로 짚을 진행/막힘 항목이 회수되지 않았어요."
        self.assertEqual(build_payload(text), Ok(None))

    def test_other_text_is_the_briefings_own_failure_line(self):
        self.assertEqual(build_payload("⚠️  응답\n없음"), Err("⚠️ 응답 없음"))

    def test_json_without_blocks_is_not_a_payload(self):
        self.assertEqual(build_payload('{"text": "x"}'), Err('blocks 페이로드 아님: {"text": "x"}'))


class InvokeWithinTest(unittest.TestCase):
    def test_a_graph_that_never_returns_is_a_timeout_value(self):
        never = threading.Event()
        fake = mock.Mock(invoke=lambda _state: never.wait())
        with mock.patch.object(run, "graph", fake):
            self.assertEqual(run._invoke_within({}, 0.05), Err(TimedOut(0.05)))
        never.set()

    def test_an_exception_is_a_generation_failure_value(self):
        def boom(_state):
            raise AttributeError("'list' object has no attribute 'get'")

        with mock.patch.object(run, "graph", mock.Mock(invoke=boom)):
            self.assertEqual(
                run._invoke_within({}, 5),
                Err(GenerationFailed("AttributeError: 'list' object has no attribute 'get'")),
            )

    def test_a_final_state_passes_through(self):
        with mock.patch.object(run, "graph", mock.Mock(invoke=lambda state: {"outcome": 1, **state})):
            self.assertEqual(run._invoke_within({"a": 1}, 5), Ok({"outcome": 1, "a": 1}))


if __name__ == "__main__":
    unittest.main()
