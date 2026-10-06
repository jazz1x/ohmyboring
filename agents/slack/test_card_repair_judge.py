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
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for extra in (REPO / "agents" / "slack", REPO / "agents" / "shared", REPO / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import card_repair_judge as crj  # noqa: E402
import card_types as cc  # noqa: E402

GROUP = {"subject": "next-step", "variants": ["next step", "next-step"], "rows": 268, "notes": 34}


def ONE_FACT(variants: list[str]) -> list[dict]:
    return [{"predicate": "usage", "rows": 1, "value": "한 줄"}]


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
        "prompt_version": cc.REPAIR_JUDGE_PROMPT_VERSION,
    }


class PromptTests(unittest.TestCase):
    def test_the_question_carries_the_group_and_the_owners_past_reviews(self):
        past = [
            {"choice": "defer", "card_ts": "1727480000.0001", "idx": 0, "subject": "next-step"},
            {"choice": "drop", "card_ts": "1727480000.0002", "idx": 1, "subject": "next-step"},
        ]
        samples = [{"predicate": "action", "rows": 198, "value": "playwright 설치 확인"}]
        prompt = crj.build_prompt(GROUP, past, samples)
        self.assertIn("주제: next-step", prompt)
        self.assertIn("철자 목록: next step, next-step", prompt)
        self.assertIn("사실 수: 268 · 노트 수: 34", prompt)
        self.assertIn("- action · 198 · playwright 설치 확인", prompt)
        # the owner's 보류·거절 ride into the prompt — the model weighs them, never overrides
        self.assertIn("소유자가 이 묶음에 남긴 기록", prompt)
        self.assertIn("- 보류 (카드 1727480000.0001, 줄 0)", prompt)
        self.assertIn("- 거절 (카드 1727480000.0002, 줄 1)", prompt)
        # the closed verdict vocabulary — the parse boundary's contract
        self.assertIn('"verdict": "generic"', prompt)
        self.assertIn('verdict 는 "same_name · generic · unsure" 셋 중 하나만 써라', prompt)

    def test_no_past_reviews_no_history_section(self):
        prompt = crj.build_prompt(GROUP, [], [])
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
        key = ("next-step", ("next step", "next-step"))
        for order in (entries, entries[::-1]):
            out = crj.judged_map(order)
            self.assertEqual(out[key].verdict, "generic")
            self.assertEqual(out[key].reason, "뒤집은 판정")
            self.assertEqual(len(out), 1)

    def test_a_judgment_asked_under_another_prompt_is_not_read(self):
        current = _judged_attrs("kb-agent", ["kb-agent", "kb_agent"], "same_name")
        old = {**_judged_attrs("next-step", ["next step", "next-step"], "same_name"), "prompt_version": 1}
        unversioned = {k: v for k, v in old.items() if k != "prompt_version"}
        out = crj.judged_map([_entry(a, "2026-10-03T00:00:00+00:00") for a in (current, old, unversioned)])
        self.assertEqual(list(out), [("kb-agent", ("kb-agent", "kb_agent"))])

    def test_a_malformed_judged_row_raises(self):
        entries = [
            _entry(
                {**_judged_attrs("next-step", ["next step", "next-step"], "same_name"), "verdict": "dunno"}
            )
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
                return "ollama said nothing useful"
            return json.dumps(
                {"verdict": "same_name", "reason": "같은 이름의 갈라진 철자"}, ensure_ascii=False
            )

        judged_n, failed_n = crj.run(groups, {}, {}, invoke, lambda e, f: records.append((e, f)), ONE_FACT)
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

        crj.run([GROUP], {}, reviews, invoke, lambda e, f: None, ONE_FACT)
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

        crj.run([GROUP], judged, {}, invoke, lambda e, f: None, ONE_FACT)
        self.assertEqual(calls["n"], 0)

    def test_each_judged_group_brings_its_own_facts_into_its_prompt(self):
        asked: list[list[str]] = []
        prompts: list[str] = []

        def samples(variants: list[str]) -> list[dict]:
            asked.append(variants)
            return [{"predicate": "usage", "rows": 22, "value": f"{variants[0]} 의 쓰임"}]

        def invoke(prompt: str) -> str:
            prompts.append(prompt)
            return '{"verdict": "same_name", "reason": "한 제품의 성질"}'

        groups = [_group("spark-connect", ["spark-connect", "spark connect"]), GROUP]
        crj.run(groups, {}, {}, invoke, lambda e, f: None, samples)
        self.assertEqual(asked, [["spark connect", "spark-connect"], ["next step", "next-step"]])
        self.assertIn("- usage · 22 · spark connect 의 쓰임", prompts[0])
        self.assertIn("- usage · 22 · next step 의 쓰임", prompts[1])

    def test_a_group_with_no_facts_is_unsure_without_asking_the_model(self):
        records: list[tuple[str, dict]] = []
        calls = {"n": 0}

        def invoke(prompt: str) -> str:
            calls["n"] += 1
            return '{"verdict": "same_name", "reason": "철자로 추측"}'

        with_facts = _group("spark-connect", ["spark connect", "spark-connect"])
        crj.run(
            [GROUP, with_facts],
            {},
            {},
            invoke,
            lambda e, f: records.append((e, f)),
            lambda v: (
                [] if v == ["next step", "next-step"] else [{"predicate": "usage", "rows": 1, "value": "x"}]
            ),
        )
        self.assertEqual(calls["n"], 1)
        verdicts = {f["subject"]: (f["verdict"], f["reason"]) for _, f in records}
        self.assertEqual(verdicts["next-step"], ("unsure", crj.NO_FACTS_REASON))
        self.assertEqual(verdicts["spark-connect"][0], "same_name")

    def test_an_unreachable_model_stops_the_run_instead_of_stamping_failures(self):
        records: list[tuple[str, dict]] = []

        def invoke(prompt: str) -> str:
            raise ConnectionError("ollama unreachable")

        with self.assertRaises(ConnectionError):
            crj.run([GROUP], {}, {}, invoke, lambda e, f: records.append((e, f)), ONE_FACT)
        self.assertEqual(records, [])


class OwnerCommentTests(unittest.TestCase):
    """The owner's comments on a group reach its next judgment, which names their ids.
    Mutation targets: a run or build_prompt that never reads the comments kills the prompt
    and ids tests; a pick that never reopens a commented group kills the reopen tests."""

    COMMENTS = [
        cc.OwnerComment(
            id=41, at="2026-10-06T03:00:00+00:00", text="이건 우리 제품 이름이 맞아요\n합쳐 주세요"
        ),
        cc.OwnerComment(id=40, at="2026-10-06T02:00:00+00:00", text="먼저 남긴 말"),
    ]

    def test_the_prompt_carries_the_owners_words_verbatim_newest_first(self):
        prompt = crj.build_prompt(GROUP, [], ONE_FACT([]), self.COMMENTS)
        self.assertIn("소유자가 이 묶음에 직접 남긴 말", prompt)
        self.assertIn("- 이건 우리 제품 이름이 맞아요\n  합쳐 주세요\n- 먼저 남긴 말", prompt)
        self.assertLess(prompt.index("이 주제로 남은 사실"), prompt.index("소유자가 이 묶음에 직접"))
        self.assertLess(prompt.index("소유자가 이 묶음에 직접"), prompt.index("판정 셋"))
        self.assertNotIn("소유자가 이 묶음에 직접", crj.build_prompt(GROUP, [], ONE_FACT([])))

    def test_a_judgment_that_read_comments_records_their_ids(self):
        records: list[tuple[str, dict]] = []
        prompts: list[str] = []

        def invoke(prompt: str) -> str:
            prompts.append(prompt)
            return '{"verdict": "same_name", "reason": "오너가 제품 이름이라고 했다"}'

        crj.run(
            [GROUP],
            {},
            {},
            invoke,
            lambda e, f: records.append((e, f)),
            ONE_FACT,
            comments={"next-step": self.COMMENTS, "other": [cc.OwnerComment(id=99, at="t", text="남의 말")]},
        )
        self.assertIn("이건 우리 제품 이름이 맞아요", prompts[0])
        self.assertNotIn("남의 말", prompts[0])
        ((name, fields),) = records
        self.assertEqual((name, fields["comment_ids"]), ("repair_judged", [41, 40]))

    def test_a_judged_group_the_owner_commented_on_afterwards_is_judged_again(self):
        key = ("next-step", ("next step", "next-step"))
        entries = [
            _entry(
                _judged_attrs("next-step", ["next step", "next-step"], "generic"), "2026-10-06T01:00:00+00:00"
            )
        ]
        judged = crj.judged_map(entries)
        judged_at = crj.judged_times(entries)
        self.assertEqual(judged_at, {key: "2026-10-06T01:00:00+00:00"})
        later = {"next-step": [cc.OwnerComment(id=5, at="2026-10-06T02:00:00+00:00", text="아니에요")]}
        earlier = {"next-step": [cc.OwnerComment(id=4, at="2026-10-06T00:30:00+00:00", text="전에 한 말")]}
        self.assertEqual(crj.reopened_groups(judged_at, later), frozenset({key}))
        self.assertEqual(crj.reopened_groups(judged_at, earlier), frozenset())
        self.assertEqual(crj.reopened_groups(judged_at, {"other": later["next-step"]}), frozenset())
        self.assertEqual(crj.pick([GROUP], judged), [])
        self.assertEqual(crj.pick([GROUP], judged, reopened=frozenset({key})), [GROUP])
        calls = {"n": 0}

        def invoke(prompt: str) -> str:
            calls["n"] += 1
            return '{"verdict": "same_name", "reason": "오너 말대로다"}'

        crj.run(
            [GROUP],
            judged,
            {},
            invoke,
            lambda e, f: None,
            ONE_FACT,
            comments=later,
            reopened=frozenset({key}),
        )
        self.assertEqual(calls["n"], 1)

    def test_comments_by_subject_reads_the_owners_repair_comments_of_todays_groups(self):
        def comment(ident, subject, judge="owner", lane="repair"):
            return {
                "id": ident,
                "observed_at": f"2026-10-06T0{ident}:00:00+00:00",
                "attributes": {"judge": judge, "lane": lane, "subject": subject, "text": f"글 {ident}"},
            }

        entries = [
            comment(1, "next-step"),
            comment(2, "next-step", judge="agent:x"),
            comment(3, "next-step", lane="review"),
            comment(4, "elsewhere"),
        ]
        got = crj.comments_by_subject(entries, [GROUP])
        self.assertEqual({k: [c.id for c in v] for k, v in got.items()}, {"next-step": [1]})


class MainExitTests(unittest.TestCase):
    def _main(self, *answers: str) -> int:
        groups = [_group(f"subject-{i}", [f"subject {i}", f"subject-{i}"]) for i in range(len(answers))]
        replies = iter(answers)
        with (
            mock.patch.dict("os.environ", {"BORING_DOOR_URL": "http://door.invalid"}),
            mock.patch.object(crj, "_live_groups", return_value=groups),
            mock.patch.object(crj, "_live_events", return_value=[]),
            mock.patch.object(crj, "make_judge", return_value=lambda prompt: next(replies)),
            mock.patch.object(crj, "_live_record"),
            mock.patch.object(crj, "_live_samples", side_effect=ONE_FACT),
        ):
            return crj.main()

    def test_main_feeds_a_comment_left_after_the_judgment_into_the_next_one_and_records_its_id(self):
        # the whole path: card_comment 사건 read by name → the judged group reopened → the
        # prompt carries the owner's words → the repair_judged 사건 names the 사건 id
        by_name = {
            "repair_judged": [
                {
                    "id": 1,
                    "observed_at": "2026-10-06T01:00:00+00:00",
                    "attributes": _judged_attrs("next-step", ["next step", "next-step"], "same_name"),
                }
            ],
            "card_comment": [
                {
                    "id": 77,
                    "observed_at": "2026-10-06T02:00:00+00:00",
                    "attributes": {
                        "judge": "owner",
                        "lane": "repair",
                        "subject": "next-step",
                        "text": "이건 흔한 말이라 합치면 안 돼요",
                    },
                }
            ],
        }
        prompts: list[str] = []
        recorded: list[tuple[str, dict]] = []

        def judge(prompt: str) -> str:
            prompts.append(prompt)
            return '{"verdict": "generic", "reason": "오너가 흔한 말이라고 했다"}'

        with (
            mock.patch.dict("os.environ", {"BORING_DOOR_URL": "http://door.invalid"}),
            mock.patch.object(crj, "_live_groups", return_value=[GROUP]),
            mock.patch.object(crj, "_live_events", side_effect=lambda name, hours: by_name.get(name, [])),
            mock.patch.object(crj, "make_judge", return_value=judge),
            mock.patch.object(crj, "_live_record", side_effect=lambda e, f: recorded.append((e, f))),
            mock.patch.object(crj, "_live_samples", side_effect=ONE_FACT),
        ):
            self.assertEqual(crj.main(), 0)
        self.assertEqual(len(prompts), 1)
        self.assertIn("이건 흔한 말이라 합치면 안 돼요", prompts[0])
        ((name, fields),) = recorded
        self.assertEqual((name, fields["verdict"], fields["comment_ids"]), ("repair_judged", "generic", [77]))

    def test_only_a_run_where_every_call_failed_exits_non_zero(self):
        judged = '{"verdict": "generic", "reason": "흔한 말"}'
        self.assertEqual(self._main("not json"), 4)
        self.assertEqual(self._main(judged), 0)
        self.assertEqual(self._main(judged, "not json"), 0)


if __name__ == "__main__":
    unittest.main()
