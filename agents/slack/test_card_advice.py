#!/usr/bin/env python3
"""card_advice — resolve_lang, build_advice_prompt, parse_advised.

Run: python3 agents/slack/test_card_advice.py
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import card_advice as ca  # noqa: E402


def _advice_json(bottleneck: str, advice: str, note: str, quote: str) -> str:
    return json.dumps(
        {
            "result": {
                "kind": "proposal",
                "bottleneck": bottleneck,
                "advice": advice,
                "evidence": [{"note": note, "quote": quote}],
            }
        },
        ensure_ascii=False,
    )


NOT_WORTH_JSON = json.dumps({"result": {"kind": "not_worth", "reason": "근거가 약하다"}}, ensure_ascii=False)


class ParseAdvisedTests(unittest.TestCase):
    """AC1: exact match, whitespace difference, not-a-substring, missing note, NotWorth."""

    NOTE = "/vault/wiki/wiki-0900.md"
    TEXT = "머리말\n문 응답 지연이 반복되는 원인은 타임아웃 설정이다\n끝"

    def _proposal_json(self, quote: str, note: str | None = None) -> str:
        return _advice_json("병목 열자 이상 문장", "조언 열자 이상 문장", note or self.NOTE, quote)

    def test_exact_match_computes_the_line(self):
        out = ca.parse_advised(
            self._proposal_json("문 응답 지연이 반복되는 원인은 타임아웃 설정이다"),
            {self.NOTE: self.TEXT},
            {},
        )
        self.assertIsInstance(out, ca.Advice)
        self.assertEqual(out.evidence[0].line, 2)

    def test_whitespace_difference_still_verifies(self):
        # The model retypes rather than copy-pastes — a run of whitespace collapsing to one
        # space is not a fabrication.
        quote = "문 응답    지연이  반복되는 원인은 타임아웃 설정이다"
        out = ca.parse_advised(self._proposal_json(quote), {self.NOTE: self.TEXT}, {})
        self.assertIsInstance(out, ca.Advice)
        self.assertEqual(out.evidence[0].line, 2)

    def test_not_a_substring_is_ungrounded(self):
        out = ca.parse_advised(
            self._proposal_json("이 문장은 노트에 없는 지어낸 인용문이다"), {self.NOTE: self.TEXT}, {}
        )
        self.assertIsInstance(out, ca.Ungrounded)

    def test_missing_note_is_ungrounded(self):
        out = ca.parse_advised(
            self._proposal_json(
                "문 응답 지연이 반복되는 원인은 타임아웃 설정이다", note="/vault/wiki/wiki-9999.md"
            ),
            {self.NOTE: self.TEXT},
            {},
        )
        self.assertIsInstance(out, ca.Ungrounded)

    def test_paraphrase_sharing_only_a_quote_prefix_is_ungrounded(self):
        # A prefix-matching bug (checking only the first MIN_QUOTE_CHARS characters) would
        # treat this as grounded because it shares a real line's opening — the tail is a
        # plausible paraphrase, fabricated, and never appears verbatim anywhere in the note.
        real_line = " ".join(self.TEXT.splitlines()[1].split())
        prefix = real_line[: ca.MIN_QUOTE_CHARS]
        quote = prefix + " 이하는 노트에 없는 완전히 다른 결말이다"
        self.assertGreaterEqual(len(quote), ca.MIN_QUOTE_CHARS)
        out = ca.parse_advised(self._proposal_json(quote), {self.NOTE: self.TEXT}, {})
        self.assertIsInstance(out, ca.Ungrounded)

    def test_not_worth_passes_through(self):
        out = ca.parse_advised(NOT_WORTH_JSON, {self.NOTE: self.TEXT}, {})
        self.assertIsInstance(out, ca.NotWorth)
        self.assertEqual(out.reason, "근거가 약하다")

    def test_bad_json_is_ungrounded_not_raised(self):
        out = ca.parse_advised("not json", {}, {})
        self.assertIsInstance(out, ca.Ungrounded)

    def test_schema_violation_is_ungrounded_not_raised(self):
        out = ca.parse_advised(json.dumps({"result": {"kind": "proposal"}}), {}, {})
        self.assertIsInstance(out, ca.Ungrounded)

    def test_superseded_names_ride_on_their_own_evidence_only(self):
        other = "/vault/wiki/wiki-0576.md"
        texts = {self.NOTE: self.TEXT, other: "머리말\nrelay synchronization issue recurred twice\n끝"}
        llm_json = json.dumps(
            {
                "result": {
                    "kind": "proposal",
                    "bottleneck": "병목 열자 이상 문장",
                    "advice": "조언 열자 이상 문장",
                    "evidence": [
                        {"note": self.NOTE, "quote": "문 응답 지연이 반복되는 원인은 타임아웃 설정이다"},
                        {"note": other, "quote": "relay synchronization issue recurred twice"},
                    ],
                }
            },
            ensure_ascii=False,
        )
        out = ca.parse_advised(llm_json, texts, {self.NOTE: ["wiki-0602"], other: []})
        self.assertEqual([e.superseded_by for e in out.evidence], [["wiki-0602"], []])
        control = ca.parse_advised(llm_json, texts, {})
        self.assertEqual([e.superseded_by for e in control.evidence], [[], []])


class ResolveLangTests(unittest.TestCase):
    def test_explicit_languages_pass_through(self):
        for lang in ("en", "ko", "ja"):
            self.assertEqual(ca.resolve_lang(lang), lang)

    def test_auto_and_anything_unmapped_falls_back_to_english(self):
        for raw in ("auto", "", "fr", "KO"):
            self.assertEqual(ca.resolve_lang(raw), "en")


class AdviceLangInstructionTests(unittest.TestCase):
    """AC3: build_advice_prompt attaches a language instruction, distill_core-style."""

    def test_ja_instruction_is_in_the_prompt(self):
        prompt = ca.build_advice_prompt("주어", "risks", [], "ja")
        self.assertIn(ca.ADVICE_LANG_INSTRUCTION["ja"], prompt)

    def test_ko_and_en_instructions_are_in_the_prompt(self):
        for lang in ("ko", "en"):
            prompt = ca.build_advice_prompt("주어", "risks", [], lang)
            self.assertIn(ca.ADVICE_LANG_INSTRUCTION[lang], prompt)

    def test_unmapped_lang_gets_the_fallback_instruction(self):
        prompt = ca.build_advice_prompt("주어", "risks", [], "auto")
        self.assertIn("same language as the past record above", prompt)


class SupersededPromptTests(unittest.TestCase):
    HITS = [
        {
            "source_path": "/vault/wiki/wiki-0576.md",
            "snippet": "relay sync",
            "superseded_by": ["/vault/wiki/wiki-0602.md"],
        },
        {"source_path": "/vault/wiki/wiki-0602.md", "snippet": "relay sync fixed", "superseded_by": []},
        {"source_path": "/vault/wiki/wiki-0101.md", "snippet": "필수 모드"},
    ]

    def test_a_superseded_hit_keeps_its_section_and_gets_its_own_marked_line(self):
        lines = ca.build_advice_prompt("주어", "risks", self.HITS, "ko").splitlines()
        self.assertEqual(
            [ln for ln in lines if ln.startswith("노트 경로:")][:1], ["노트 경로: /vault/wiki/wiki-0576.md"]
        )
        self.assertEqual(sum(ln.startswith("노트 경로:") for ln in lines), len(self.HITS))
        marked = [ln for ln in lines if "대체됨" in ln]
        self.assertEqual(len(marked), 1)
        self.assertIn("wiki-0602", marked[0])
        self.assertFalse(marked[0].startswith("노트 경로:"))
        control = ca.build_advice_prompt("주어", "risks", self.HITS[1:], "ko")
        self.assertNotIn("대체됨", control)


if __name__ == "__main__":
    unittest.main()
