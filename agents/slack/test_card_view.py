#!/usr/bin/env python3
"""card_view — build_blocks, and card_i18n's display-string table it renders.

Run: python3 agents/slack/test_card_view.py
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import card_i18n  # noqa: E402
import card_types as cc  # noqa: E402
import card_view as cv  # noqa: E402

# The happy path's candidate order is: f64_risk (priority) → wiki-0536 → wiki-0576 →
# essential_mode → draft_wiring (recurrence sources are sorted, so 0536 precedes 0576).
# Three successes stop the loop after the first three.
EXPECTED_NOTES = ["/vault/wiki/wiki-0900.md", "/vault/wiki/wiki-0536.md", "/vault/wiki/wiki-0576.md"]


def _blocks_text(blocks) -> str:
    return json.dumps(blocks, ensure_ascii=False)


class BuildBlocksTests(unittest.TestCase):
    def setUp(self):
        self.proposals = [
            cc.Proposal(
                subject=f"주어 {i}",
                note=note,
                register="stalled",
                bottleneck=f"병목 설명 {i} 열자 이상",
                advice=f"오늘 할 일 {i} 열자 이상",
                evidence=[cc.Evidence(note=note, quote="근거 인용문 열두자 이상입니다", line=i + 1)],
            )
            for i, note in enumerate(EXPECTED_NOTES)
        ]

    def test_header_counts_the_proposals_and_rows_carry_note_paths(self):
        blocks = cv.build_blocks(self.proposals, lang="ko")
        self.assertEqual(blocks[0]["text"]["text"], f"☀️ 오늘 제안 {len(self.proposals)}")
        action_rows = [b for b in blocks if b["type"] == "actions"]
        self.assertEqual(len(action_rows), 3)
        for idx, row in enumerate(action_rows):
            buttons = {b["action_id"]: b for b in row["elements"]}
            self.assertEqual(set(buttons), {f"card:{idx}:do", f"card:{idx}:defer", f"card:{idx}:drop"})
            for button in row["elements"]:
                self.assertEqual(button["value"], EXPECTED_NOTES[idx].rsplit("/", 1)[-1].removesuffix(".md"))
            labels = [b["text"]["text"] for b in row["elements"]]
            self.assertEqual(labels, ["채택", "보류", "거절"])

    def test_each_proposal_shows_its_bottleneck_and_evidence_coordinate(self):
        blocks = cv.build_blocks(self.proposals, lang="ko")
        text = _blocks_text(blocks)
        self.assertIn("병목 설명 0", text)
        self.assertIn("wiki-0900 L1", text)
        self.assertIn("근거 인용문", text)

    def test_judged_row_shows_its_mark_instead_of_buttons(self):
        verdict = cc.ButtonVerdict(idx=1, choice="do", user="U_OWNER", at="t")
        blocks = cv.build_blocks(self.proposals, [verdict], lang="ko")
        action_rows = [b for b in blocks if b["type"] == "actions"]
        marks = [b for b in blocks if b["type"] == "context" and "✓" in _blocks_text([b])]
        self.assertEqual(len(action_rows), 2)
        self.assertEqual(len(marks), 1)
        self.assertIn("✓ 채택", marks[0]["elements"][0]["text"])
        self.assertNotIn("card:1:", _blocks_text(blocks))

    def test_confirmation_line_sits_under_the_header_when_there_is_one(self):
        confirmation = cc.Confirmation(
            total=3,
            done=["/vault/wiki/wiki-9999.md", "/vault/wiki/wiki-9998.md"],
            pending=["/vault/wiki/wiki-0900.md"],
        )
        blocks = cv.build_blocks(self.proposals, confirmation=confirmation, lang="ko")
        self.assertEqual(blocks[1]["type"], "context")
        self.assertEqual(blocks[1]["elements"][0]["text"], "지난 승인 3 · 했다 2 · 아직 1")

    def test_confirmation_line_counts_unknown_when_the_door_failed(self):
        confirmation = cc.Confirmation(
            total=2,
            done=[],
            pending=["/vault/wiki/wiki-0900.md"],
            unknown=[("/vault/wiki/wiki-9999.md", "claim-source answered 500")],
        )
        blocks = cv.build_blocks(self.proposals, confirmation=confirmation, lang="ko")
        self.assertEqual(blocks[1]["elements"][0]["text"], "지난 승인 2 · 했다 0 · 아직 1 · 확인불가 1")

    def test_no_confirmation_no_line(self):
        for none in (None, cc.Confirmation(total=0, done=[], pending=[])):
            blocks = cv.build_blocks(self.proposals, confirmation=none, lang="ko")
            self.assertNotIn("지난 승인", _blocks_text(blocks))

    def test_empty_proposals_still_renders_a_zero_header(self):
        blocks = cv.build_blocks([], lang="ko")
        self.assertEqual(blocks[0]["text"]["text"], "☀️ 오늘 제안 0")
        self.assertEqual(len(blocks), 1)


class ProjectGroupingBlocksTests(unittest.TestCase):
    def _proposal(self, subject: str, project: str, note: str) -> cc.Proposal:
        return cc.Proposal(
            subject=subject,
            note=note,
            project=project,
            register="stalled",
            bottleneck=f"{subject} 병목 열자 이상 문장",
            advice=f"{subject} 조언 열자 이상 문장",
            evidence=[cc.Evidence(note=note, quote="근거 인용문 열두자 이상입니다", line=1)],
        )

    def test_two_projects_and_unassigned_get_three_fixed_order_headers(self):
        proposals = [
            self._proposal("s1", "proj-a", "/vault/wiki/wiki-0001.md"),
            self._proposal("s2", "", "/vault/wiki/wiki-0002.md"),
            self._proposal("s3", "proj-b", "/vault/wiki/wiki-0003.md"),
            self._proposal("s4", "proj-a", "/vault/wiki/wiki-0004.md"),
        ]
        blocks = cv.build_blocks(proposals, lang="ko")
        self.assertEqual(blocks[0]["text"]["text"], "☀️ 오늘 제안 4")
        # v2: the project's own label is a section block ("　\n*label*"), not a header — only
        # the card's title uses "header". One per project in first-seen order.
        labels = [
            b["text"]["text"]
            for b in blocks
            if b["type"] == "section" and b["text"]["text"].startswith("　\n")
        ]
        self.assertEqual(labels, ["　\n*proj-a*", "　\n*프로젝트 없음*", "　\n*proj-b*"])
        action_rows = [b for b in blocks if b["type"] == "actions"]
        self.assertEqual(len(action_rows), 4)

    def test_english_lang_uses_the_unassigned_label_and_button_words(self):
        proposals = [self._proposal("s1", "", "/vault/wiki/wiki-0001.md")]
        blocks = cv.build_blocks(proposals, lang="en")
        self.assertEqual(blocks[0]["text"]["text"], "☀️ Today's picks 1")
        self.assertIn("No project", _blocks_text(blocks))
        action_row = next(b for b in blocks if b["type"] == "actions")
        labels = [el["text"]["text"] for el in action_row["elements"]]
        self.assertEqual(labels, ["Adopt", "Hold", "Reject"])


#: A run of 3+ ASCII words — a proper noun ("Slack", "JSON") or a symbol never matches this;
#: three real English words in a row does.
_LATIN_SENTENCE = re.compile(r"[A-Za-z]+(?:\s+[A-Za-z]+){2,}")
_HANGUL = re.compile(r"[가-힣]")
_KANA_OR_KANJI = re.compile(r"[぀-ヿ一-鿿]")


class CardI18nTests(unittest.TestCase):
    """AC3: en/ko/ja key parity, and no language bleeding into a table that isn't its own."""

    def test_key_sets_match_across_languages(self):
        en, ko, ja = card_i18n.STRINGS["en"], card_i18n.STRINGS["ko"], card_i18n.STRINGS["ja"]
        self.assertEqual(set(en), set(ko))
        self.assertEqual(set(en), set(ja))

    def test_ko_table_has_no_latin_sentence(self):
        for key, value in card_i18n.STRINGS["ko"].items():
            self.assertIsNone(_LATIN_SENTENCE.search(value), f"{key}: {value!r}")

    def test_en_table_has_no_hangul_or_kana(self):
        for key, value in card_i18n.STRINGS["en"].items():
            self.assertIsNone(_HANGUL.search(value), f"{key}: {value!r}")
            self.assertIsNone(_KANA_OR_KANJI.search(value), f"{key}: {value!r}")

    def test_ja_table_has_no_hangul(self):
        for key, value in card_i18n.STRINGS["ja"].items():
            self.assertIsNone(_HANGUL.search(value), f"{key}: {value!r}")

    def test_the_detectors_actually_catch_a_planted_violation(self):
        # A guard nobody has seen fail is not proven — plant one of each violation the three
        # tests above are meant to catch, on a copy, and check the same regex still fires.
        self.assertIsNotNone(_LATIN_SENTENCE.search("오늘 제안 please do the thing now"))
        self.assertIsNotNone(_HANGUL.search("Today's picks 오늘"))
        self.assertIsNotNone(_HANGUL.search("今日の提案 오늘"))


class CardV2ShapeTests(unittest.TestCase):
    """One test per AC3-AC8 — the v2 layout's shape, not a restatement of BuildBlocksTests."""

    def _proposal(self, **overrides) -> cc.Proposal:
        base = dict(
            subject="주어",
            note="/vault/wiki/wiki-0900.md",
            register="stalled",
            bottleneck="병목 문장 열자 이상입니다",
            advice="조언 문장 열자 이상입니다",
            evidence=[cc.Evidence(note="/vault/wiki/wiki-0900.md", quote="근거 인용문 열두자 이상", line=1)],
        )
        base.update(overrides)
        return cc.Proposal(**base)

    def test_row_order_is_divider_context_section_richtext_actions(self):
        # AC3: a mutant moving the register tag after the buttons must kill this.
        blocks = cv.build_blocks([self._proposal()], lang="ko")
        row = blocks[2:7]  # [0]=header, [1]=project label, then the one row
        self.assertEqual([b["type"] for b in row], ["divider", "context", "section", "rich_text", "actions"])

    def test_quote_block_is_a_top_level_rich_text_with_two_evidence_joined(self):
        # AC4: a mutant rendering the quote as a context block must kill this.
        proposal = self._proposal(
            evidence=[
                cc.Evidence(note="/vault/wiki/wiki-0900.md", quote="첫째 근거 인용문 열두자 이상", line=3),
                cc.Evidence(note="/vault/wiki/wiki-0536.md", quote="둘째 근거 인용문 열두자 이상", line=7),
            ]
        )
        blocks = cv.build_blocks([proposal], lang="ko")
        quote_block = next(b for b in blocks if b["type"] == "rich_text")
        self.assertEqual(quote_block["elements"][0]["type"], "rich_text_quote")
        texts = quote_block["elements"][0]["elements"]
        joined = "".join(t["text"] for t in texts)
        self.assertIn("첫째 근거", joined)
        self.assertIn("둘째 근거", joined)
        self.assertIn("\n\n", joined)
        code_pieces = [t["text"] for t in texts if t.get("style") == {"code": True}]
        self.assertEqual(code_pieces, ["\nwiki-0900 L3", "\nwiki-0536 L7"])

    def test_button_words_styles_and_verdict_marks_come_from_i18n(self):
        # AC5: a mutant dropping style="danger" from the reject button must kill this.
        proposal = self._proposal()
        action_row = next(b for b in cv.build_blocks([proposal], lang="ko") if b["type"] == "actions")
        by_choice = {el["action_id"].split(":")[-1]: el for el in action_row["elements"]}
        self.assertEqual(by_choice["do"]["text"]["text"], "채택")
        self.assertEqual(by_choice["do"]["style"], "primary")
        self.assertEqual(by_choice["drop"]["text"]["text"], "거절")
        self.assertEqual(by_choice["drop"]["style"], "danger")
        self.assertEqual(by_choice["defer"]["text"]["text"], "보류")
        self.assertNotIn("style", by_choice["defer"])

        verdict = cc.ButtonVerdict(idx=0, choice="drop", user="U1", at="t")
        judged = cv.build_blocks([proposal], [verdict], lang="ko")
        mark = next(b for b in judged if b["type"] == "context" and "✕" in _blocks_text([b]))
        self.assertIn("✕ 거절", mark["elements"][0]["text"])

    def test_register_tag_uses_localized_name_and_icon_in_three_languages(self):
        # AC6: recurrences → 🔁 + localized name, checked in ko/en/ja.
        proposal = self._proposal(register="recurrences", note="/vault/wiki/wiki-0576.md")
        expected = {"ko": "재발", "en": "Recurring", "ja": "再発"}
        for lang, label in expected.items():
            blocks = cv.build_blocks([proposal], lang=lang)
            tag = next(b for b in blocks if b["type"] == "context" and "wiki-0576" in _blocks_text([b]))
            text = tag["elements"][0]["text"]
            self.assertIn("🔁", text)
            self.assertIn(label, text)
        self.assertEqual(
            {k: cv.REGISTER_ICONS[k] for k in ("recurrences", "risks", "stalled", "next_actions")},
            {"recurrences": "🔁", "risks": "⚠️", "stalled": "🧊", "next_actions": "➡️"},
        )

    def test_no_vault_path_appears_anywhere_in_the_blocks(self):
        # AC7: a mutant putting the full note path back in the button value must kill this.
        proposal = self._proposal(
            project="proj-a",
            evidence=[cc.Evidence(note="/vault/wiki/wiki-0536.md", quote="근거 인용문 열두자 이상", line=4)],
        )
        blocks = cv.build_blocks([proposal], lang="ko")
        self.assertNotIn("/vault/", _blocks_text(blocks))

    def test_more_than_fifty_blocks_reports_overflow_instead_of_truncating_silently(self):
        # AC8: a mutant that silently truncates past 50 blocks must kill this.
        proposals = [
            self._proposal(
                subject=f"주어 {i}",
                note=f"/vault/wiki/wiki-{1000 + i}.md",
                evidence=[
                    cc.Evidence(
                        note=f"/vault/wiki/wiki-{1000 + i}.md", quote="근거 인용문 열두자 이상", line=1
                    )
                ],
            )
            for i in range(12)
        ]
        blocks = cv.build_blocks(proposals, lang="ko")
        self.assertLessEqual(len(blocks), cv.BLOCK_LIMIT)
        shown = len([b for b in blocks if b["type"] == "actions"])
        self.assertLess(shown, 12)
        overflow = next(b for b in blocks if b["type"] == "context" and "더 있음" in _blocks_text([b]))
        self.assertIn(str(12 - shown), overflow["elements"][0]["text"])

    def test_repair_lane_sits_above_the_advice_lane_and_shares_its_idx_space(self):
        # AC6: a mutant moving 「짚어 둔 것」 ahead of the repair rows, or one that leaves the
        # 「오늘 할 일」 header up when repairs is empty, or one that fails to offset the advice
        # button idx by n_repairs, must each kill this.
        repair = cc.Repair(
            subject="foodspring-front",
            variants=["foodspring front", "foodspring-front"],
            rows=3218,
            notes=212,
        )
        proposal = self._proposal()
        blocks = cv.build_blocks([proposal], repairs=[repair], repairs_total_groups=1, lang="ko")
        section_labels = [
            b["text"]["text"]
            for b in blocks
            if b["type"] == "section" and b["text"]["text"].startswith("　\n")
        ]
        self.assertEqual(section_labels[:2], ["　\n*오늘 할 일*", "　\n*짚어 둔 것*"])
        sections = [b for b in blocks if b["type"] == "section"]
        todo_idx = blocks.index(next(b for b in sections if b["text"]["text"] == "　\n*오늘 할 일*"))
        advice_idx = blocks.index(next(b for b in sections if b["text"]["text"] == "　\n*짚어 둔 것*"))
        self.assertLess(todo_idx, advice_idx)
        repair_action_row = next(b for b in blocks if b["type"] == "actions")
        self.assertEqual(
            {el["action_id"] for el in repair_action_row["elements"]},
            {"card:0:do", "card:0:defer", "card:0:drop"},
        )
        labels = {el["action_id"]: el["text"]["text"] for el in repair_action_row["elements"]}
        self.assertEqual(labels["card:0:do"], "실행")
        # the advice row's own button idx is offset by n_repairs=1, not 0
        advice_action_row = [b for b in blocks if b["type"] == "actions"][1]
        self.assertEqual(
            {el["action_id"] for el in advice_action_row["elements"]},
            {"card:1:do", "card:1:defer", "card:1:drop"},
        )
        # a judged repair row with a door result shows the merge numbers, not the plain mark
        verdict = cc.ButtonVerdict(idx=0, choice="do", user="U1", at="t")
        judged = cv.build_blocks(
            [proposal],
            [verdict],
            repairs=[repair],
            repairs_total_groups=1,
            repair_results={0: {"deleted_rows": 5, "reread_notes": 2}},
            lang="ko",
        )
        mark = next(b for b in judged if b["type"] == "context" and "합침" in _blocks_text([b]))
        self.assertIn("✓ 합침 — 지운 행 5 · 다시 읽은 노트 2", mark["elements"][0]["text"])

        # repairs empty (nothing to do today) but the lane is still active (merged yesterday) —
        # the 오늘 할 일 header must not appear, 짚어 둔 것 still does
        blocks_empty = cv.build_blocks(
            [proposal], repairs=[], repairs_total_groups=0, merged_yesterday_rows=7, lang="ko"
        )
        labels_empty = [
            b["text"]["text"]
            for b in blocks_empty
            if b["type"] == "section" and b["text"]["text"].startswith("　\n")
        ]
        self.assertEqual(labels_empty[:1], ["　\n*짚어 둔 것*"])

        # the default (no repairs args at all) renders exactly as the pre-existing single lane
        plain = cv.build_blocks([proposal], lang="ko")
        self.assertNotIn("오늘 할 일", _blocks_text(plain))
        self.assertNotIn("짚어 둔 것", _blocks_text(plain))

    def test_headline_shows_remaining_groups_and_optional_merged_yesterday(self):
        # AC7: a mutant dropping the remaining-groups count from the head line must kill this.
        proposal = self._proposal()
        with_merge = cv.build_blocks(
            [proposal], repairs_total_groups=42, merged_yesterday_rows=120, lang="ko"
        )
        headline = with_merge[1]["elements"][0]["text"]
        self.assertEqual(headline, "남은 묶음 42 · 어제 합친 행 120")

        without_merge = cv.build_blocks(
            [proposal], repairs_total_groups=42, merged_yesterday_rows=None, lang="ko"
        )
        headline_no_merge = without_merge[1]["elements"][0]["text"]
        self.assertEqual(headline_no_merge, "남은 묶음 42")
        self.assertNotIn("어제", headline_no_merge)


if __name__ == "__main__":
    unittest.main()
