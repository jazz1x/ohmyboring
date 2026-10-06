#!/usr/bin/env python3
"""card_delegate — build_judge_prompt, parse_judgment, read_note_text, proposal_evidence.

Run: python3 agents/slack/test_card_delegate.py   (no pytest dependency)

「맡길게요」가 진짜 판단을 맡기려면 모델이 노트 본문·근거 문장·제안 종류를 읽고
맞다/틀리다와 이유 한 줄을 내야 한다. 이 모듈은 그 판정의 준비 자리:
  - build_judge_prompt shapes the one question — the claim (제안 종류), the sentence the
    session-end scorer caught it in (근거 문장), the note's own text — JSON right/wrong out.
  - parse_judgment is the boundary that never raises: JSON이 아니거나 verdict 어휘가 아니거나
    이유가 빈 답은 전부 DelegationFailed — 모델이 못 답한 사실이지, 쓰러질 이유가 아니다.
    맞다 = 제안 종류 그대로, 틀리다 = used↔contested 뒤집기는 이 경계 한 곳.
  - read_note_text / proposal_evidence are the two live reads (vault note, verdict_proposed
    사건) — missing on either side is None/"" values, never exceptions.

Mutation targets: letting parse_judgment guess a verdict on a malformed answer kills the
malformed-answer tests; flipping the right/wrong fold kills the kind assertions; stamping
the proposed kind without reading the verdict kills the fold tests; a prompt that omits the
evidence sentence or the note body kills the prompt tests.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for extra in (REPO / "agents" / "slack", REPO / "agents" / "shared", REPO / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import card_delegate as cd  # noqa: E402
import card_types as cc  # noqa: E402

NOTE = "/vault/wiki/wiki-0700.md"


def _entry(attrs: dict, observed_at: str = "2026-10-01T00:00:00+00:00") -> dict:
    return {"attributes": attrs, "observed_at": observed_at}


class JudgePromptTests(unittest.TestCase):
    def test_the_question_carries_the_claim_the_evidence_and_the_note(self):
        prompt = cd.build_judge_prompt(
            NOTE, "노트 본문입니다. 두 줄째.", "contested", "이 노트는 옛 것이라고 했어요."
        )
        self.assertIn("세션이 '이 노트가 틀렸다'고 주장했다", prompt)
        self.assertIn("근거 문장(세션에서 실제로 나온 문장):\n이 노트는 옛 것이라고 했어요.", prompt)
        self.assertIn(f"노트 경로: {NOTE}\n노트 본문입니다. 두 줄째.", prompt)
        # the JSON shapes and the closed verdict vocabulary — the parse boundary's contract
        self.assertIn('{"verdict": "right", "reason": "왜 맞는지 한 문장"}', prompt)
        self.assertIn('{"verdict": "wrong", "reason": "왜 틀리는지 한 문장"}', prompt)
        self.assertIn('verdict 는 "right" 아니면 "wrong" 만 써라', prompt)

    def test_frontmatter_is_not_in_the_prompt_and_a_long_body_is_cut(self):
        text = f"---\ntitle: 옛 접근\n---\n{'가' * (cd.NOTE_TEXT_PROMPT_MAX + 500)}"
        prompt = cd.build_judge_prompt(NOTE, text, "used", "썼다는 문장")
        self.assertNotIn("title: 옛 접근", prompt)
        self.assertIn("… (앞부분만)", prompt)
        self.assertNotIn("가" * cd.NOTE_TEXT_PROMPT_MAX, prompt)

    def test_an_empty_evidence_says_so_honestly(self):
        prompt = cd.build_judge_prompt(NOTE, "본문", "used", "")
        self.assertIn("근거 문장(세션에서 실제로 나온 문장):\n(채점이 남긴 문장 없음)", prompt)

    def test_both_kind_claims_name_the_sessions_claim(self):
        self.assertIn("썼다", cd.build_judge_prompt(NOTE, "본문", "used", "문장"))
        self.assertIn("틀렸다", cd.build_judge_prompt(NOTE, "본문", "contested", "문장"))


class ParseJudgmentTests(unittest.TestCase):
    def test_right_keeps_the_proposed_kind_wrong_flips_it(self):
        right = json.dumps({"verdict": "right", "reason": "본문에 그대로 있습니다"}, ensure_ascii=False)
        wrong = json.dumps(
            {"verdict": "wrong", "reason": "옛 기록이라 쓰이지 않았습니다"}, ensure_ascii=False
        )
        self.assertEqual(
            cd.parse_judgment(right, "used"),
            cc.DelegatedJudgment(kind="used", reason="본문에 그대로 있습니다"),
        )
        self.assertEqual(
            cd.parse_judgment(wrong, "used"),
            cc.DelegatedJudgment(kind="contested", reason="옛 기록이라 쓰이지 않았습니다"),
        )
        self.assertEqual(cd.parse_judgment(right, "contested").kind, "contested")
        self.assertEqual(cd.parse_judgment(wrong, "contested").kind, "used")

    def test_every_malformed_answer_is_a_failure_never_a_guessed_verdict(self):
        cases = [
            "not json at all",
            '{"verdict": "right"}',  # no reason
            '{"verdict": "right", "reason": "   "}',
            '{"verdict": "maybe", "reason": "애매합니다"}',
            '{"reason": "판정 없음"}',
            "[1, 2]",
            "",
        ]
        for raw in cases:
            out = cd.parse_judgment(raw, "used")
            self.assertIsInstance(out, cc.DelegationFailed, raw)
            self.assertNotIsInstance(out, cc.DelegatedJudgment, raw)
        # the proposed kind must survive untouched — a failure stamps nothing
        self.assertNotIn("used", repr(cd.parse_judgment("{}", "used")))

    def test_the_reason_is_one_line_capped_and_fences_do_not_hide_the_json(self):
        long_reason = "가" * (cd.DELEGATE_REASON_MAX + 100)
        fenced = f'```json\n{{"verdict": "right", "reason": "{long_reason}"}}\n```'
        out = cd.parse_judgment(fenced, "used")
        self.assertIsInstance(out, cc.DelegatedJudgment)
        self.assertEqual(len(out.reason), cd.DELEGATE_REASON_MAX)
        self.assertTrue(out.reason.endswith("…"))
        squeezed = cd.parse_judgment('{"verdict": "right", "reason": "줄이\\n\\n바뀐다"}', "used")
        self.assertEqual(squeezed.reason, "줄이 바뀐다")


class ReadNoteTextTests(unittest.TestCase):
    def test_the_vault_note_comes_back_and_a_missing_one_is_none(self):
        vault = Path(tempfile.mkdtemp(prefix="card-delegate-vault-"))
        wiki = vault / "wiki"
        wiki.mkdir()
        (wiki / "wiki-0700.md").write_text("---\ntitle: t\n---\n본문", encoding="utf-8")
        with mock.patch.dict(os.environ, {"BORING_VAULT_DIR": str(vault)}):
            self.assertEqual(cd.read_note_text("/vault/wiki/wiki-0700.md"), "---\ntitle: t\n---\n본문")
            self.assertIsNone(cd.read_note_text("/vault/wiki/wiki-9999.md"))
            self.assertIsNone(cd.read_note_text("/vault/wiki/../../etc/passwd.md"))


class ProposalEvidenceTests(unittest.TestCase):
    ROW = {
        "attributes": {
            "session_id": "sess-agent-1",
            "note": NOTE,
            "kind": "used",
            "reason": "채점이 잡은 문장입니다.",
        },
        "observed_at": "2026-10-01T00:00:00+00:00",
    }

    def _events(self, rows):
        return mock.patch.object(cd, "_live_events", side_effect=lambda name, hours: rows)

    def test_the_matching_rows_reason_comes_back(self):
        with self._events([self.ROW]):
            self.assertEqual(cd.proposal_evidence(["sess-agent-1"], NOTE, "used"), "채점이 잡은 문장입니다.")

    def test_kind_note_and_session_must_all_match(self):
        with self._events([self.ROW]):
            self.assertIsNone(cd.proposal_evidence(["sess-agent-1"], NOTE, "contested"))
            self.assertIsNone(cd.proposal_evidence(["sess-other"], NOTE, "used"))
            self.assertIsNone(cd.proposal_evidence(["sess-agent-1"], "/vault/wiki/wiki-0701.md", "used"))

    def test_a_grouped_press_takes_the_newest_sentence_and_empty_is_a_value(self):
        newer = json.loads(json.dumps(self.ROW))
        newer["observed_at"] = "2026-10-02T00:00:00+00:00"
        newer["attributes"]["session_id"] = "sess-agent-2"
        newer["attributes"]["reason"] = "둘째 문장입니다."
        with self._events([self.ROW, newer]):
            self.assertEqual(
                cd.proposal_evidence(["sess-agent-1", "sess-agent-2"], NOTE, "used"), "둘째 문장입니다."
            )
        no_sentence = json.loads(json.dumps(self.ROW))
        no_sentence["attributes"]["reason"] = ""
        with self._events([no_sentence]):
            self.assertEqual(cd.proposal_evidence(["sess-agent-1"], NOTE, "used"), "")
        no_reason_key = json.loads(json.dumps(self.ROW))
        del no_reason_key["attributes"]["reason"]
        with self._events([no_reason_key]):
            self.assertEqual(cd.proposal_evidence(["sess-agent-1"], NOTE, "used"), "")

    def test_no_matching_row_is_none(self):
        with self._events([]):
            self.assertIsNone(cd.proposal_evidence(["sess-agent-1"], NOTE, "used"))
        with self._events([{"attributes": "garbage"}, {"attributes": {"note": 1}}]):
            self.assertIsNone(cd.proposal_evidence(["sess-agent-1"], NOTE, "used"))


class OwnerCommentTests(unittest.TestCase):
    """The owner's words on a note ride the 맡길게요 prompt verbatim, and the judgment names the
    사건 ids it was given. Mutation targets: a prompt that drops the comments, or a
    parse_judgment that forgets the ids, kills these."""

    COMMENTS = [
        cc.OwnerComment(
            id=31, at="2026-10-06T02:00:00+00:00", text="이 노트는 옛 접근이 맞아요\n그래서 틀렸다고 봐요"
        ),
        cc.OwnerComment(id=30, at="2026-10-06T01:00:00+00:00", text="처음 남긴 말"),
    ]

    def test_the_prompt_carries_the_owners_words_verbatim_newest_first(self):
        prompt = cd.build_judge_prompt(NOTE, "본문", "used", "근거", self.COMMENTS)
        self.assertIn("소유자가 이 노트에 직접 남긴 말", prompt)
        self.assertIn("- 이 노트는 옛 접근이 맞아요\n  그래서 틀렸다고 봐요\n- 처음 남긴 말", prompt)
        self.assertLess(prompt.index("노트 경로"), prompt.index("소유자가 이 노트에"))
        self.assertLess(prompt.index("소유자가 이 노트에"), prompt.index("주장이 맞으면"))

    def test_without_comments_the_prompt_is_the_one_it_always_was(self):
        self.assertEqual(
            cd.build_judge_prompt(NOTE, "본문", "used", "근거"),
            cd.build_judge_prompt(NOTE, "본문", "used", "근거", []),
        )
        self.assertNotIn("소유자가 이 노트에", cd.build_judge_prompt(NOTE, "본문", "used", "근거"))

    def test_only_the_newest_few_comments_enter_the_prompt(self):
        many = [
            cc.OwnerComment(id=i, at=f"2026-10-06T{i:02d}:00:00+00:00", text=f"말 {i}")
            for i in range(20, 0, -1)
        ]
        prompt = cd.build_judge_prompt(NOTE, "본문", "used", "근거", many)
        self.assertEqual(prompt.count("\n- 말 "), cc.COMMENTS_IN_PROMPT)
        self.assertIn("- 말 20", prompt)
        self.assertNotIn("- 말 1\n", prompt)

    def test_the_judgment_names_the_comment_ids_it_was_given(self):
        raw = '{"verdict": "wrong", "reason": "오너 말대로 옛 접근이다"}'
        judged = cd.parse_judgment(raw, "used", [31, 30])
        self.assertEqual((judged.kind, judged.comment_ids), ("contested", [31, 30]))
        self.assertEqual(cd.parse_judgment(raw, "used").comment_ids, [])

    def test_comments_for_note_reads_the_owners_review_comments_of_that_note_only(self):
        def comment(ident, note, judge="owner"):
            return {
                "id": ident,
                "observed_at": f"2026-10-06T0{ident}:00:00+00:00",
                "attributes": {"judge": judge, "lane": "review", "note": note, "text": f"글 {ident}"},
            }

        rows = [comment(1, NOTE), comment(2, "/vault/wiki/wiki-0701.md"), comment(3, NOTE, judge="agent:x")]
        with mock.patch.object(cd, "fetch_events", return_value=rows) as fetch:
            got = cd.comments_for_note(NOTE)
        fetch.assert_called_once_with("card_comment", cc.COMMENT_WINDOW_HOURS)
        self.assertEqual([c.id for c in got], [1])

    def test_a_dead_door_or_a_clipped_page_is_an_error_not_an_empty_list(self):
        with mock.patch.object(cd.urllib.request, "urlopen", side_effect=OSError("refused")):
            with self.assertRaises(OSError):
                cd.fetch_events("card_comment", 168)

        class Page:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(self.body).encode()

        for body in ({"entries": [], "maybe_truncated": True}, {"nothing": 1}):
            with mock.patch.object(cd.urllib.request, "urlopen", return_value=Page(body)):
                with self.assertRaises(OSError):
                    cd.fetch_events("card_comment", 168)
        with mock.patch.object(cd.urllib.request, "urlopen", return_value=Page({"entries": [{"id": 1}]})):
            self.assertEqual(cd.fetch_events("card_comment", 168), [{"id": 1}])


if __name__ == "__main__":
    unittest.main()
