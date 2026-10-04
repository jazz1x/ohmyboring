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
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "src"))

import card_types as cc  # noqa: E402
import card_view as cv  # noqa: E402

from ohmyboring.i18n import card as card_i18n  # noqa: E402

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
                self.assertEqual(
                    json.loads(button["value"]),
                    {
                        "lane": "advice",
                        "note": EXPECTED_NOTES[idx].rsplit("/", 1)[-1].removesuffix(".md"),
                    },
                )
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

    def test_two_projects_and_unassigned_group_in_first_seen_order_without_headings(self):
        proposals = [
            self._proposal("s1", "proj-a", "/vault/wiki/wiki-0001.md"),
            self._proposal("s2", "", "/vault/wiki/wiki-0002.md"),
            self._proposal("s3", "proj-b", "/vault/wiki/wiki-0003.md"),
            self._proposal("s4", "proj-a", "/vault/wiki/wiki-0004.md"),
        ]
        blocks = cv.build_blocks(proposals, lang="ko")
        self.assertEqual(blocks[0]["text"]["text"], "☀️ 오늘 제안 4")
        # v2: no per-project headings at all — grouping shows a project's rows together in
        # first-seen order (proj-a's second row, original idx 3, displays second with its
        # own 4/4 — k/N is the original list index, not the display position), and the
        # project name rides in each row's tag line instead.
        self.assertEqual(
            [
                b["text"]["text"]
                for b in blocks
                if b["type"] == "section" and b["text"]["text"].startswith("　\n")
            ],
            [],
        )
        tags = [
            b["elements"][0]["text"]
            for b in blocks
            if b["type"] == "context" and "`wiki-000" in b["elements"][0]["text"]
        ]
        self.assertEqual(
            tags,
            [
                "🧊 *정체* · proj-a · `wiki-0001` · 1/4",
                "🧊 *정체* · proj-a · `wiki-0004` · 4/4",
                "🧊 *정체* · `wiki-0002` · 2/4",
                "🧊 *정체* · proj-b · `wiki-0003` · 3/4",
            ],
        )
        action_rows = [b for b in blocks if b["type"] == "actions"]
        self.assertEqual(len(action_rows), 4)

    def test_advice_lane_has_one_heading_and_each_tag_carries_its_project(self):
        # 낱말 고침(2026-09-28): 칸 제목(짚어 둔 것)이 유일한 머릿말 — 프로젝트별 작은
        # 제목은 없고 프로젝트 이름은 각 제안 꼬리표가 실는다. 무소속("") 버킷은 꼬리표에
        # 프로젝트 구간 자체가 없다(「프로젝트 없음」 표기도 사라졌다).
        proposals = [
            self._proposal("s1", "proj-a", "/vault/wiki/wiki-0001.md"),
            self._proposal("s2", "", "/vault/wiki/wiki-0002.md"),
            self._proposal("s3", "proj-b", "/vault/wiki/wiki-0003.md"),
        ]
        repair = cc.Repair(
            subject="foodspring-front",
            variants=["foodspring front", "foodspring-front"],
            rows=3218,
            notes=212,
        )
        blocks = cv.build_blocks(proposals, repairs=[repair], repairs_total_groups=1, lang="ko")
        headers = [b["text"]["text"] for b in blocks if b["type"] == "header"]
        self.assertEqual(headers, ["☀️ 오늘 제안 3", "오늘 할 일", "짚어 둔 것"])
        # the advice lane's only heading: everything after the repair lane's header is the
        # advice lane, and it carries exactly one heading block — its own title
        advice_header_idx = next(
            i for i, b in enumerate(blocks) if b["type"] == "header" and b["text"]["text"] == "짚어 둔 것"
        )
        advice_headers = [b for b in blocks[advice_header_idx:] if b["type"] == "header"]
        self.assertEqual([b["text"]["text"] for b in advice_headers], ["짚어 둔 것"])
        # no section in the whole card pretends to be a lane or project heading
        self.assertEqual(
            [b for b in blocks if b["type"] == "section" and b["text"]["text"].startswith("　\n")],
            [],
        )
        self.assertNotIn("프로젝트 없음", _blocks_text(blocks))
        tags = [
            b["elements"][0]["text"]
            for b in blocks
            if b["type"] == "context" and "`wiki-000" in b["elements"][0]["text"]
        ]
        self.assertEqual(
            tags,
            [
                "🧊 *정체* · proj-a · `wiki-0001` · 1/3",
                "🧊 *정체* · `wiki-0002` · 2/3",
                "🧊 *정체* · proj-b · `wiki-0003` · 3/3",
            ],
        )

    def test_english_lang_buttons_and_unassigned_tag_has_no_project(self):
        proposals = [self._proposal("s1", "", "/vault/wiki/wiki-0001.md")]
        blocks = cv.build_blocks(proposals, lang="en")
        self.assertEqual(blocks[0]["text"]["text"], "☀️ Today's picks 1")
        tag = next(b for b in blocks if b["type"] == "context" and "wiki-0001" in _blocks_text([b]))
        self.assertEqual(tag["elements"][0]["text"], "🧊 *Stalled* · `wiki-0001` · 1/1")
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
        row = blocks[1:6]  # [0]=header, then the one row
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

    def test_superseded_evidence_label_names_the_newer_note_in_the_card_language(self):
        note = "/vault/wiki/wiki-0576.md"
        evidence = [
            cc.Evidence(note=note, quote="근거 인용문 열두자 이상", line=2, superseded_by=["wiki-0602"])
        ]
        for lang, label in (("ko", "대체됨"), ("en", "superseded")):
            blocks = cv.build_blocks([self._proposal(note=note, evidence=evidence)], lang=lang)
            texts = next(b for b in blocks if b["type"] == "rich_text")["elements"][0]["elements"]
            code_pieces = [t["text"] for t in texts if t.get("style") == {"code": True}]
            self.assertEqual(code_pieces, [f"\nwiki-0576 L2 · {label} → wiki-0602"])

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
        # every lane titles itself with Slack's header block — the card title, then the two
        # lane headers in order; no lane title is a same-size section among its rows
        headers = [b["text"]["text"] for b in blocks if b["type"] == "header"]
        self.assertEqual(headers[:3], ["☀️ 오늘 제안 1", "오늘 할 일", "짚어 둔 것"])
        header_blocks = [b for b in blocks if b["type"] == "header"]
        todo_idx = blocks.index(next(b for b in header_blocks if b["text"]["text"] == "오늘 할 일"))
        advice_idx = blocks.index(next(b for b in header_blocks if b["text"]["text"] == "짚어 둔 것"))
        self.assertLess(todo_idx, advice_idx)
        # the repair row's new wording: tag line carries the label·subject·k/N, the section
        # is the plain before→after sentence (no separate bold title)
        repair_tag = next(b for b in blocks if b["type"] == "context" and "🧩" in _blocks_text([b]))
        self.assertEqual(repair_tag["elements"][0]["text"], "🧩 *이름 맞추기* · `foodspring-front` · 1/1")
        repair_section = next(b for b in blocks if b["type"] == "section" and "3,218" in _blocks_text([b]))
        self.assertEqual(
            repair_section["text"]["text"],
            "`foodspring front` 로 적힌 3,218행(노트 212)을\nfoodspring-front 로 바꿉니다",
        )
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
            repair_results={0: cc.RepairDone(subject="foodspring-front", deleted_rows=5, reread_notes=2)},
            lang="ko",
        )
        mark = next(b for b in judged if b["type"] == "context" and "합침" in _blocks_text([b]))
        self.assertNotIn("소유자 노트", mark["elements"][0]["text"])
        held_judged = cv.build_blocks(
            [proposal],
            [verdict],
            repairs=[repair],
            repairs_total_groups=1,
            repair_results={
                0: cc.RepairDone(
                    subject="foodspring-front", deleted_rows=5, reread_notes=2, owner_held=["/v/wiki-0001.md"]
                )
            },
            lang="ko",
        )
        held_mark = next(b for b in held_judged if b["type"] == "context" and "합침" in _blocks_text([b]))
        self.assertIn("그대로 둔 소유자 노트 1: `/v/wiki-0001.md`", held_mark["elements"][0]["text"])
        self.assertIn("✓ 합침 — 지운 행 5 · 다시 읽는 노트 2", mark["elements"][0]["text"])

        # F2: a failed merge (door 502, sync error) still names the counts it committed —
        # never the plain "✓ 채택" mark, never silence.
        failed_judged = cv.build_blocks(
            [proposal],
            [verdict],
            repairs=[repair],
            repairs_total_groups=1,
            repair_results={
                0: cc.RepairFailed(
                    subject="foodspring-front", deleted_rows=5, reread_notes=2, reason="engine unreachable"
                )
            },
            lang="ko",
        )
        failed_mark = next(b for b in failed_judged if b["type"] == "context" and "실패" in _blocks_text([b]))
        self.assertIn(
            "✕ 합침 실패 — 지운 행 5 · 다시 읽은 노트 2 · engine unreachable",
            failed_mark["elements"][0]["text"],
        )

        unanswered_judged = cv.build_blocks(
            [proposal],
            [verdict],
            repairs=[repair],
            repairs_total_groups=1,
            repair_results={
                0: cc.RepairUnanswered(subject="foodspring-front", reason="door unreachable: timed out")
            },
            lang="ko",
        )
        unanswered_mark = next(
            b for b in unanswered_judged if b["type"] == "context" and "응답 없음" in _blocks_text([b])
        )
        self.assertIn("✕ 합침 응답 없음", unanswered_mark["elements"][0]["text"])
        self.assertIn("timed out", unanswered_mark["elements"][0]["text"])
        self.assertNotIn("지운 행 0", _blocks_text(unanswered_judged))

        # repairs empty (nothing to do today) but the lane is still active (merged yesterday) —
        # the 오늘 할 일 header must not appear, 짚어 둔 것 still does
        blocks_empty = cv.build_blocks(
            [proposal], repairs=[], repairs_total_groups=0, merged_yesterday_rows=7, lang="ko"
        )
        headers_empty = [b["text"]["text"] for b in blocks_empty if b["type"] == "header"]
        self.assertEqual(headers_empty, ["☀️ 오늘 제안 1", "짚어 둔 것"])

        # the default (no repairs args at all) renders exactly as the pre-existing single lane
        plain = cv.build_blocks([proposal], lang="ko")
        self.assertNotIn("오늘 할 일", _blocks_text(plain))
        self.assertNotIn("짚어 둔 것", _blocks_text(plain))

    def test_review_lane_sits_below_the_advice_lane_with_four_buttons(self):
        # 분류 칸: 조언 칸 아래 머리 + 행마다 kind_label · 이유 한 줄(옛 판정은 근거 없음) ·
        # 짧은 노트 이름 + 맞아요/아니에요/맡길게요/보류 네 버튼. 비면 머리도 없다 — 0건
        # 아침이 카드 모양을 바꾸면 안 된다(검증자 첫 질문).
        proposal = self._proposal()
        reviews = [
            cc.ProposedVerdict(session_id="s1", note="/vault/wiki/wiki-0700.md", kind="contested", at="t-1"),
            cc.ProposedVerdict(session_id="s2", note="/vault/wiki/wiki-0701.md", kind="used", at="t-2"),
        ]
        blocks = cv.build_blocks([proposal], reviews=reviews, lang="ko")
        lane_headers = [b["text"]["text"] for b in blocks if b["type"] == "header"]
        self.assertEqual(lane_headers, ["☀️ 오늘 제안 1", "에이전트가 가른 것"])
        advice_row = next(b for b in blocks if b["type"] == "actions")
        review_header = next(
            b for b in blocks if b["type"] == "header" and b["text"]["text"] == "에이전트가 가른 것"
        )
        self.assertGreater(blocks.index(review_header), blocks.index(advice_row))
        action_rows = [b for b in blocks if b["type"] == "actions"]
        self.assertEqual(len(action_rows), 3)  # one advice row + two review rows
        for row, expected_idx in zip(action_rows[1:], (1, 2)):
            buttons = {el["action_id"]: el for el in row["elements"]}
            self.assertEqual(
                set(buttons),
                {
                    f"card:{expected_idx}:do",
                    f"card:{expected_idx}:drop",
                    f"card:{expected_idx}:delegate",
                    f"card:{expected_idx}:defer",
                },
            )
        self.assertEqual(
            [el["text"]["text"] for el in action_rows[1]["elements"]],
            ["맞아요", "아니에요", "맡길게요", "보류"],
        )
        tag = next(b for b in blocks if b["type"] == "section" and "wiki-0700" in _blocks_text([b]))
        self.assertEqual(
            tag["text"]["text"],
            "이 세션에서 *이 노트가 틀렸다*는 말이 나왔어요.\n"
            "이유: 세션 끝 채점이 남긴 문장이 없어요 (옛 판정)\n"
            "노트: `wiki-0700` (제목 없음)\n"
            "작업: 세션 `s1` (세션 노트 없음)",
        )
        self.assertNotIn("/vault/", _blocks_text(blocks))

        judged = cv.build_blocks(
            [proposal],
            [
                cc.ButtonVerdict(idx=1, choice="do", user="U1", at="t"),
                cc.ButtonVerdict(idx=2, choice="drop", user="U1", at="t"),
                cc.ButtonVerdict(idx=3, choice="delegate", user="U1", at="t"),
                cc.ButtonVerdict(idx=4, choice="defer", user="U1", at="t"),
            ],
            reviews=[
                *reviews,
                cc.ProposedVerdict(session_id="s3", note="/vault/wiki/wiki-0702.md", kind="used", at="t-3"),
                cc.ProposedVerdict(
                    session_id="s4", note="/vault/wiki/wiki-0703.md", kind="contested", at="t-4"
                ),
            ],
            lang="ko",
        )
        marks = [
            b
            for b in judged
            if b["type"] == "context" and any(k in _blocks_text([b]) for k in ("✓", "↺", "⏸"))
        ]
        self.assertEqual(marks[0]["elements"][0]["text"], "✓ 맞음")
        self.assertEqual(marks[1]["elements"][0]["text"], "↺ 뒤집음 — 소유자 판정으로 틀린 노트")
        self.assertEqual(marks[2]["elements"][0]["text"], "✓ 맡김 — 에이전트 판정 그대로")
        self.assertEqual(marks[3]["elements"][0]["text"], "⏸ 보류 — 이레 뒤에 다시 올려요")

        plain = cv.build_blocks([proposal], reviews=[], lang="ko")
        self.assertNotIn("에이전트가 가른 것", _blocks_text(plain))

    def test_a_review_row_shows_the_reason_sentence_and_groups_its_works(self):
        # 이유 한 줄: 채점기가 잡은 문장이 그대로 보인다(200자 자름). 묶인 줄: 작업을 나열하고
        # 버튼 값은 묶인 세션 전부를 싣는다 — 판정은 전부에 간다. 근거 없는 옛 판정은 근거
        # 없음 한 줄로 정직하게.
        reasoned = cc.ProposedVerdict(
            session_id="s1",
            note="/vault/wiki/wiki-0700.md",
            kind="contested",
            at="t-1",
            reason="wiki-0700 의 폴 접근이 낡았다고 봤어요 — 소켓을 닫고 재활용하니 그렇습니다.",
        )
        strings = card_i18n.STRINGS["ko"]
        text = cv._review_tag_block(reasoned, strings)["text"]["text"]
        self.assertIn(
            strings["review_reason"].format(
                reason="wiki-0700 의 폴 접근이 낡았다고 봤어요 — 소켓을 닫고 재활용하니 그렇습니다."
            ),
            text,
        )

        grouped = cc.ProposedVerdict(
            session_id="s1",
            note="/vault/wiki/wiki-2498.md",
            kind="contested",
            at="t-1",
            reason="첫째 근거 문장입니다.",
            sessions=["s1", "s2", "s3"],
            works=["proj · 09-29 · 첫 작업", "", "proj · 09-30 · 셋째 작업"],
        )
        grouped_text = cv._review_tag_block(grouped, strings)["text"]["text"]
        self.assertIn("이유: 첫째 근거 문장입니다.", grouped_text)
        self.assertIn("작업: proj · 09-29 · 첫 작업; 세션 `s2` (세션 노트 없음); 외 1건", grouped_text)

        blocks = cv.build_blocks([self._proposal()], reviews=[grouped], lang="ko")
        row = next(
            b
            for b in blocks
            if b["type"] == "actions"
            and any(el.get("action_id", "").startswith("card:1:") for el in b["elements"])
        )
        value = json.loads(row["elements"][0]["value"])
        self.assertEqual(
            value,
            {
                "lane": "review",
                "session": "s1",
                "kind": "contested",
                "sessions": ["s1", "s2", "s3"],
                "note": "wiki-2498",
            },
        )

        bare = cc.ProposedVerdict(
            session_id="s1", note="/vault/wiki/wiki-0700.md", kind="contested", at="t-1"
        )
        bare_text = cv._review_tag_block(bare, strings)["text"]["text"]
        self.assertIn(strings["review_reason_none"], bare_text)
        self.assertIn("작업: 세션 `s1` (세션 노트 없음)", bare_text)

    def test_repair_and_review_buttons_carry_their_lane_values(self):
        # Every button's value is compact JSON naming its lane and what that lane needs —
        # card_press parses it back without any of the card's state. No empty value either:
        # Slack rejects the block outright when a present value has 0 chars.
        repair = cc.Repair(
            subject="foodspring-front",
            variants=["foodspring front", "foodspring-front"],
            rows=3218,
            notes=212,
        )
        reviews = [
            cc.ProposedVerdict(session_id="s1", note="/vault/wiki/wiki-0700.md", kind="contested", at="t-1"),
        ]
        blocks = cv.build_blocks(
            [self._proposal()],
            repairs=[repair],
            repairs_total_groups=1,
            reviews=reviews,
            lang="ko",
        )
        action_rows = [b for b in blocks if b["type"] == "actions"]
        self.assertEqual(len(action_rows), 3)  # repair row + advice row + review row
        expected = [
            {"lane": "repair", "subject": "foodspring-front"},
            {"lane": "advice", "note": "wiki-0900"},
            {"lane": "review", "session": "s1", "note": "wiki-0700", "kind": "contested"},
        ]
        for row, lane_data in zip(action_rows, expected):
            for button in row["elements"]:
                self.assertTrue(button["value"])
                self.assertEqual(json.loads(button["value"]), lane_data)

    def test_a_note_outside_the_vault_wiki_or_an_over_limit_value_refuses_the_card(self):
        # A card that cannot be answered must not be sent: the value carries the note in
        # label form, so a note not under /vault/wiki/ cannot ride it (ValueError at build),
        # and neither can a value past Slack's 2000-char cap.
        with self.assertRaises(ValueError):
            cv.build_blocks([self._proposal(note="/somewhere/note.md")], lang="ko")
        oversized = cc.Repair(
            subject="x" * 2100,
            variants=["a", "b"],
            rows=1,
            notes=1,
        )
        with self.assertRaises(ValueError):
            cv.build_blocks([self._proposal()], repairs=[oversized], repairs_total_groups=1, lang="ko")

    def test_a_subfolder_or_extensionless_note_refuses_the_card(self):
        # 라벨↔경로는 평탄한 /vault/wiki/<이름>.md 만 왕복한다 — 서브폴더 노트는 라벨로
        # 접히면 되돌릴 수 없고, .md 없는 이름은 라벨이 아니다. 못 되돌리는 카드는 보내지
        # 않는다.
        with self.assertRaises(ValueError):
            cv.build_blocks([self._proposal(note="/vault/wiki/sub/note.md")], lang="ko")
        with self.assertRaises(ValueError):
            cv.build_blocks([self._proposal(note="/vault/wiki/wiki-0576")], lang="ko")

    def test_review_lane_counts_against_the_block_limit(self):
        # r3.1: the review lane's header and rows live inside the same 50-block cap as the
        # advice rows — the advice loop reserves their tail, and review rows that still do
        # not fit are reported in an overflow line, never dropped silently. A mutant that
        # counts the review header outside the limit (the 51-block morning), or that breaks
        # out of the review loop without the overflow line, must each kill this.
        proposals = [
            self._proposal(
                subject=f"주어 {i}",
                note=f"/vault/wiki/wiki-{1000 + i}.md",
                evidence=[
                    cc.Evidence(note=f"/vault/wiki/wiki-{1000 + i}.md", quote="근거 인용문 열자 이상", line=1)
                ],
            )
            for i in range(12)
        ]
        reviews = [
            cc.ProposedVerdict(
                session_id=f"s{i}",
                note=f"/vault/wiki/wiki-07{i:02d}.md",
                kind="contested",
                at=f"t-{i}",
            )
            for i in range(3)
        ]
        blocks = cv.build_blocks(proposals, reviews=reviews, lang="ko")
        self.assertEqual(len(blocks), 47)
        self.assertLessEqual(len(blocks), cv.BLOCK_LIMIT)
        advice_rows = [b for b in blocks if b["type"] == "actions" and len(b["elements"]) == 3]
        review_rows = [b for b in blocks if b["type"] == "actions" and len(b["elements"]) == 4]
        self.assertEqual(len(advice_rows), 7)
        self.assertEqual(len(review_rows), 3)
        overflows = [
            b["elements"][0]["text"]
            for b in blocks
            if b["type"] == "context" and "더 있음" in _blocks_text([b])
        ]
        self.assertEqual(overflows, ["+5건 더 있음 (카드 상한)"])
        # shown advice rows + the overflow count == the lane's of-total: nothing double-
        # counted, nothing lost
        self.assertEqual(len(advice_rows) + 5, len(proposals))

        # when the lanes above already ate the cap, the review lane itself overflows —
        # its unshown rows land in the overflow line, not on the floor
        repairs = [
            cc.Repair(
                subject=f"split-{i}",
                variants=[f"split {i}", f"split-{i}"],
                rows=100 + i,
                notes=10 + i,
            )
            for i in range(10)
        ]
        blocks_full = cv.build_blocks(
            [proposals[0]], repairs=repairs, repairs_total_groups=10, reviews=reviews, lang="ko"
        )
        self.assertEqual(len(blocks_full), 50)
        self.assertLessEqual(len(blocks_full), cv.BLOCK_LIMIT)
        self.assertIn("에이전트가 가른 것", _blocks_text(blocks_full))
        review_rows_full = [b for b in blocks_full if b["type"] == "actions" and len(b["elements"]) == 4]
        self.assertEqual(len(review_rows_full), 1)
        overflows_full = [
            b["elements"][0]["text"]
            for b in blocks_full
            if b["type"] == "context" and "더 있음" in _blocks_text([b])
        ]
        self.assertEqual(
            overflows_full,
            ["+1건 더 있음 (카드 상한)", "+2건 더 있음 (카드 상한)"],
        )

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


class FailedStatusTests(unittest.TestCase):
    def test_a_long_failure_reason_is_cut_so_a_pressed_card_stays_in_budget(self):
        strings = card_i18n.STRINGS["ko"]
        text = cv.status_text(cc.Failed(reason="가" * 400), strings)
        self.assertEqual(
            text, strings["progress_failed"].format(reason="가" * (cv.TEXT_MAX_STEPS[-1] - 1) + "…")
        )


class CardBudgetTests(unittest.TestCase):
    """2026-10-04 08:00: a 35-block, 9,674-char card was refused (msg_blocks_too_long). Over
    budget, the prose shortens; every row and button stays."""

    def _reviews(self, n: int) -> list[cc.ProposedVerdict]:
        long = "가" * 300
        return [
            cc.ProposedVerdict(
                session_id=f"s{i}",
                note=f"/vault/wiki/wiki-{700 + i:04d}.md",
                kind="used",
                at=f"t-{i}",
                reason=long,
                note_title=long,
                work=long,
            )
            for i in range(n)
        ]

    def _actions(self, blocks) -> int:
        return sum(1 for b in blocks if b["type"] == "actions")

    def test_an_oversized_card_shortens_its_prose_and_keeps_every_row(self):
        reviews = self._reviews(7)
        full = cv.build_blocks([], reviews=reviews, lang="ko")
        self.assertGreater(cv.card_chars(full), cv.CARD_CHARS_BUDGET)
        fitted = cv.fit_blocks([], reviews=reviews, lang="ko")
        self.assertLessEqual(cv.card_chars(fitted), cv.CARD_CHARS_BUDGET)
        self.assertEqual(self._actions(fitted), self._actions(full))
        self.assertEqual(self._actions(fitted), 7)

    def test_rows_past_the_shortest_cut_are_counted_in_the_overflow_line(self):
        reviews = self._reviews(12)
        self.assertGreater(
            cv.card_chars(cv.build_blocks([], reviews=reviews, lang="ko", text_max=cv.TEXT_MAX_STEPS[-1])),
            cv.CARD_CHARS_BUDGET,
        )
        fitted = cv.fit_blocks([], reviews=reviews, lang="ko")
        self.assertLessEqual(cv.card_chars(fitted), cv.CARD_CHARS_BUDGET)
        shown = self._actions(fitted)
        overflow = card_i18n.STRINGS["ko"]["overflow_line"].format(n=12 - shown)
        self.assertLess(shown, 12)
        self.assertIn(overflow, _blocks_text(fitted))

    def test_an_advice_heavy_card_overflows_its_advice_rows_too(self):
        long = "조언 " * 120
        proposals = [
            cc.Proposal(
                subject=f"주어{i}",
                note=f"/vault/wiki/wiki-{900 + i:04d}.md",
                register="stalled",
                bottleneck=long,
                advice=long,
                evidence=[
                    cc.Evidence(
                        note=f"/vault/wiki/wiki-{900 + i:04d}.md", quote="근거 인용문 열두자 이상", line=1
                    )
                ],
            )
            for i in range(12)
        ]
        fitted = cv.fit_blocks(proposals, lang="ko")
        self.assertLessEqual(cv.card_chars(fitted), cv.CARD_CHARS_BUDGET)
        shown = self._actions(fitted)
        self.assertLess(shown, 12)
        self.assertIn(card_i18n.STRINGS["ko"]["overflow_line"].format(n=12 - shown), _blocks_text(fitted))

    def test_a_card_under_budget_is_left_as_built(self):
        reviews = self._reviews(1)
        self.assertEqual(
            cv.fit_blocks([], reviews=reviews, lang="ko"), cv.build_blocks([], reviews=reviews, lang="ko")
        )

    def test_a_card_that_cannot_fit_raises(self):
        orig = cv.CARD_CHARS_BUDGET
        cv.CARD_CHARS_BUDGET = 100
        try:
            with self.assertRaises(ValueError):
                cv.fit_blocks([], reviews=self._reviews(1), lang="ko")
        finally:
            cv.CARD_CHARS_BUDGET = orig


class RepairJudgmentBlockTests(unittest.TestCase):
    """The agent's 판정 on a repair row: verdict label(같은 이름·못 가름) + 이유 한 줄 +
    철자 목록 전부, in the card's own language. A row without a 판정 renders exactly as
    before — no block. A mutant that drops the judgment block, or renders only the head
    spelling, or pins one language's label for all three, must each kill these."""

    def _repair(self, verdict="same_name", reason="같은 이름의 갈라진 철자입니다") -> cc.Repair:
        return cc.Repair(
            subject="foodspring-front",
            variants=["foodspring front", "foodspring-front", "FoodspringFront"],
            rows=3218,
            notes=212,
            judgment=cc.RepairJudgment(
                subject="foodspring-front",
                variants=["FoodspringFront", "foodspring front", "foodspring-front"],
                verdict=verdict,
                reason=reason,
            ),
        )

    def _judgment_text(self, repair: cc.Repair, lang: str) -> str:
        blocks = cv.build_blocks([], repairs=[repair], repairs_total_groups=1, lang=lang)
        block = next(b for b in blocks if b["type"] == "context" and "🤖" in _blocks_text([b]))
        return block["elements"][0]["text"]

    def test_the_judgment_line_and_every_variant_show_in_each_language(self):
        cases = {
            "ko": (
                "🤖 에이전트 판정: *같은 이름*",
                "철자: `FoodspringFront`, `foodspring front`, `foodspring-front`",
            ),
            "en": (
                "🤖 Agent judged: *Same name*",
                "Spellings: `FoodspringFront`, `foodspring front`, `foodspring-front`",
            ),
            "ja": (
                "🤖 エージェント判定: *同じ名前*",
                "表記: `FoodspringFront`, `foodspring front`, `foodspring-front`",
            ),
        }
        for lang, (line, variants_line) in cases.items():
            text = self._judgment_text(self._repair(), lang)
            self.assertIn(line, text, lang)
            self.assertIn(
                "같은 이름의 갈라진 철자입니다", text, lang
            )  # 이유 한 줄 — the model's own sentence
            self.assertIn(variants_line, text, lang)  # 철자 목록 전부 — never the head spelling only

    def test_unsure_carries_its_own_label(self):
        text = self._judgment_text(self._repair(verdict="unsure", reason="어느 쪽인지 못 가르겠습니다"), "ko")
        self.assertIn("*못 가름*", text)
        self.assertIn("어느 쪽인지 못 가르겠습니다", text)

    def test_a_row_without_a_judgment_renders_unchanged(self):
        repair = cc.Repair(
            subject="foodspring-front", variants=["foodspring front", "foodspring-front"], rows=1, notes=1
        )
        blocks = cv.build_blocks([], repairs=[repair], repairs_total_groups=1, lang="ko")
        self.assertFalse(any("🤖" in _blocks_text([b]) for b in blocks))

    def test_mark_pressed_keeps_the_judgment_block_intact(self):
        blocks = cv.build_blocks([], repairs=[self._repair()], repairs_total_groups=1, lang="ko")
        press = cc.RepairPress(
            idx=0, choice="do", user="U_OWNER", card_ts="1.0", channel="C1", subject="foodspring-front"
        )
        marked = cv.mark_pressed(blocks, press, lang="ko")
        self.assertNotIsInstance(marked, cc.Rejected)
        self.assertIn("🤖 에이전트 판정: *같은 이름*", _blocks_text(marked))


class MarkPressedParityTests(unittest.TestCase):
    """The heart of slice ②: mark_pressed(blocks, press) on the no-verdict card must equal
    build_blocks with exactly that one verdict — for every lane and choice. The equality is
    whole-list, so it pins both directions: the pressed row renders exactly as build_blocks
    renders a judged row, and everything else is byte-identical (build_blocks has no text
    that counts judged rows — the header counts proposals, the head line counts repair
    groups — so nothing else may differ). Proposals group by first-seen project, so display
    order here is rows 1, 3, 2 while the idx space is repair 0, advice 1..3, review 4..5: a
    mark_pressed that matched by position instead of action_id would mark the wrong row and
    kill these tests."""

    def setUp(self):
        self.repair = cc.Repair(
            subject="foodspring-front",
            variants=["foodspring front", "foodspring-front"],
            rows=3218,
            notes=212,
        )
        self.proposals = [
            cc.Proposal(
                subject="주어 하나",
                note="/vault/wiki/wiki-0900.md",
                project="proj-a",
                register="stalled",
                bottleneck="첫째 병목 열자 이상 문장입니다",
                advice="첫째 조언 열자 이상 문장입니다",
                evidence=[
                    cc.Evidence(note="/vault/wiki/wiki-0900.md", quote="근거 인용문 열자 이상", line=1)
                ],
            ),
            cc.Proposal(
                subject="주어 둘",
                note="/vault/wiki/wiki-0536.md",
                project="proj-b",
                register="risks",
                bottleneck="둘째 병목 열자 이상 문장입니다",
                advice="둘째 조언 열자 이상 문장입니다",
                evidence=[
                    cc.Evidence(note="/vault/wiki/wiki-0536.md", quote="근거 인용문 열자 이상", line=2)
                ],
            ),
            cc.Proposal(
                subject="주어 셋",
                note="/vault/wiki/wiki-0576.md",
                project="proj-a",
                register="recurrences",
                bottleneck="셋째 병목 열자 이상 문장입니다",
                advice="셋째 조언 열자 이상 문장입니다",
                evidence=[
                    cc.Evidence(note="/vault/wiki/wiki-0576.md", quote="근거 인용문 열자 이상", line=3)
                ],
            ),
        ]
        self.reviews = [
            cc.ProposedVerdict(session_id="s1", note="/vault/wiki/wiki-0700.md", kind="contested", at="t-1"),
            cc.ProposedVerdict(session_id="s2", note="/vault/wiki/wiki-0701.md", kind="used", at="t-2"),
        ]
        self.result = cc.RepairDone(subject="foodspring-front", deleted_rows=5, reread_notes=2)

    def _card(self, verdicts=(), repair_results=None):
        return cv.build_blocks(
            self.proposals,
            verdicts,
            repairs=[self.repair],
            repairs_total_groups=1,
            repair_results=repair_results,
            reviews=self.reviews,
            lang="ko",
        )

    def _press(self, lane, idx, choice):
        common = dict(idx=idx, choice=choice, user="U_OWNER", card_ts="1.0", channel="C1")
        if lane == "repair":
            return cc.RepairPress(subject=self.repair.subject, **common)
        if lane == "advice":
            return cc.AdvicePress(note="wiki-0900", **common)
        review = self.reviews[idx - len(self.proposals) - 1]
        return cc.ReviewPress(session=review.session_id, note="wiki-0700", kind=review.kind, **common)

    def _lane_cases(self):
        return [
            ("repair", 0, "do", self.result),
            ("repair", 0, "defer", None),
            ("repair", 0, "drop", None),
            ("advice", 1, "do", None),
            ("advice", 2, "defer", None),
            ("advice", 3, "drop", None),
            ("review", 4, "do", None),
            ("review", 5, "drop", None),
            ("review", 4, "delegate", None),
            ("review", 5, "defer", None),
        ]

    def test_mark_pressed_equals_build_blocks_with_that_one_verdict(self):
        for lane, idx, choice, result in self._lane_cases():
            with self.subTest(lane=lane, idx=idx, choice=choice):
                blocks = self._card()
                marked = cv.mark_pressed(
                    blocks, self._press(lane, idx, choice), lang="ko", repair_result=result
                )
                verdict = cc.ButtonVerdict(idx=idx, choice=choice, user="U_OWNER", at="t")
                expected = self._card([verdict], repair_results={idx: result} if result is not None else None)
                self.assertEqual(marked, expected)
                # exactly one block changed — the row's actions block became its judged block
                diffs = [i for i, (a, b) in enumerate(zip(blocks, marked)) if a != b]
                actions_at = next(
                    i
                    for i, b in enumerate(blocks)
                    if b["type"] == "actions"
                    and any(el.get("action_id", "").startswith(f"card:{idx}:") for el in b["elements"])
                )
                self.assertEqual(diffs, [actions_at])
                # the source blocks are untouched — the refused-press path relies on it
                self.assertIn(f"card:{idx}:", _blocks_text(blocks))

    def test_parity_holds_in_every_display_language(self):
        for lang in ("ko", "en", "ja"):
            with self.subTest(lang=lang):
                blocks = cv.build_blocks(
                    self.proposals,
                    repairs=[self.repair],
                    repairs_total_groups=1,
                    reviews=self.reviews,
                    lang=lang,
                )
                marked = cv.mark_pressed(blocks, self._press("advice", 2, "drop"), lang=lang)
                expected = cv.build_blocks(
                    self.proposals,
                    [cc.ButtonVerdict(idx=2, choice="drop", user="U_OWNER", at="t")],
                    repairs=[self.repair],
                    repairs_total_groups=1,
                    reviews=self.reviews,
                    lang=lang,
                )
                self.assertEqual(marked, expected)

    def test_a_row_without_its_actions_block_is_refused(self):
        judged = self._card([cc.ButtonVerdict(idx=1, choice="do", user="U_OWNER", at="t")])
        again = cv.mark_pressed(judged, self._press("advice", 1, "do"), lang="ko")
        self.assertIsInstance(again, cc.Rejected)
        blocks = self._card()
        self.assertIsInstance(
            cv.mark_pressed(blocks, self._press("advice", 99, "do"), lang="ko"), cc.Rejected
        )
        self.assertIsInstance(cv.mark_pressed([], self._press("advice", 1, "do"), lang="ko"), cc.Rejected)

    def test_progress_replaces_only_its_row_and_a_later_outcome_settles_over_it(self):
        blocks = self._card()
        for lang in ("ko", "en", "ja"):
            pending = cv.mark_progress(blocks, 2, cc.Pending(), lang=lang)
            changed = [i for i, (a, b) in enumerate(zip(blocks, pending)) if a != b]
            self.assertEqual(len(pending), len(blocks))
            self.assertEqual(len(changed), 1)
            row = pending[changed[0]]
            self.assertEqual(row["block_id"], "card:2:status")
            self.assertEqual(row["elements"][0]["text"], card_i18n.STRINGS[lang]["progress_pending"])
            failed = cv.mark_progress(pending, 2, cc.Failed(reason="boom"), lang=lang)
            self.assertEqual(
                failed[changed[0]]["elements"][0]["text"],
                card_i18n.STRINGS[lang]["progress_failed"].format(reason="boom"),
            )
            self.assertEqual(failed[: changed[0]], blocks[: changed[0]])
            self.assertEqual(failed[changed[0] + 1 :], blocks[changed[0] + 1 :])
        self.assertIsInstance(cv.mark_progress(blocks, 99, cc.Pending(), lang="ko"), cc.Rejected)

    def test_a_review_row_names_the_note_and_the_work_in_every_language(self):
        review = cc.ProposedVerdict(
            session_id="53e83281-ea21",
            note="/vault/wiki/wiki-2216.md",
            kind="used",
            at="t",
            note_title="폴더 관례 <조사>",
            work="ohmyboring · 09-29 · 구조 개편 & 정리",
        )
        for lang in ("ko", "en", "ja"):
            strings = card_i18n.STRINGS[lang]
            text = cv._review_tag_block(review, strings)["text"]["text"]
            self.assertIn(strings["review_judged_used"], text)
            self.assertIn("폴더 관례 &lt;조사&gt;", text)
            self.assertIn("`wiki-2216`", text)
            self.assertIn("구조 개편 &amp; 정리", text)
            self.assertNotIn("/vault/", text)

    def test_a_huge_title_keeps_the_row_under_slacks_section_limit(self):
        review = cc.ProposedVerdict(
            session_id="s1",
            note="/vault/wiki/wiki-0700.md",
            kind="used",
            at="t",
            note_title="가" * 3100,
            work="w" * 3100,
        )
        text = cv._review_tag_block(review, card_i18n.STRINGS["ko"])["text"]["text"]
        self.assertLess(len(text), 3000)
        self.assertIn("…", text)

    def test_note_links_follow_the_setting_and_no_setting_leaves_the_card_as_it_was(self):
        from ohmyboring.config import FileLink, ObsidianLink

        proposal = self.proposals[0]
        review = cc.ProposedVerdict(session_id="s1", note="/vault/wiki/wiki-0700.md", kind="used", at="t")
        plain = cv.build_blocks([proposal], reviews=[review], lang="ko")
        self.assertEqual(plain, cv.build_blocks([proposal], reviews=[review], lang="ko", note_links=()))
        self.assertNotIn("obsidian://", _blocks_text(plain))
        links = (ObsidianLink(vault="my vault", folder="vault/wiki"), FileLink(folder="/Users/me/notes"))
        linked = _blocks_text(cv.build_blocks([proposal], reviews=[review], lang="ko", note_links=links))
        self.assertIn(
            "<obsidian://open?vault=my%20vault&file=vault%2Fwiki%2Fwiki-0700|Obsidian 으로 열기>", linked
        )
        self.assertIn("<vscode://file/Users/me/notes/wiki-0700.md|파일로 열기>", linked)
        cursor = _blocks_text(
            cv.build_blocks(
                [proposal], reviews=[review], lang="ko", note_links=(FileLink(folder="/n", editor="cursor"),)
            )
        )
        self.assertIn("<cursor://file/n/wiki-0700.md|파일로 열기>", cursor)
        advice_note = cv.note_label(proposal.note)
        self.assertIn(f"file=vault%2Fwiki%2F{advice_note}|", linked)

    def test_a_failure_keeps_the_buttons_and_a_retry_leaves_one_status_line(self):
        blocks = self._card()
        actions_at = cv._row_at(blocks, 2)
        failed = cv.mark_progress(blocks, 2, cc.Failed(reason="boom"), lang="ko")
        self.assertEqual(failed[actions_at]["block_id"], "card:2:status")
        self.assertEqual(failed[actions_at + 1 :], blocks[actions_at:])
        retried = cv.mark_progress(failed, 2, cc.Pending(), lang="ko")
        statuses = [b for b in retried if b.get("block_id") == "card:2:status"]
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["elements"][0]["text"], card_i18n.STRINGS["ko"]["progress_pending"])
        self.assertFalse(any(cv._is_row_actions(b, 2) for b in retried))
        self.assertEqual(len(retried), len(blocks))


if __name__ == "__main__":
    unittest.main()
