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

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import card  # noqa: E402
import card_core as cc  # noqa: E402
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


def _fetch(path: str) -> dict:
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
    ):
        self.resolutions = resolutions if resolutions is not None else RESOLUTIONS
        self.approved_items = approved if approved is not None else PAST_APPROVED
        self.paths_resolve = paths_resolve
        self.failures = failures or {}
        self.responses = (
            list(responses) if responses is not None else [ADVICE[s] for s in CANDIDATE_QUEUE[:3]]
        )
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
        blocks = cc.build_blocks(self.proposals)
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
        blocks = cc.build_blocks(self.proposals)
        text = _blocks_text(blocks)
        self.assertIn("병목 설명 0", text)
        self.assertIn("wiki-0900 L1", text)
        self.assertIn("근거 인용문", text)

    def test_judged_row_shows_its_mark_instead_of_buttons(self):
        verdict = cc.ButtonVerdict(idx=1, choice="do", user=OWNER, at="t")
        blocks = cc.build_blocks(self.proposals, [verdict])
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
        blocks = cc.build_blocks(self.proposals, confirmation=confirmation)
        self.assertEqual(blocks[1]["type"], "context")
        self.assertEqual(blocks[1]["elements"][0]["text"], "지난 승인 3 · 했다 2 · 아직 1")

    def test_confirmation_line_counts_unknown_when_the_door_failed(self):
        confirmation = cc.Confirmation(
            total=2,
            done=[],
            pending=["/vault/wiki/wiki-0900.md"],
            unknown=[("/vault/wiki/wiki-9999.md", "claim-source answered 500")],
        )
        blocks = cc.build_blocks(self.proposals, confirmation=confirmation)
        self.assertEqual(blocks[1]["elements"][0]["text"], "지난 승인 2 · 했다 0 · 아직 1 · 확인불가 1")

    def test_no_confirmation_no_line(self):
        for none in (None, cc.Confirmation(total=0, done=[], pending=[])):
            blocks = cc.build_blocks(self.proposals, confirmation=none)
            self.assertNotIn("지난 승인", _blocks_text(blocks))

    def test_empty_proposals_still_renders_a_zero_header(self):
        blocks = cc.build_blocks([])
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


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.sends = []
        self.handovers = []
        self.consumptions = []
        self.stubs = Stubs()
        self.graph = self._build(self.stubs)
        self.cfg = {"configurable": {"thread_id": "test-card"}}

    def _build(self, stubs: Stubs):
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
        self.assertEqual(
            self.stubs.records,
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
        self.assertEqual(
            stubs.records,
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
        )
        graph = card.build_graph(collabs)
        with self.assertRaises(OSError):
            graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-card-nosend"}})
        self.assertEqual(stubs.records, [])  # the card never went out — no confirmation log

    def test_no_past_approvals_means_no_line_no_event(self):
        stubs = Stubs(approved=[])
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-card-empty"}})
        self.assertIsNone(out["confirmation"])
        self.assertNotIn("지난 승인", _blocks_text(self.sends[-1]))
        self.assertEqual(stubs.records, [])


if __name__ == "__main__":
    unittest.main()
