#!/usr/bin/env python3
"""card_advice_write — the pre-card step that writes the day's advice and the card's read of it.

Run: python3 agents/slack/test_card_advice_write.py   (no pytest dependency)

  - internal_names is the deterministic checker behind the one line (내부 이름·영문 식별자·
    wiki 번호·파일 경로·커밋 해시 없음).
  - revise is one rewrite pass with one goal and one line: a clean rewrite replaces the
    original, a still-flagged second rewrite or a rewrite that loses a number keeps the
    original and says so.
  - written_proposals is the card's whole read of the stored advice: today's newest run,
    this prompt version, rank order — nothing else.

Mutation targets: a checker that skips one pattern kills its flag test; a revise that stores
the rewrite when it is still flagged (or lost a number) kills the kept_original / lost_facts
tests; a reader that ignores day, run or prompt_version kills the reader tests.
"""

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for extra in (REPO / "agents" / "slack", REPO / "agents" / "shared", REPO / "src"):
    sys.path.insert(0, str(extra))

import card_advice_write as aw  # noqa: E402
import card_types as cc  # noqa: E402

DAY = "2026-10-19"


def _proposal(
    bottleneck: str = "문 응답이 자주 늦어지는데 원인을 아직 못 짚었다",
    advice: str = "오늘은 응답이 늦은 시각을 30건만 모아 본다",
    subject: str = "f64_risk",
) -> cc.Proposal:
    return cc.Proposal(
        subject=subject,
        note="/vault/wiki/wiki-0900.md",
        project="",
        register="risks",
        bottleneck=bottleneck,
        advice=advice,
        evidence=[cc.Evidence(note="/vault/wiki/wiki-0900.md", quote="문 응답 지연이 반복된다", line=2)],
    )


def _answer(bottleneck: str, advice: str) -> str:
    return json.dumps({"bottleneck": bottleneck, "advice": advice}, ensure_ascii=False)


class Invoker:
    """A model seam that hands out canned answers in order and remembers every prompt."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers[len(self.prompts) - 1]


class InternalNamesTests(unittest.TestCase):
    def test_each_kind_of_inside_talk_is_flagged(self):
        cases = {
            "wiki 번호": "wiki-0900 노트를 본다",
            "파일 경로": "/vault/wiki/note.md 를 연다",
            "상대 경로": "agents/slack/card.py 를 고친다",
            "커밋 해시": "ee049ff 에서 깨졌다",
            "스네이크 식별자": "advise_stats 를 본다",
            "케밥 이름": "repair-judge 가 돈다",
            "카멜 식별자": "buildGraph 를 부른다",
            "백틱 조각": "`card_fit` 값을 본다",
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertTrue(aw.internal_names(text), text)

    def test_plain_korean_with_numbers_and_dates_is_clean(self):
        text = "2026-10-19 아침에 3218건 중 212건을 다시 읽었다 (95%)"
        self.assertEqual(aw.internal_names(text), [])


class ReviseTests(unittest.TestCase):
    def test_a_clean_rewrite_replaces_the_original(self):
        invoke = Invoker(
            _answer("문 응답이 자꾸 늦는데 이유를 모른다", "오늘은 늦은 시각을 30건만 모아 본다")
        )
        out = aw.revise(_proposal(), invoke, "ko")
        self.assertEqual(out.revise, "rewritten")
        self.assertEqual(out.proposal.bottleneck, "문 응답이 자꾸 늦는데 이유를 모른다")
        self.assertEqual(len(invoke.prompts), 1)

    def test_a_flagged_rewrite_gets_one_more_pass_that_names_what_was_flagged(self):
        invoke = Invoker(
            _answer("advise_stats 가 자꾸 비는데 이유를 모른다", "오늘은 늦은 시각을 30건만 모아 본다"),
            _answer("조언 집계가 자꾸 비는데 이유를 모른다", "오늘은 늦은 시각을 30건만 모아 본다"),
        )
        out = aw.revise(_proposal(), invoke, "ko")
        self.assertEqual(out.revise, "rewritten")
        self.assertEqual(out.proposal.bottleneck, "조언 집계가 자꾸 비는데 이유를 모른다")
        self.assertEqual(len(invoke.prompts), 2)
        self.assertIn("advise_stats", invoke.prompts[1])

    def test_still_flagged_after_the_second_pass_keeps_the_original_and_says_so(self):
        original = _proposal()
        flagged = _answer("advise_stats 가 자꾸 빈다는 걸 알았다", "오늘은 늦은 시각을 30건만 모아 본다")
        invoke = Invoker(flagged, flagged)
        out = aw.revise(original, invoke, "ko")
        self.assertEqual(out.revise, "kept_original")
        self.assertEqual(out.proposal, original)
        self.assertEqual(len(invoke.prompts), 2)

    def test_a_rewrite_that_loses_a_number_keeps_the_original(self):
        original = _proposal()
        invoke = Invoker(
            _answer("문 응답이 자꾸 늦는데 이유를 모른다", "오늘은 늦은 시각을 몇 건만 모아 본다")
        )
        out = aw.revise(original, invoke, "ko")
        self.assertEqual(out.revise, "lost_facts")
        self.assertEqual(out.proposal, original)
        self.assertEqual(len(invoke.prompts), 1)

    def test_dropping_a_wiki_number_is_not_a_lost_fact(self):
        original = _proposal(advice="오늘은 wiki-0900 의 응답 시각을 30건만 모아 본다")
        invoke = Invoker(
            _answer("문 응답이 자꾸 늦는데 이유를 모른다", "오늘은 응답 시각을 30건만 모아 본다")
        )
        out = aw.revise(original, invoke, "ko")
        self.assertEqual(out.revise, "rewritten")

    def test_an_answer_that_is_not_the_two_sentences_keeps_the_original(self):
        original = _proposal()
        out = aw.revise(original, Invoker("모르겠다"), "ko")
        self.assertEqual(out.revise, "unusable")
        self.assertEqual(out.proposal, original)

    def test_the_prompt_carries_one_goal_and_one_line(self):
        invoke = Invoker(
            _answer("문 응답이 자꾸 늦는데 이유를 모른다", "오늘은 늦은 시각을 30건만 모아 본다")
        )
        aw.revise(_proposal(), invoke, "ko")
        prompt = invoke.prompts[0]
        self.assertEqual(prompt.count("목표:"), 1)
        self.assertEqual(prompt.count("선:"), 1)


def _entry(proposal: cc.Proposal, **over) -> dict:
    fields = aw.event_fields(proposal, day=DAY, run="2026-10-19T07:40:00+00:00", rank=0, revise="rewritten")
    fields.update(over)
    return {"observed_at": "2026-10-18T22:40:00Z", "attributes": fields}


class WrittenProposalsTests(unittest.TestCase):
    def test_a_stored_proposal_reads_back_whole(self):
        proposal = _proposal()
        self.assertEqual(aw.written_proposals([_entry(proposal)], DAY), [proposal])

    def test_rows_come_back_in_rank_order(self):
        first, second = _proposal(subject="a_subj"), _proposal(subject="b_subj")
        entries = [_entry(second, rank=1), _entry(first, rank=0)]
        self.assertEqual([p.subject for p in aw.written_proposals(entries, DAY)], ["a_subj", "b_subj"])

    def test_only_the_newest_run_of_the_day_stands(self):
        old = _entry(_proposal(subject="old_subj"), run="2026-10-19T07:00:00+00:00")
        new = _entry(_proposal(subject="new_subj"), run="2026-10-19T07:40:00+00:00")
        self.assertEqual([p.subject for p in aw.written_proposals([old, new], DAY)], ["new_subj"])

    def test_another_day_and_another_prompt_version_are_not_read(self):
        proposal = _proposal()
        entries = [
            _entry(proposal, day="2026-10-18"),
            _entry(proposal, prompt_version=cc.ADVICE_WRITTEN_PROMPT_VERSION + 1),
        ]
        self.assertEqual(aw.written_proposals(entries, DAY), [])

    def test_no_rows_is_an_empty_section_not_an_error(self):
        self.assertEqual(aw.written_proposals([], DAY), [])

    def test_a_malformed_row_raises_instead_of_vanishing(self):
        broken = _entry(_proposal())
        del broken["attributes"]["bottleneck"]
        with self.assertRaises(ValueError):
            aw.written_proposals([broken], DAY)


class _Graph:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def invoke(self, state):
        if self.error is not None:
            raise self.error
        return self.result


class MainTests(unittest.TestCase):
    def _main(self, graph, door="http://door"):
        import contextlib
        import io
        import os
        from unittest import mock

        import card

        env = {"BORING_DOOR_URL": door} if door else {}
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(card, "build_advice_graph", return_value=graph),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            rc = aw.main()
        return rc, out.getvalue(), err.getvalue()

    def test_it_says_what_it_wrote_and_how_each_rewrite_ended(self):
        rc, out, _ = self._main(_Graph({"advice_revisions": ["rewritten", "rewritten", "kept_original"]}))
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "[advice-write] written=3 kept_original=1 rewritten=2")

    def test_a_dead_door_stops_with_exit_3_and_one_line(self):
        rc, _, err = self._main(_Graph(error=OSError("door down")))
        self.assertEqual(rc, 3)
        self.assertIn("door down", err)

    def test_no_door_url_is_exit_2_before_any_graph_is_built(self):
        rc, _, err = self._main(_Graph({"advice_revisions": []}), door="")
        self.assertEqual(rc, 2)
        self.assertIn("BORING_DOOR_URL", err)


if __name__ == "__main__":
    unittest.main()
