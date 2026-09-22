#!/usr/bin/env python3
"""card_core's parsers and builders, and the g_card graph — no network, no LLM, no Slack.

The graph test drives build_graph with stub collaborators through MemorySaver + thread_id:
first invoke runs read_registers → cross_check → advise → resolve → post_card and pauses at
the interrupt with no verdicts, each Command(resume=…) records one verdict, and the run ends
after the last proposal is judged. The resolve stub resolves a fixed map of subjects,
mirroring the live collaborator's rule that a source already shaped like a note path resolves
to itself. The search/read_note stubs stand in for the door's /search and the host vault —
each candidate subject gets one canned hit whose note has known text, so parse_advised's
quote check has something real to check against.

Run: python3 agents/slack/test_card_core.py
"""

import contextlib
import io
import json
import os
import re
import sys
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import card  # noqa: E402
import card_core as cc  # noqa: E402
import card_i18n  # noqa: E402
from langgraph.types import Command  # noqa: E402

OWNER = "U_OWNER"
OTHER = "U_OTHER"
CARD_TS = "1.0"
CARD_CH = "C1"
SESSION = f"slack:{CARD_CH}:{CARD_TS}"

FETCH_DATA = {
    "/next_actions": {"answer": "next: 정리하고 재작성할 것", "sources": ["next_action", "loop"]},
    "/risks": {"answer": "risk: 엔진 문 응답 지연", "sources": ["f64_risk", "essential_mode"]},
    "/stalled": {"answer": "stalled: 대기 중인 병합", "sources": ["draft_wiring"]},
    "/recurrences": {
        "rows": [
            {
                "newer": {
                    "source_path": "/vault/wiki/wiki-0576.md",
                    "subject": "relay sync",
                    "predicate": "incident",
                    "value": "relay synchronization issue",
                },
                "older": [{"source_path": "/vault/wiki/wiki-0536.md"}],
            }
        ],
        "days": 30,
        "max_distance": 0.2,
        "min_days_apart": 3,
    },
}

# One hit per candidate subject — just enough for build_advice_prompt and note_texts.
CANDIDATE_HITS = {
    "f64_risk": [{"source_path": "/vault/wiki/wiki-0900.md", "snippet": "문 응답 지연", "claims": []}],
    "/vault/wiki/wiki-0576.md": [
        {"source_path": "/vault/wiki/wiki-0576.md", "snippet": "relay sync", "claims": []}
    ],
    "/vault/wiki/wiki-0536.md": [
        {"source_path": "/vault/wiki/wiki-0536.md", "snippet": "relay sync older", "claims": []}
    ],
    "essential_mode": [{"source_path": "/vault/wiki/wiki-0101.md", "snippet": "필수 모드", "claims": []}],
    "draft_wiring": [{"source_path": "/vault/wiki/wiki-0202.md", "snippet": "배선 초안", "claims": []}],
}

# Each note's own text — line 2 is what a valid quote must verify against.
NOTE_TEXTS = {
    "/vault/wiki/wiki-0900.md": "머리말\n문 응답 지연이 반복되는 원인은 타임아웃 설정이다\n끝",
    "/vault/wiki/wiki-0576.md": "머리말\nrelay synchronization issue recurred twice\n끝",
    "/vault/wiki/wiki-0536.md": "머리말\nearlier relay sync incident logged here\n끝",
    "/vault/wiki/wiki-0101.md": "머리말\nessential mode trims non essential paths\n끝",
    "/vault/wiki/wiki-0202.md": "머리말\ndraft wiring merge is still pending review\n끝",
}


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


def _advice_for(subject: str) -> str:
    """A grounded Advice response for `subject`, citing exactly the note its own search hit
    carries — so it verifies regardless of which position in the candidate queue it lands on."""
    note = CANDIDATE_HITS[subject][0]["source_path"]
    quote = NOTE_TEXTS[note].splitlines()[1]
    return _advice_json(f"{subject} 병목 한 문장 열자 이상", f"{subject} 오늘 할 일 열자 이상", note, quote)


# candidate_order's order for priority=["f64_risk"]: f64_risk → wiki-0536 (recurrences
# sorts before wiki-0576) → wiki-0576 → essential_mode → draft_wiring.
CANDIDATE_QUEUE = [
    "f64_risk",
    "/vault/wiki/wiki-0536.md",
    "/vault/wiki/wiki-0576.md",
    "essential_mode",
    "draft_wiring",
]
ADVICE = {subject: _advice_for(subject) for subject in CANDIDATE_QUEUE}

# subject → note path; subjects absent from the map are Unresolved. Recurrence-style
# paths resolve to themselves, like the live collaborator.
RESOLUTIONS = {
    "f64_risk": "/vault/wiki/wiki-0900.md",
    "loop": "/vault/wiki/wiki-0101.md",
    "essential_mode": "/vault/wiki/wiki-0101.md",
    "draft_wiring": "/vault/wiki/wiki-0202.md",
}

# The happy path's candidate order is: f64_risk (priority) → wiki-0536 → wiki-0576 →
# essential_mode → draft_wiring (recurrence sources are sorted, so 0536 precedes 0576).
# Three successes stop the loop after the first three.
EXPECTED_NOTES = ["/vault/wiki/wiki-0900.md", "/vault/wiki/wiki-0536.md", "/vault/wiki/wiki-0576.md"]

# Past approvals, newest first: the first still resolves from today's registers (아직),
# the other two no longer do (했다 2 · 아직 1 — asymmetric, so a done/pending swap shows).
PAST_APPROVED = [
    cc.PastApproved(session="slack:C1:1759000000.000001", note="/vault/wiki/wiki-0900.md", at="t-1"),
    cc.PastApproved(session="slack:C1:1758000000.000002", note="/vault/wiki/wiki-9999.md", at="t-2"),
    cc.PastApproved(session="slack:C1:1757000000.000003", note="/vault/wiki/wiki-9998.md", at="t-3"),
]
PAST_SESSION = "slack:C1:1759000000.000001"


def _fetch(path: str, project: str = "") -> dict:
    return FETCH_DATA[path]


def _payload(action_id: str, user: str = OWNER) -> dict:
    return {
        "type": "block_actions",
        "actions": [{"action_id": action_id, "value": "/vault/wiki/wiki-0576.md"}],
        "user": {"id": user},
        "message": {"ts": CARD_TS},
        "channel": {"id": CARD_CH},
    }


class Stubs:
    """The stub collaborators, recording every call. `failures` maps a subject to the
    Unresolved reason the door would have returned (5xx, unreachable). `responses` is the
    queue of advised JSON the propose stub hands out, one per call, in order — the caller
    picks it to match whichever candidates it wants to succeed, refuse, or go unworth."""

    def __init__(
        self,
        resolutions: dict[str, str] | None = None,
        approved=None,
        paths_resolve: bool = True,
        failures: dict[str, str] | None = None,
        responses: list[str] | None = None,
        active_project_names: list[str] | None = None,
        past_verdict_pairs: list[cc.PastVerdictPair] | None = None,
    ):
        self.resolutions = resolutions if resolutions is not None else RESOLUTIONS
        self.approved_items = approved if approved is not None else PAST_APPROVED
        self.paths_resolve = paths_resolve
        self.failures = failures or {}
        self.responses = (
            list(responses) if responses is not None else [ADVICE[s] for s in CANDIDATE_QUEUE[:3]]
        )
        self.active_project_names = active_project_names if active_project_names is not None else []
        self.past_verdict_pairs = past_verdict_pairs if past_verdict_pairs is not None else []
        self.propose_calls: list[str] = []
        self.search_calls: list[str] = []
        self.resolve_calls: list[tuple[str, str]] = []
        self.records: list[tuple[str, dict]] = []

    def search(self, subject: str) -> list[dict]:
        self.search_calls.append(subject)
        return CANDIDATE_HITS.get(subject, [])

    def read_note(self, note: str) -> str | None:
        return NOTE_TEXTS.get(note)

    def propose(self, prompt: str) -> str:
        self.propose_calls.append(prompt)
        idx = len(self.propose_calls) - 1
        return self.responses[idx] if idx < len(self.responses) else NOT_WORTH_JSON

    def resolve(self, subject: str, register: str):
        self.resolve_calls.append((subject, register))
        reason = self.failures.get(subject)
        if reason is not None:
            return cc.Unresolved(subject=subject, register=register, reason=reason)
        if self.paths_resolve and subject.startswith("/"):
            return cc.ResolvedNote(subject=subject, note=subject)
        note = self.resolutions.get(subject)
        if note is None:
            return cc.Unresolved(subject=subject, register=register, reason=cc.NO_CURRENT_CLAIM)
        return cc.ResolvedNote(subject=subject, note=note)

    def approved(self, since_hours: int):
        assert since_hours == 48
        return self.approved_items

    def record(self, event: str, fields: dict) -> None:
        self.records.append((event, fields))

    def active_projects(self, active_days: int) -> list[str]:
        assert active_days == card.PROJECT_ACTIVE_DAYS
        return self.active_project_names

    def past_verdicts(self, since_hours: int) -> list[cc.PastVerdictPair]:
        assert since_hours == cc.SUPPRESS_WINDOW_HOURS
        return self.past_verdict_pairs


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
        blocks = cc.build_blocks(self.proposals, lang="ko")
        self.assertEqual(blocks[0]["text"]["text"], f"☀️ 오늘 제안 {len(self.proposals)}")
        action_rows = [b for b in blocks if b["type"] == "actions"]
        self.assertEqual(len(action_rows), 3)
        for idx, row in enumerate(action_rows):
            buttons = {b["action_id"]: b for b in row["elements"]}
            self.assertEqual(set(buttons), {f"card:{idx}:do", f"card:{idx}:defer", f"card:{idx}:drop"})
            for button in row["elements"]:
                self.assertEqual(button["value"], EXPECTED_NOTES[idx])
                self.assertTrue(button["value"].startswith("/vault/wiki/"))
            labels = [b["text"]["text"] for b in row["elements"]]
            self.assertEqual(labels, ["해", "미뤄", "빼"])
        self.assertIn("주어 0", _blocks_text(blocks))

    def test_each_proposal_shows_its_bottleneck_and_evidence_coordinate(self):
        blocks = cc.build_blocks(self.proposals, lang="ko")
        text = _blocks_text(blocks)
        self.assertIn("병목 설명 0", text)
        self.assertIn("wiki-0900 L1", text)
        self.assertIn("근거 인용문", text)

    def test_judged_row_shows_its_mark_instead_of_buttons(self):
        verdict = cc.ButtonVerdict(idx=1, choice="do", user=OWNER, at="t")
        blocks = cc.build_blocks(self.proposals, [verdict], lang="ko")
        action_rows = [b for b in blocks if b["type"] == "actions"]
        marks = [b for b in blocks if b["type"] == "context" and "✓" in _blocks_text([b])]
        self.assertEqual(len(action_rows), 2)
        self.assertEqual(len(marks), 1)
        self.assertIn("✓ 해", marks[0]["elements"][0]["text"])
        self.assertNotIn("card:1:", _blocks_text(blocks))

    def test_confirmation_line_sits_under_the_header_when_there_is_one(self):
        confirmation = cc.Confirmation(
            total=3,
            done=["/vault/wiki/wiki-9999.md", "/vault/wiki/wiki-9998.md"],
            pending=["/vault/wiki/wiki-0900.md"],
        )
        blocks = cc.build_blocks(self.proposals, confirmation=confirmation, lang="ko")
        self.assertEqual(blocks[1]["type"], "context")
        self.assertEqual(blocks[1]["elements"][0]["text"], "지난 승인 3 · 했다 2 · 아직 1")

    def test_confirmation_line_counts_unknown_when_the_door_failed(self):
        confirmation = cc.Confirmation(
            total=2,
            done=[],
            pending=["/vault/wiki/wiki-0900.md"],
            unknown=[("/vault/wiki/wiki-9999.md", "claim-source answered 500")],
        )
        blocks = cc.build_blocks(self.proposals, confirmation=confirmation, lang="ko")
        self.assertEqual(blocks[1]["elements"][0]["text"], "지난 승인 2 · 했다 0 · 아직 1 · 확인불가 1")

    def test_no_confirmation_no_line(self):
        for none in (None, cc.Confirmation(total=0, done=[], pending=[])):
            blocks = cc.build_blocks(self.proposals, confirmation=none, lang="ko")
            self.assertNotIn("지난 승인", _blocks_text(blocks))

    def test_empty_proposals_still_renders_a_zero_header(self):
        blocks = cc.build_blocks([], lang="ko")
        self.assertEqual(blocks[0]["text"]["text"], "☀️ 오늘 제안 0")
        self.assertEqual(len(blocks), 1)


class ConfirmPastTests(unittest.TestCase):
    def test_note_still_in_today_register_is_pending_otherwise_done(self):
        out = cc.confirm_past(PAST_APPROVED, {"/vault/wiki/wiki-0900.md"})
        self.assertEqual(out.total, 3)
        self.assertEqual(out.done, ["/vault/wiki/wiki-9999.md", "/vault/wiki/wiki-9998.md"])
        self.assertEqual(out.pending, ["/vault/wiki/wiki-0900.md"])
        self.assertEqual(out.unknown, [])
        self.assertEqual(out.session, PAST_SESSION)

    def test_absence_is_done_only_when_the_door_answered(self):
        out = cc.confirm_past(PAST_APPROVED[1:], set())
        self.assertEqual(out.done, ["/vault/wiki/wiki-9999.md", "/vault/wiki/wiki-9998.md"])
        self.assertEqual(out.pending, [])

    def test_door_failure_leaves_the_past_approval_unknown(self):
        out = cc.confirm_past(PAST_APPROVED, set(), failures=["claim-source answered 500"])
        self.assertEqual(out.done, [])
        self.assertEqual(out.pending, [])
        self.assertEqual(
            out.unknown,
            [
                ("/vault/wiki/wiki-0900.md", "claim-source answered 500"),
                ("/vault/wiki/wiki-9999.md", "claim-source answered 500"),
                ("/vault/wiki/wiki-9998.md", "claim-source answered 500"),
            ],
        )

    def test_empty_past_is_a_zero_confirmation(self):
        out = cc.confirm_past([], {"/vault/wiki/wiki-0900.md"})
        self.assertEqual(out.total, 0)
        self.assertIsNone(out.session)


class ParseActionTests(unittest.TestCase):
    def test_valid_press_is_a_verdict(self):
        out = cc.parse_action(_payload("card:1:do"), owner_id=OWNER, n_proposals=3, at="t")
        self.assertIsInstance(out, cc.ButtonVerdict)
        self.assertEqual((out.idx, out.choice, out.user, out.at), (1, "do", OWNER, "t"))

    def test_unknown_action_id_is_rejected(self):
        out = cc.parse_action(_payload("card:1:nope"), owner_id=OWNER, n_proposals=3)
        self.assertIsInstance(out, cc.Rejected)
        out = cc.parse_action(_payload("reaction:x"), owner_id=OWNER, n_proposals=3)
        self.assertIsInstance(out, cc.Rejected)

    def test_missing_proposal_is_rejected(self):
        out = cc.parse_action(_payload("card:7:do"), owner_id=OWNER, n_proposals=3)
        self.assertIsInstance(out, cc.Rejected)

    def test_non_owner_press_is_rejected_only_when_owner_configured(self):
        out = cc.parse_action(_payload("card:0:do", user=OTHER), owner_id=OWNER, n_proposals=3)
        self.assertIsInstance(out, cc.Rejected)
        out = cc.parse_action(_payload("card:0:do", user=OTHER), owner_id=None, n_proposals=3)
        self.assertIsInstance(out, cc.ButtonVerdict)


class CandidateOrderTests(unittest.TestCase):
    def setUp(self):
        self.registers = cc.collect_registers(_fetch)

    def test_priority_subjects_lead_in_their_own_register(self):
        order = cc.candidate_order(self.registers, priority=["f64_risk"])
        self.assertEqual(order[0], ("f64_risk", "risks"))

    def test_then_recurrences_then_risks_then_stalled(self):
        order = cc.candidate_order(self.registers, priority=["f64_risk"])
        subjects = [s for s, _ in order]
        self.assertEqual(
            subjects,
            [
                "f64_risk",
                "/vault/wiki/wiki-0536.md",
                "/vault/wiki/wiki-0576.md",
                "essential_mode",
                "draft_wiring",
            ],
        )

    def test_next_actions_never_appears(self):
        order = cc.candidate_order(self.registers)
        self.assertNotIn("next_action", [s for s, _ in order])
        self.assertNotIn("loop", [s for s, _ in order])

    def test_a_subject_appears_once_even_if_also_priority(self):
        order = cc.candidate_order(self.registers, priority=["essential_mode", "essential_mode"])
        subjects = [s for s, _ in order]
        self.assertEqual(subjects.count("essential_mode"), 1)


class ParseAdvisedTests(unittest.TestCase):
    """AC1: exact match, whitespace difference, not-a-substring, missing note, NotWorth."""

    NOTE = "/vault/wiki/wiki-0900.md"
    TEXT = "머리말\n문 응답 지연이 반복되는 원인은 타임아웃 설정이다\n끝"

    def _proposal_json(self, quote: str, note: str | None = None) -> str:
        return _advice_json("병목 열자 이상 문장", "조언 열자 이상 문장", note or self.NOTE, quote)

    def test_exact_match_computes_the_line(self):
        out = cc.parse_advised(
            self._proposal_json("문 응답 지연이 반복되는 원인은 타임아웃 설정이다"), {self.NOTE: self.TEXT}
        )
        self.assertIsInstance(out, cc.Advice)
        self.assertEqual(out.evidence[0].line, 2)

    def test_whitespace_difference_still_verifies(self):
        # The model retypes rather than copy-pastes — a run of whitespace collapsing to one
        # space is not a fabrication.
        quote = "문 응답    지연이  반복되는 원인은 타임아웃 설정이다"
        out = cc.parse_advised(self._proposal_json(quote), {self.NOTE: self.TEXT})
        self.assertIsInstance(out, cc.Advice)
        self.assertEqual(out.evidence[0].line, 2)

    def test_not_a_substring_is_ungrounded(self):
        out = cc.parse_advised(
            self._proposal_json("이 문장은 노트에 없는 지어낸 인용문이다"), {self.NOTE: self.TEXT}
        )
        self.assertIsInstance(out, cc.Ungrounded)

    def test_missing_note_is_ungrounded(self):
        out = cc.parse_advised(
            self._proposal_json(
                "문 응답 지연이 반복되는 원인은 타임아웃 설정이다", note="/vault/wiki/wiki-9999.md"
            ),
            {self.NOTE: self.TEXT},
        )
        self.assertIsInstance(out, cc.Ungrounded)

    def test_paraphrase_sharing_only_a_quote_prefix_is_ungrounded(self):
        # A prefix-matching bug (checking only the first MIN_QUOTE_CHARS characters) would
        # treat this as grounded because it shares a real line's opening — the tail is a
        # plausible paraphrase, fabricated, and never appears verbatim anywhere in the note.
        real_line = " ".join(self.TEXT.splitlines()[1].split())
        prefix = real_line[: cc.MIN_QUOTE_CHARS]
        quote = prefix + " 이하는 노트에 없는 완전히 다른 결말이다"
        self.assertGreaterEqual(len(quote), cc.MIN_QUOTE_CHARS)
        out = cc.parse_advised(self._proposal_json(quote), {self.NOTE: self.TEXT})
        self.assertIsInstance(out, cc.Ungrounded)

    def test_not_worth_passes_through(self):
        out = cc.parse_advised(NOT_WORTH_JSON, {self.NOTE: self.TEXT})
        self.assertIsInstance(out, cc.NotWorth)
        self.assertEqual(out.reason, "근거가 약하다")

    def test_bad_json_is_ungrounded_not_raised(self):
        out = cc.parse_advised("not json", {})
        self.assertIsInstance(out, cc.Ungrounded)

    def test_schema_violation_is_ungrounded_not_raised(self):
        out = cc.parse_advised(json.dumps({"result": {"kind": "proposal"}}), {})
        self.assertIsInstance(out, cc.Ungrounded)


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


class ResolveLangTests(unittest.TestCase):
    def test_explicit_languages_pass_through(self):
        for lang in ("en", "ko", "ja"):
            self.assertEqual(cc.resolve_lang(lang), lang)

    def test_auto_and_anything_unmapped_falls_back_to_english(self):
        for raw in ("auto", "", "fr", "KO"):
            self.assertEqual(cc.resolve_lang(raw), "en")


class AdviceLangInstructionTests(unittest.TestCase):
    """AC3: build_advice_prompt attaches a language instruction, distill_core-style."""

    def test_ja_instruction_is_in_the_prompt(self):
        prompt = cc.build_advice_prompt("주어", "risks", [], "ja")
        self.assertIn(cc.ADVICE_LANG_INSTRUCTION["ja"], prompt)

    def test_ko_and_en_instructions_are_in_the_prompt(self):
        for lang in ("ko", "en"):
            prompt = cc.build_advice_prompt("주어", "risks", [], lang)
            self.assertIn(cc.ADVICE_LANG_INSTRUCTION[lang], prompt)

    def test_unmapped_lang_gets_the_fallback_instruction(self):
        prompt = cc.build_advice_prompt("주어", "risks", [], "auto")
        self.assertIn("same language as the past record above", prompt)


def _registers(sources: dict[str, list[str]]) -> cc.Registers:
    full = {name: sources.get(name, []) for name in cc.REGISTER_NAMES}
    return cc.Registers(texts={name: "" for name in cc.REGISTER_NAMES}, sources=full)


class MergeProjectCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.registers_a = _registers({"risks": ["f64_risk"], "stalled": ["a_only"]})
        self.registers_b = _registers({"risks": ["f64_risk"], "stalled": ["b_only"]})
        self.project_registers = {"proj-a": self.registers_a, "proj-b": self.registers_b}

    def test_priority_pair_leads_regardless_of_project_order(self):
        order = cc.merge_project_candidates(
            ["proj-a", "proj-b"], self.project_registers, priority=[("proj-b", "f64_risk")]
        )
        self.assertEqual(order[0], ("proj-b", "f64_risk", "risks"))

    def test_project_order_is_respected_after_priority(self):
        order = cc.merge_project_candidates(["proj-a", "proj-b"], self.project_registers)
        projects_in_order = [p for p, _, _ in order]
        # proj-a's own candidates all precede proj-b's — merge_project_candidates walks
        # project_order in sequence, each project's own candidate_order intact within it.
        first_b_index = projects_in_order.index("proj-b")
        self.assertTrue(all(p == "proj-a" for p in projects_in_order[:first_b_index]))

    def test_a_subject_is_deduplicated_across_projects(self):
        # f64_risk appears identically in both projects' registers (same fixture data) — it
        # must be spent once, not once per project, or the call budget scales with project
        # count instead of staying fixed at ADVISE_CALL_CAP.
        order = cc.merge_project_candidates(["proj-a", "proj-b"], self.project_registers)
        subjects = [s for _, s, _ in order]
        self.assertEqual(subjects.count("f64_risk"), 1)

    def test_unknown_project_in_the_order_is_skipped_not_raised(self):
        order = cc.merge_project_candidates(["proj-a", "ghost"], self.project_registers)
        self.assertTrue(all(p != "ghost" for p, _, _ in order))


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
        blocks = cc.build_blocks(proposals, lang="ko")
        headers = [b["text"]["text"] for b in blocks if b["type"] == "header"]
        # title header, then one per project in first-seen order: proj-a, unassigned, proj-b.
        self.assertEqual(headers, ["☀️ 오늘 제안 4", "proj-a", "무소속", "proj-b"])
        action_rows = [b for b in blocks if b["type"] == "actions"]
        self.assertEqual(len(action_rows), 4)

    def test_english_lang_uses_the_unassigned_label_and_button_words(self):
        proposals = [self._proposal("s1", "", "/vault/wiki/wiki-0001.md")]
        blocks = cc.build_blocks(proposals, lang="en")
        headers = [b["text"]["text"] for b in blocks if b["type"] == "header"]
        self.assertEqual(headers, ["☀️ Today's picks 1", "Unassigned"])
        action_row = next(b for b in blocks if b["type"] == "actions")
        labels = [el["text"]["text"] for el in action_row["elements"]]
        self.assertEqual(labels, ["Do", "Defer", "Drop"])


class SuppressedTests(unittest.TestCase):
    NOW = datetime(2026, 9, 22, 8, 0, 0, tzinfo=UTC)

    def _proposal(self, note: str, evidence_note: str, line: int) -> cc.Proposal:
        return cc.Proposal(
            subject="주어",
            note=note,
            register="stalled",
            bottleneck="병목 문장 열자 이상입니다",
            advice="조언 문장 열자 이상입니다",
            evidence=[cc.Evidence(note=evidence_note, quote="근거 인용문 열두자 이상", line=line)],
        )

    def test_recent_do_or_drop_suppresses_the_same_pair(self):
        candidate = self._proposal("/n1.md", "/e1.md", 3)
        for choice in ("do", "drop"):
            past = [
                cc.PastVerdictPair(
                    note="/n1.md",
                    evidence_note="/e1.md",
                    evidence_line=3,
                    choice=choice,
                    at=(self.NOW - timedelta(days=1)).isoformat(),
                )
            ]
            kept, dropped = cc.suppressed([candidate], past, now=self.NOW)
            self.assertEqual(kept, [], choice)
            self.assertEqual(dropped, [candidate], choice)

    def test_defer_never_suppresses(self):
        candidate = self._proposal("/n1.md", "/e1.md", 3)
        past = [
            cc.PastVerdictPair(
                note="/n1.md",
                evidence_note="/e1.md",
                evidence_line=3,
                choice="defer",
                at=self.NOW.isoformat(),
            )
        ]
        kept, dropped = cc.suppressed([candidate], past, now=self.NOW)
        self.assertEqual(kept, [candidate])
        self.assertEqual(dropped, [])

    def test_a_verdict_older_than_the_window_does_not_suppress(self):
        candidate = self._proposal("/n1.md", "/e1.md", 3)
        past = [
            cc.PastVerdictPair(
                note="/n1.md",
                evidence_note="/e1.md",
                evidence_line=3,
                choice="drop",
                at=(self.NOW - timedelta(hours=cc.SUPPRESS_WINDOW_HOURS + 1)).isoformat(),
            )
        ]
        kept, dropped = cc.suppressed([candidate], past, now=self.NOW)
        self.assertEqual(kept, [candidate])
        self.assertEqual(dropped, [])

    def test_a_different_pair_is_unaffected(self):
        candidate = self._proposal("/n1.md", "/e1.md", 3)
        past = [
            cc.PastVerdictPair(
                note="/n1.md", evidence_note="/e1.md", evidence_line=9, choice="drop", at=self.NOW.isoformat()
            )
        ]
        kept, dropped = cc.suppressed([candidate], past, now=self.NOW)
        self.assertEqual(kept, [candidate])
        self.assertEqual(dropped, [])


class EventFieldsTests(unittest.TestCase):
    def test_proposal_event_fields_carries_the_full_contract_field_set(self):
        proposal = cc.Proposal(
            subject="주어",
            note="/n1.md",
            project="proj-a",
            register="stalled",
            bottleneck="병목 문장 열자 이상입니다",
            advice="조언 문장 열자 이상입니다",
            evidence=[cc.Evidence(note="/e1.md", quote="근거 인용문 열두자 이상", line=4)],
        )
        fields = cc.proposal_event_fields(proposal, "ko", "1234.5", 2)
        self.assertEqual(
            set(fields),
            {
                "register",
                "project",
                "subject",
                "note",
                "bottleneck",
                "advice",
                "evidence",
                "lang",
                "card_ts",
                "idx",
            },
        )
        self.assertEqual(
            fields["evidence"], [{"note": "/e1.md", "quote": "근거 인용문 열두자 이상", "line": 4}]
        )
        self.assertEqual((fields["card_ts"], fields["idx"]), ("1234.5", 2))

    def test_verdict_event_fields_is_thin(self):
        verdict = cc.ButtonVerdict(idx=1, choice="drop", user="U1", at="t")
        fields = cc.verdict_event_fields(verdict, "1234.5")
        self.assertEqual(fields, {"card_ts": "1234.5", "idx": 1, "choice": "drop"})


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.sends = []
        self.handovers = []
        self.consumptions = []
        self.stubs = Stubs()
        self.graph = self._build(self.stubs)
        self.cfg = {"configurable": {"thread_id": "test-card"}}

    def _build(self, stubs: Stubs, lang: str = "ko"):
        collabs = card.Collaborators(
            fetch=_fetch,
            search=stubs.search,
            read_note=stubs.read_note,
            propose=stubs.propose,
            send=self._send,
            handover=self._handover,
            consumption=self._consumption,
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=stubs.past_verdicts,
            lang=lang,
        )
        return card.build_graph(collabs)

    def _send(self, blocks):
        self.sends.append(blocks)
        return cc.PostedCard(channel=CARD_CH, ts=CARD_TS)

    def _handover(self, session, at, paths):
        self.handovers.append((session, at, paths))
        return {}

    def _consumption(self, session, kind, paths):
        self.consumptions.append((session, kind, paths))
        return {"edges": 1}

    def _resume(self, idx, choice):
        verdict = cc.ButtonVerdict(idx=idx, choice=choice, user=OWNER, at="t")
        return self.graph.invoke(Command(resume=verdict), self.cfg)

    def test_graph_has_the_seven_nodes(self):
        names = set(self.graph.get_graph().nodes) - {"__start__", "__end__"}
        self.assertEqual(
            names,
            {
                "read_registers",
                "cross_check",
                "advise",
                "resolve",
                "post_card",
                "await_verdict",
                "record_verdict",
            },
        )

    def test_advise_stops_after_three_successful_candidates(self):
        out = self.graph.invoke({"verdicts": []}, self.cfg)
        self.assertIn("__interrupt__", out)
        self.assertEqual(len(self.stubs.propose_calls), 3)
        self.assertIn("f64_risk", self.stubs.propose_calls[0])
        self.assertIn("wiki-0536", self.stubs.propose_calls[1])
        self.assertIn("wiki-0576", self.stubs.propose_calls[2])
        self.assertEqual(len(self.sends), 1)
        self.assertEqual([p.note for p in out["proposals"]], EXPECTED_NOTES)
        for note in EXPECTED_NOTES:
            self.assertTrue(note.startswith("/vault/wiki/"))

    def test_notworth_candidates_are_skipped_not_counted_as_proposals(self):
        stubs = Stubs(responses=[NOT_WORTH_JSON, NOT_WORTH_JSON] + [ADVICE[s] for s in CANDIDATE_QUEUE[2:]])
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-notworth"}})
        self.assertEqual(len(stubs.propose_calls), 5)  # all five candidates tried
        self.assertEqual(len(out["proposals"]), 3)
        self.assertEqual(
            {p.subject for p in out["proposals"]},
            {"/vault/wiki/wiki-0576.md", "essential_mode", "draft_wiring"},
        )

    def test_advise_call_cap_stops_the_loop(self):
        stubs = Stubs(responses=[NOT_WORTH_JSON] * 8)
        original_cap = card.ADVISE_CALL_CAP
        card.ADVISE_CALL_CAP = 2
        try:
            graph = self._build(stubs)
            out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-cap"}})
        finally:
            card.ADVISE_CALL_CAP = original_cap
        self.assertEqual(len(stubs.propose_calls), 2)
        self.assertEqual(out["proposals"], [])

    def test_zero_proposals_still_sends_a_card(self):
        stubs = Stubs(responses=[NOT_WORTH_JSON] * 8)
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-zero"}})
        self.assertEqual(out["proposals"], [])
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.sends[0][0]["text"]["text"], "☀️ 오늘 제안 0")
        self.assertEqual(self.handovers[0][2], [])

    def test_resolve_drops_an_unresolved_subject_without_reproposing(self):
        # f64_risk is absent from `resolutions` — its Advice is grounded but the door has no
        # current claim for it, so cross_check also fails to resolve it, wiki-0900.md counts
        # as 했다 (not 아직), and the candidate queue has no priority section: recurrences
        # first (wiki-0536, wiki-0576), then risks (f64_risk, essential_mode), then stalled.
        stubs = Stubs(
            resolutions={
                "essential_mode": "/vault/wiki/wiki-0101.md",
                "draft_wiring": "/vault/wiki/wiki-0202.md",
            },
            responses=[
                ADVICE["/vault/wiki/wiki-0536.md"],
                ADVICE["/vault/wiki/wiki-0576.md"],
                ADVICE["f64_risk"],
            ],
        )
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-drop"}})
        # three candidates were tried and all three grounded — resolve, not advise, drops
        # f64_risk, so the loop never spends a fourth call to backfill it.
        self.assertEqual(len(stubs.propose_calls), 3)
        self.assertEqual(
            [p.subject for p in out["proposals"]], ["/vault/wiki/wiki-0536.md", "/vault/wiki/wiki-0576.md"]
        )

    def test_all_unresolved_still_sends_an_empty_card(self):
        # A dead-door world: every candidate's Advice is grounded, but none resolves to a
        # note. The card ships with 「제안 0」, never a refusal.
        stubs = Stubs(resolutions={}, paths_resolve=False)
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-all-unresolved"}})
        self.assertEqual(out["proposals"], [])
        self.assertEqual(len(self.sends), 1)

    def test_handover_and_consumption_cite_note_paths(self):
        out = self.graph.invoke({"verdicts": []}, self.cfg)
        self.assertEqual(len(self.handovers), 1)
        self.assertEqual(self.handovers[0][0], SESSION)
        # AC4: handover must carry the union of proposal notes and evidence notes. In this
        # fixture each proposal's evidence cites its own resolved note, so the union equals
        # EXPECTED_NOTES exactly — test_handover_cites_evidence_notes_even_when_they_differ_
        # from_the_resolved_note below exercises the case where they diverge.
        self.assertEqual(self.handovers[0][2], cc.handover_paths(out["proposals"]))
        self.assertEqual(self.handovers[0][2], EXPECTED_NOTES)
        for path in self.handovers[0][2]:
            self.assertTrue(path.startswith("/vault/wiki/"))

        self._resume(0, "do")
        self.assertEqual(self.consumptions, [(SESSION, "used", [EXPECTED_NOTES[0]])])
        self._resume(1, "drop")
        self.assertEqual(
            self.consumptions,
            [(SESSION, "used", [EXPECTED_NOTES[0]]), (SESSION, "contested", [EXPECTED_NOTES[1]])],
        )
        out = self._resume(2, "defer")
        self.assertNotIn("__interrupt__", out)
        self.assertEqual(len(self.consumptions), 2)  # defer adds no engine call
        self.assertEqual(len(self.sends), 1)  # the card went out once

    def test_handover_cites_evidence_notes_even_when_they_differ_from_the_resolved_note(self):
        # f64_risk's own past hit (and so its Advice evidence) cites wiki-0900.md, but the
        # door resolves f64_risk's *current* claim to a different note (wiki-7777.md) — a
        # proposal's evidence is not guaranteed to be the same note its subject resolves to
        # today. handover must still cite both: the owner saw the wiki-0900.md quote. The
        # propose stub here matches by the subject named in the prompt (not call position),
        # so it stays correct even though this resolutions override changes candidate order.
        def propose_by_subject(prompt: str) -> str:
            for subject in CANDIDATE_QUEUE:
                if repr(subject) in prompt:
                    return ADVICE[subject]
            return NOT_WORTH_JSON

        resolutions = dict(RESOLUTIONS)
        resolutions["f64_risk"] = "/vault/wiki/wiki-7777.md"
        stubs = Stubs(resolutions=resolutions)
        stubs.propose = propose_by_subject
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-union-diverge"}})
        f64_proposal = next(p for p in out["proposals"] if p.subject == "f64_risk")
        self.assertEqual(f64_proposal.note, "/vault/wiki/wiki-7777.md")
        self.assertEqual(f64_proposal.evidence[0].note, "/vault/wiki/wiki-0900.md")
        paths = self.handovers[0][2]
        self.assertIn("/vault/wiki/wiki-7777.md", paths)
        self.assertIn("/vault/wiki/wiki-0900.md", paths)
        self.assertEqual(paths, cc.handover_paths(out["proposals"]))

    def test_advise_stats_counts_not_worth_and_ungrounded_with_their_reasons(self):
        stubs = Stubs(responses=[NOT_WORTH_JSON, "not json"] + [ADVICE[s] for s in CANDIDATE_QUEUE[2:]])
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-stats"}})
        stats = out["advise_stats"]
        self.assertEqual(stats.calls, 5)
        self.assertEqual(stats.proposals_passed, 3)
        self.assertEqual(stats.not_worth, 1)
        self.assertEqual(stats.ungrounded, 1)
        self.assertEqual(stats.not_worth_reasons, ["근거가 약하다"])
        self.assertIn("not JSON", stats.ungrounded_reasons[0])

    def test_missing_note_bodies_speak_the_vault_dir_on_stderr(self):
        class BlindStubs(Stubs):
            def read_note(self, note: str) -> str | None:
                return None

        stubs = BlindStubs(responses=[NOT_WORTH_JSON] * 8)
        graph = self._build(stubs)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-blind-vault"}})
        err = buf.getvalue()
        self.assertIn("BORING_VAULT_DIR", err)
        self.assertEqual(err.count("BORING_VAULT_DIR"), 1)  # one line, not once per candidate

    def test_confirmation_line_and_event(self):
        out = self.graph.invoke({"verdicts": []}, self.cfg)
        confirmation = out["confirmation"]
        self.assertIsNotNone(confirmation)
        self.assertEqual(confirmation.total, 3)
        self.assertEqual(confirmation.done, ["/vault/wiki/wiki-9999.md", "/vault/wiki/wiki-9998.md"])
        self.assertEqual(confirmation.pending, ["/vault/wiki/wiki-0900.md"])
        self.assertEqual(confirmation.unknown, [])
        blocks = self.sends[0]
        self.assertEqual(blocks[1]["type"], "context")
        self.assertEqual(blocks[1]["elements"][0]["text"], "지난 승인 3 · 했다 2 · 아직 1")
        confirmation_events = [r for r in self.stubs.records if r[0] == "card_confirmation"]
        self.assertEqual(
            confirmation_events,
            [("card_confirmation", {"done": 2, "pending": 1, "session": PAST_SESSION})],
        )

    def test_priority_subject_leads_the_first_advice_call(self):
        self.graph.invoke({"verdicts": []}, self.cfg)
        self.assertIn("f64_risk", self.stubs.propose_calls[0])

    def test_door_failure_marks_unknown_instead_of_done(self):
        stubs = Stubs(responses=[NOT_WORTH_JSON] * 8, failures={"f64_risk": "claim-source answered 500"})
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-card-500"}})
        confirmation = out["confirmation"]
        self.assertEqual(confirmation.done, [])
        self.assertEqual(confirmation.pending, [])
        self.assertEqual(
            [note for note, _ in confirmation.unknown],
            ["/vault/wiki/wiki-0900.md", "/vault/wiki/wiki-9999.md", "/vault/wiki/wiki-9998.md"],
        )
        blocks = self.sends[-1]
        self.assertEqual(blocks[1]["elements"][0]["text"], "지난 승인 3 · 했다 0 · 아직 0 · 확인불가 3")
        confirmation_events = [r for r in stubs.records if r[0] == "card_confirmation"]
        self.assertEqual(
            confirmation_events,
            [
                (
                    "card_confirmation",
                    {"done": 0, "pending": 0, "session": PAST_SESSION, "unknown": 3},
                )
            ],
        )

    def test_approved_failure_stops_the_run_before_any_send(self):
        stubs = Stubs()

        def dead_door(since_hours: int):
            raise OSError("door unreachable")

        collabs = card.Collaborators(
            fetch=_fetch,
            search=stubs.search,
            read_note=stubs.read_note,
            propose=stubs.propose,
            send=self._send,
            handover=self._handover,
            consumption=self._consumption,
            resolve=stubs.resolve,
            approved=dead_door,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=stubs.past_verdicts,
            lang="ko",
        )
        graph = card.build_graph(collabs)
        with self.assertRaises(OSError):
            graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-card-dead"}})
        self.assertEqual(self.sends, [])
        self.assertEqual(stubs.records, [])

    def test_send_failure_leaves_no_confirmation_event(self):
        stubs = Stubs()

        def dead_slack(blocks):
            raise OSError("slack unreachable")

        collabs = card.Collaborators(
            fetch=_fetch,
            search=stubs.search,
            read_note=stubs.read_note,
            propose=stubs.propose,
            send=dead_slack,
            handover=self._handover,
            consumption=self._consumption,
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=stubs.past_verdicts,
            lang="ko",
        )
        graph = card.build_graph(collabs)
        with self.assertRaises(OSError):
            graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-card-nosend"}})
        self.assertEqual(stubs.records, [])  # the card never went out — no confirmation log

    def test_no_past_approvals_means_no_line_no_confirmation_event(self):
        stubs = Stubs(approved=[])
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-card-empty"}})
        self.assertIsNone(out["confirmation"])
        self.assertNotIn("지난 승인", _blocks_text(self.sends[-1]))
        self.assertNotIn("card_confirmation", [r[0] for r in stubs.records])

    def test_card_proposal_and_card_verdict_events_fire_for_every_row(self):
        # AC4: 3 proposals, 3 presses (해·빼·미뤄) → 3 card_proposal + 3 card_verdict events,
        # plus the existing card_confirmation — 7 record calls, none dropped or duplicated.
        self.graph.invoke({"verdicts": []}, self.cfg)
        self._resume(0, "do")
        self._resume(1, "drop")
        self._resume(2, "defer")
        names = [name for name, _ in self.stubs.records]
        self.assertEqual(names.count("card_proposal"), 3)
        self.assertEqual(names.count("card_verdict"), 3)
        self.assertEqual(names.count("card_confirmation"), 1)
        self.assertEqual(len(self.stubs.records), 7)
        verdict_fields = [fields for name, fields in self.stubs.records if name == "card_verdict"]
        self.assertEqual([v["choice"] for v in verdict_fields], ["do", "drop", "defer"])
        self.assertEqual({v["card_ts"] for v in verdict_fields}, {CARD_TS})
        proposal_fields = [fields for name, fields in self.stubs.records if name == "card_proposal"]
        self.assertEqual([p["idx"] for p in proposal_fields], [0, 1, 2])

    def test_a_recently_judged_pair_is_suppressed_before_the_card_ships(self):
        past = [
            cc.PastVerdictPair(
                note=EXPECTED_NOTES[0],
                evidence_note=EXPECTED_NOTES[0],
                evidence_line=2,
                choice="do",
                at=datetime.now(UTC).isoformat(),
            )
        ]
        stubs = Stubs(past_verdict_pairs=past)
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-suppress"}})
        self.assertEqual([p.note for p in out["proposals"]], EXPECTED_NOTES[1:])
        self.assertEqual(out["suppressed_count"], 1)

    def test_active_projects_are_dialed_and_shared_subjects_are_deduplicated(self):
        # Every fixture project answers identical registers (the _fetch stub ignores its
        # project argument), so the global subject dedup in merge_project_candidates means
        # only the first-called project is credited with any candidates.
        stubs = Stubs(active_project_names=["proj-x", "proj-y"], responses=[NOT_WORTH_JSON] * 8)
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-multi-project"}})
        self.assertEqual(out["projects"], ["proj-x", "proj-y", ""])
        self.assertEqual(set(out["per_project_candidates"]), {"proj-x"})
        self.assertEqual(out["per_project_candidates"]["proj-x"], 5)


class AwaitVerdictsTests(unittest.TestCase):
    """AC6: CARD_WAIT_HOURS=0 (or expired) ends the wait immediately, touching Slack 0 times,
    leaving whatever was unanswered exactly as it is."""

    class _FakeWeb:
        def __init__(self):
            self.chat_update_calls = 0

        def chat_update(self, **kwargs):
            self.chat_update_calls += 1

    def test_zero_wait_hours_returns_immediately_without_a_chat_update(self):
        holder = card._Holder()
        holder.message = cc.PostedCard(channel="C1", ts=CARD_TS)
        state = {"proposals": [], "verdicts": [], "lang": "ko"}
        web = self._FakeWeb()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = card._await_verdicts(
                holder, graph=None, config={}, state=state, web_client=web, wait_hours=0
            )
        self.assertEqual(web.chat_update_calls, 0)
        self.assertIs(out, state)


class DryRunWiringTests(unittest.TestCase):
    """AC5: CARD_DRY_RUN must run the graph with the dry collaborators — never the live Slack
    send, the live handover, or the live event log. A mutant that wired `_run_dry`'s
    Collaborators to `send=_env_send, handover=_live_handover` survived all existing tests
    because nothing exercised `_run_dry` directly; this drives it end to end with every live
    network/model seam stubbed and asserts which functions were actually called."""

    def test_dry_run_never_calls_live_send_handover_or_record(self):
        calls = {"live_send": 0, "live_handover": 0, "live_record": 0}
        dry_calls = {"send": 0, "handover": 0, "record": 0}

        def fake_env_send(blocks):
            calls["live_send"] += 1
            raise AssertionError("dry run must not call the live Slack send")

        def fake_live_handover(session, at, paths):
            calls["live_handover"] += 1
            raise AssertionError("dry run must not call the live handover")

        def fake_live_record(event, fields):
            calls["live_record"] += 1
            raise AssertionError("dry run must not call the live event log")

        real_dry_send, real_dry_handover, real_dry_record = (
            card._dry_send,
            card._dry_handover,
            card._dry_record,
        )

        def counting_dry_send(blocks):
            dry_calls["send"] += 1
            return real_dry_send(blocks)

        def counting_dry_handover(session, at, paths):
            dry_calls["handover"] += 1
            return real_dry_handover(session, at, paths)

        def counting_dry_record(event, fields):
            dry_calls["record"] += 1
            return real_dry_record(event, fields)

        stubs = Stubs(responses=[NOT_WORTH_JSON] * 8)
        buf = io.StringIO()

        with (
            mock.patch.object(card, "_env_send", side_effect=fake_env_send),
            mock.patch.object(card, "_live_handover", side_effect=fake_live_handover),
            mock.patch.object(card, "_live_record", side_effect=fake_live_record),
            mock.patch.object(card, "_dry_send", side_effect=counting_dry_send),
            mock.patch.object(card, "_dry_handover", side_effect=counting_dry_handover),
            mock.patch.object(card, "_dry_record", side_effect=counting_dry_record),
            mock.patch.object(card, "_live_fetch", side_effect=_fetch),
            mock.patch.object(card, "_live_search", side_effect=stubs.search),
            mock.patch.object(card, "_live_read_note", side_effect=stubs.read_note),
            mock.patch.object(card, "_live_resolve", side_effect=stubs.resolve),
            mock.patch.object(card, "_live_approved", side_effect=stubs.approved),
            mock.patch.object(card, "_live_active_projects", side_effect=stubs.active_projects),
            mock.patch.object(card, "_live_past_verdicts", side_effect=stubs.past_verdicts),
            mock.patch.object(card, "make_propose", return_value=stubs.propose),
            contextlib.redirect_stdout(buf),
        ):
            rc = card._run_dry()

        self.assertEqual(rc, 0)
        self.assertEqual(calls, {"live_send": 0, "live_handover": 0, "live_record": 0})
        self.assertEqual(dry_calls["send"], 1)
        self.assertEqual(dry_calls["handover"], 1)
        self.assertEqual(dry_calls["record"], 1)  # PAST_APPROVED is non-empty → one confirmation event

        # AC5 telemetry: the dry-run JSON also carries the counters the contract asks the
        # executor to be able to quote back.
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["calls"], 5)
        self.assertEqual(payload["proposals_passed"], 0)
        self.assertEqual(payload["not_worth"], 5)
        self.assertEqual(payload["ungrounded"], 0)
        self.assertEqual(len(payload["not_worth_reasons"]), 5)


if __name__ == "__main__":
    unittest.main()
