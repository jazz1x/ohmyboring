#!/usr/bin/env python3
"""card_repair_judge — build_prompt, parse, pick, judged_map, reviews_by_subject, run.

Run: python3 agents/slack/test_card_repair_judge.py   (no pytest dependency)

이름 맞추기 판정의 준비 자리:
  - build_prompt shapes the one question — canon 이름·철자 목록·행/노트 수, 그리고 이
    묶음에 대한 오너의 보류·거절 기록 — 세 고정 판정 중 하나의 JSON out.
  - parse is the boundary that never raises: JSON이 아니거나 verdict 어휘가 아니거나 이유가
    빈 답은 전부 RepairJudgeFailed — 모델이 못 답한 사실이지, 판정을 찍을 이유가 아니다.
  - pick is the day's queue: 문이 주는 행 수 순 그대로, 이미 판정된 (subject, 정렬된
    variants) 는 걷어내고 하루 상한 20개만 남긴다.
  - run is one run's skeleton: 고르기 → 호출·접기 → 사건 한 줄씩. 실패는 판정으로 안
    세고 repair_judge_failed 로 남는다.

Mutation targets: a build_prompt that drops the owner's past reviews kills the prompt
tests; a parse that guesses a verdict on a malformed answer kills the garbage tests; a
pick that ignores the judged map kills the cap/skip tests; a run that counts a failure as
a judgment (or drops the judge line) kills the run tests.
"""

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for extra in (REPO / "agents" / "slack", REPO / "agents" / "shared", REPO / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import card_repair_judge as crj  # noqa: E402
import card_types as cc  # noqa: E402

GROUP = {"subject": "next-step", "variants": ["next step", "next-step"], "rows": 268, "notes": 34}


def _group(subject: str, variants: list[str], rows: int = 10, notes: int = 1) -> dict:
    return {"subject": subject, "variants": variants, "rows": rows, "notes": notes}


def _entry(attrs: dict, observed_at: str = "2026-10-01T00:00:00+00:00") -> dict:
    return {"attributes": attrs, "observed_at": observed_at}


def _judged_attrs(subject: str, variants: list[str], verdict: str, reason: str = "판정 이유입니다") -> dict:
    return {
        "subject": subject,
        "variants": variants,
        "verdict": verdict,
        "reason": reason,
        "judge": "agent:repair-judge",
    }


class PromptTests(unittest.TestCase):
    def test_the_question_carries_the_group_and_the_owners_past_reviews(self):
        past = [
            {"choice": "defer", "card_ts": "1727480000.0001", "idx": 0, "subject": "next-step"},
            {"choice": "drop", "card_ts": "1727480000.0002", "idx": 1, "subject": "next-step"},
        ]
        prompt = crj.build_prompt(GROUP, past)
        self.assertIn("정규화 이름: next-step", prompt)
        self.assertIn("철자 목록: next step, next-step", prompt)
        self.assertIn("행 수: 268 · 노트 수: 34", prompt)
        # the owner's 보류·거절 ride into the prompt — the model weighs them, never overrides
        self.assertIn("소유자가 이 묶음에 남긴 기록", prompt)
        self.assertIn("- 보류 (카드 1727480000.0001, 줄 0)", prompt)
        self.assertIn("- 거절 (카드 1727480000.0002, 줄 1)", prompt)
        # the closed verdict vocabulary — the parse boundary's contract
        self.assertIn('"verdict": "same_name"', prompt)
        self.assertIn('verdict 는 "same_name · generic · unsure" 셋 중 하나만 써라', prompt)

    def test_no_past_reviews_no_history_section(self):
        prompt = crj.build_prompt(GROUP, [])
        self.assertNotIn("소유자가 이 묶음에 남긴 기록", prompt)


class ParseJudgmentTests(unittest.TestCase):
    def test_the_three_verdicts_fold_into_judgments_with_the_agent_line(self):
        for verdict in ("same_name", "generic", "unsure"):
            raw = json.dumps({"verdict": verdict, "reason": "철자 목록이 근거입니다"}, ensure_ascii=False)
            answer = crj.parse(raw, GROUP["subject"], list(GROUP["variants"]))
            self.assertIsInstance(answer, cc.RepairJudgment, verdict)
            self.assertEqual(answer.verdict, verdict)
            self.assertEqual(answer.judge, "agent:repair-judge")
            self.assertEqual(answer.subject, "next-step")
            self.assertEqual(answer.variants, ["next step", "next-step"])

    def test_a_fenced_completion_is_unwrapped(self):
        raw = '```json\n{"verdict": "unsure", "reason": "어느 쪽인지 모르겠습니다"}\n```'
        answer = crj.parse(raw, "next-step", ["next step", "next-step"])
        self.assertIsInstance(answer, cc.RepairJudgment)
        self.assertEqual(answer.verdict, "unsure")

    def test_garbage_answers_are_failures_never_a_guessed_verdict(self):
        cases = [
            ("not json at all", "모델 답이 JSON 이 아니다"),
            ('{"verdict": "dunno", "reason": "모름"}', "모델 판정을 알아듣지 못했다: 'dunno'"),
            ('{"verdict": "same_name"}', "모델이 이유를 남기지 않았다"),
            ('["same_name"]', "모델 답이 JSON 객체가 아니다"),
            ("", "모델 답이 JSON 이 아니다"),
        ]
        for raw, reason in cases:
            answer = crj.parse(raw, "next-step", ["next step", "next-step"])
            self.assertIsInstance(answer, cc.RepairJudgeFailed, raw)
            self.assertEqual(answer.subject, "next-step")
            self.assertEqual(answer.variants, ["next step", "next-step"])
            self.assertEqual(answer.reason, reason)

    def test_the_reason_is_capped_at_400(self):
        raw = json.dumps({"verdict": "same_name", "reason": "가" * 500}, ensure_ascii=False)
        answer = crj.parse(raw, "next-step", ["next step", "next-step"])
        self.assertIsInstance(answer, cc.RepairJudgment)
        self.assertEqual(len(answer.reason), 400)
        self.assertTrue(answer.reason.endswith("…"))


class PickTests(unittest.TestCase):
    def test_the_cap_holds_and_judged_groups_are_skipped_in_door_order(self):
        groups = [
            _group(f"subject-{i:02d}", [f"variant {i:02d}", f"subject-{i:02d}"], rows=100 - i)
            for i in range(25)
        ]
        judged = {
            ("subject-00", ("subject-00", "variant 00")): cc.RepairJudgment(
                subject="subject-00",
                variants=["subject-00", "variant 00"],
                verdict="generic",
                reason="흔한 말",
            ),
            ("subject-01", ("subject-01", "variant 01")): cc.RepairJudgment(
                subject="subject-01",
                variants=["subject-01", "variant 01"],
                verdict="same_name",
                reason="같은 이름",
            ),
        }
        picked = crj.pick(groups, judged, 20)
        self.assertEqual(len(picked), 20)
        self.assertEqual([g["subject"] for g in picked], [f"subject-{i:02d}" for i in range(2, 22)])
        # judged 묶음은 어디에도 없다 — 판정이 있으면 다시 부르지 않는다
        self.assertNotIn("subject-00", [g["subject"] for g in picked])
        self.assertNotIn("subject-01", [g["subject"] for g in picked])

    def test_a_changed_variants_group_is_judged_again(self):
        judged = {
            ("next-step", ("next step", "next-step")): cc.RepairJudgment(
                subject="next-step", variants=["next step", "next-step"], verdict="generic", reason="흔한 말"
            )
        }
        changed = _group("next-step", ["next step", "next-step", "next_steps"])
        self.assertEqual(crj.pick([changed], judged, 20), [changed])

    def test_fewer_than_cap_is_not_an_error(self):
        groups = [_group("a", ["a b", "a"]), _group("b", ["b c", "b"])]
        self.assertEqual(len(crj.pick(groups, {}, 20)), 2)
        self.assertEqual(crj.pick(groups, {}, 0), [])


class ReadFoldTests(unittest.TestCase):
    def test_the_newest_judgment_wins_per_group(self):
        entries = [
            _entry(
                _judged_attrs("next-step", ["next step", "next-step"], "unsure", "첫 판정"),
                "2026-09-20T00:00:00+00:00",
            ),
            _entry(
                _judged_attrs("next-step", ["next step", "next-step"], "generic", "뒤집은 판정"),
                "2026-10-01T00:00:00+00:00",
            ),
        ]
        out = crj.judged_map(entries)
        key = ("next-step", ("next step", "next-step"))
        self.assertEqual(out[key].verdict, "generic")
        self.assertEqual(out[key].reason, "뒤집은 판정")
        self.assertEqual(len(out), 1)

    def test_a_malformed_judged_row_raises(self):
        entries = [
            _entry({"subject": "next-step", "variants": ["next step", "next-step"], "verdict": "dunno"})
        ]
        with self.assertRaises(ValueError):
            crj.judged_map(entries)

    def test_reviews_fold_by_subject_and_a_malformed_row_raises(self):
        entries = [
            _entry({"card_ts": "1.0", "idx": 0, "choice": "defer", "subject": "next-step"}),
            _entry({"card_ts": "2.0", "idx": 1, "choice": "drop", "subject": "next-step"}),
            _entry({"card_ts": "3.0", "idx": 0, "choice": "defer", "subject": "main-page"}),
        ]
        out = crj.reviews_by_subject(entries)
        self.assertEqual([r["choice"] for r in out["next-step"]], ["defer", "drop"])
        self.assertEqual(len(out["main-page"]), 1)
        with self.assertRaises(ValueError):
            crj.reviews_by_subject([_entry({"choice": "defer"})])


class RunTests(unittest.TestCase):
    def test_run_records_judgments_and_failures_and_never_overruns_the_cap(self):
        groups = [
            _group(f"subject-{i:02d}", [f"variant {i:02d}", f"subject-{i:02d}"], rows=100 - i)
            for i in range(25)
        ]
        records: list[tuple[str, dict]] = []
        prompts: list[str] = []
        calls = {"n": 0}

        def invoke(prompt: str) -> str:
            calls["n"] += 1
            prompts.append(prompt)
            if calls["n"] == 1:
                raise OSError("ollama unreachable")
            return json.dumps(
                {"verdict": "same_name", "reason": "같은 이름의 갈라진 철자"}, ensure_ascii=False
            )

        judged_n, failed_n = crj.run(groups, {}, {}, invoke, lambda e, f: records.append((e, f)))
        # 상한 20 — 25개 중 20개만 불렀다
        self.assertEqual(calls["n"], 20)
        self.assertEqual(judged_n, 19)
        self.assertEqual(failed_n, 1)
        events = [name for name, _ in records]
        self.assertEqual(events.count("repair_judged"), 19)
        self.assertEqual(events.count("repair_judge_failed"), 1)
        # 실패 사건은 subject·variants·reason 을 갖는다 — 판정으로 안 세고 다음 날 다시 본다
        failed = next(fields for name, fields in records if name == "repair_judge_failed")
        self.assertEqual(failed["subject"], "subject-00")
        self.assertIn("ollama unreachable", failed["reason"])
        self.assertNotIn("judge", failed)
        # 판정 사건은 judge='agent:repair-judge' 를 탄다 — 오너가 나중에 뒤집을 수 있게
        judged = next(fields for name, fields in records if name == "repair_judged")
        self.assertEqual(judged["judge"], "agent:repair-judge")
        self.assertEqual(judged["verdict"], "same_name")

    def test_the_owners_past_reviews_ride_into_the_prompts(self):
        reviews = {"next-step": [{"choice": "drop", "card_ts": "1.0", "idx": 2, "subject": "next-step"}]}
        prompts: list[str] = []

        def invoke(prompt: str) -> str:
            prompts.append(prompt)
            return json.dumps({"verdict": "unsure", "reason": "모르겠습니다"}, ensure_ascii=False)

        crj.run([GROUP], {}, reviews, invoke, lambda e, f: None)
        self.assertEqual(len(prompts), 1)
        self.assertIn("- 거절 (카드 1.0, 줄 2)", prompts[0])

    def test_an_already_judged_group_costs_no_model_call(self):
        judged = {
            ("next-step", ("next step", "next-step")): cc.RepairJudgment(
                subject="next-step", variants=["next step", "next-step"], verdict="generic", reason="흔한 말"
            )
        }
        calls = {"n": 0}

        def invoke(prompt: str) -> str:
            calls["n"] += 1
            return '{"verdict": "same_name", "reason": "x"}'

        crj.run([GROUP], judged, {}, invoke, lambda e, f: None)
        self.assertEqual(calls["n"], 0)


if __name__ == "__main__":
    unittest.main()
