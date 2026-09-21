#!/usr/bin/env python3
"""card_core's parsers and builders, and the g_card graph — no network, no LLM, no Slack.

The graph test drives build_graph with stub collaborators through MemorySaver + thread_id:
first invoke pauses at the interrupt with no verdicts, each Command(resume=…) records one
verdict, and the run ends after the last proposal is judged. The resolve stub resolves a
fixed map of subjects, mirroring the live collaborator's rule that a source already shaped
like a note path resolves to itself.

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

# Round 1: one subject refuses to resolve ("next_action"), one resolves ("f64_risk"),
# one is a recurrence source — already a note path.
PROPOSE_JSON = json.dumps(
    {
        "proposals": [
            {
                "title": "정리부터",
                "why": "정해진 것부터 닫는다",
                "subject": "next_action",
                "register": "next_actions",
            },
            {
                "title": "지연 방어",
                "why": "문 타임아웃을 늘려야 한다",
                "subject": "f64_risk",
                "register": "risks",
            },
            {
                "title": "재발 수정",
                "why": "같은 사고가 또 나왔다",
                "subject": "/vault/wiki/wiki-0576.md",
                "register": "recurrences",
            },
        ]
    }
)

# Round 2 (re-propose on the remaining candidates): only "loop" resolves.
PROPOSE_JSON_2 = json.dumps(
    {
        "proposals": [
            {
                "title": "루프 닫기",
                "why": "반복되는 정리를 자동화한다",
                "subject": "loop",
                "register": "next_actions",
            },
            {
                "title": "본질 모드",
                "why": "필수 동작만 남긴다",
                "subject": "essential_mode",
                "register": "risks",
            },
            {
                "title": "병합 대기",
                "why": "머지를 끝낸다",
                "subject": "draft_wiring",
                "register": "stalled",
            },
        ]
    }
)

# subject → note path; subjects absent from the map are Unresolved. Recurrence-style
# paths resolve to themselves, like the live collaborator.
RESOLUTIONS = {
    "f64_risk": "/vault/wiki/wiki-0900.md",
    "loop": "/vault/wiki/wiki-0101.md",
}

EXPECTED_NOTES = ["/vault/wiki/wiki-0900.md", "/vault/wiki/wiki-0576.md", "/vault/wiki/wiki-0101.md"]

# Past approvals, newest first: the first still resolves from today's registers (아직),
# the second no longer does (했다).
PAST_APPROVED = [
    cc.PastApproved(session="slack:C1:1759000000.000001", note="/vault/wiki/wiki-0900.md", at="t-1"),
    cc.PastApproved(session="slack:C1:1758000000.000002", note="/vault/wiki/wiki-9999.md", at="t-2"),
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
    """The three new collaborators, recording every call."""

    def __init__(self, resolutions: dict[str, str] | None = None, approved=None, paths_resolve: bool = True):
        self.resolutions = resolutions if resolutions is not None else RESOLUTIONS
        self.approved_items = approved if approved is not None else PAST_APPROVED
        self.paths_resolve = paths_resolve
        self.propose_calls: list[str] = []
        self.resolve_calls: list[tuple[str, str]] = []
        self.records: list[tuple[str, dict]] = []

    def propose(self, prompt: str) -> str:
        self.propose_calls.append(prompt)
        return PROPOSE_JSON if len(self.propose_calls) == 1 else PROPOSE_JSON_2

    def resolve(self, subject: str, register: str):
        self.resolve_calls.append((subject, register))
        if self.paths_resolve and subject.startswith("/"):
            return cc.ResolvedNote(subject=subject, note=subject)
        note = self.resolutions.get(subject)
        if note is None:
            return cc.Unresolved(subject=subject, register=register, reason="no current claim for subject")
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
                title=f"제안 {i}",
                why=f"이유 {i}",
                subject=f"주어 {i}",
                note=note,
                register="next_actions",
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

    def test_judged_row_shows_its_mark_instead_of_buttons(self):
        verdict = cc.ButtonVerdict(idx=1, choice="do", user=OWNER, at="t")
        blocks = cc.build_blocks(self.proposals, [verdict])
        action_rows = [b for b in blocks if b["type"] == "actions"]
        marks = [b for b in blocks if b["type"] == "context"]
        self.assertEqual(len(action_rows), 2)
        self.assertEqual(len(marks), 1)
        self.assertIn("✓ 해", marks[0]["elements"][0]["text"])
        self.assertNotIn("card:1:", _blocks_text(blocks))

    def test_confirmation_line_sits_under_the_header_when_there_is_one(self):
        confirmation = cc.Confirmation(total=2, done=PAST_APPROVED[:1], pending=PAST_APPROVED[1:])
        blocks = cc.build_blocks(self.proposals, confirmation=confirmation)
        self.assertEqual(blocks[1]["type"], "context")
        self.assertEqual(blocks[1]["elements"][0]["text"], "지난 승인 2 · 했다 1 · 아직 1")

    def test_no_confirmation_no_line(self):
        for none in (None, cc.Confirmation(total=0, done=[], pending=[])):
            blocks = cc.build_blocks(self.proposals, confirmation=none)
            self.assertNotIn("지난 승인", _blocks_text(blocks))


class ConfirmPastTests(unittest.TestCase):
    def test_note_still_in_today_register_is_pending_otherwise_done(self):
        out = cc.confirm_past(PAST_APPROVED, {"/vault/wiki/wiki-0900.md"})
        self.assertEqual(out.total, 2)
        self.assertEqual([d.note for d in out.done], ["/vault/wiki/wiki-9999.md"])
        self.assertEqual([p.note for p in out.pending], ["/vault/wiki/wiki-0900.md"])

    def test_empty_past_is_a_zero_confirmation(self):
        out = cc.confirm_past([], {"/vault/wiki/wiki-0900.md"})
        self.assertEqual(out.total, 0)


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


class ParseProposalsTests(unittest.TestCase):
    def setUp(self):
        self.registers = cc.collect_registers(_fetch)

    def test_collect_registers_shapes_texts_and_allow_lists(self):
        self.assertIn("next_action", self.registers.sources["next_actions"])
        self.assertIn("f64_risk", self.registers.sources["risks"])
        self.assertIn("/vault/wiki/wiki-0576.md", self.registers.sources["recurrences"])
        self.assertIn("/vault/wiki/wiki-0536.md", self.registers.sources["recurrences"])
        self.assertIn("relay sync", self.registers.texts["recurrences"])

    def test_collect_registers_refuses_malformed_payload(self):
        with self.assertRaises(ValueError):
            cc.collect_registers(lambda path: {"unexpected": True})

    def test_grounded_json_passes(self):
        out = cc.parse_proposals(PROPOSE_JSON, self.registers)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0].title, "정리부터")
        self.assertEqual(out[2].register_, "recurrences")
        self.assertEqual(out[0].subject, "next_action")
        self.assertEqual(out[0].note, "")  # the graph fills note after resolve

    def test_subject_outside_allow_list_is_refused(self):
        data = json.loads(PROPOSE_JSON)
        data["proposals"][0]["subject"] = "지어낸 것"
        with self.assertRaises(ValueError):
            cc.parse_proposals(json.dumps(data, ensure_ascii=False), self.registers)

    def test_prefix_of_a_real_path_is_refused(self):
        # startswith is not membership: /vault/wiki/wiki-0576 is the real path minus .md.
        data = json.loads(PROPOSE_JSON)
        data["proposals"][2]["subject"] = "/vault/wiki/wiki-0576"
        with self.assertRaises(ValueError):
            cc.parse_proposals(json.dumps(data), self.registers)

    def test_path_with_trailing_space_is_refused(self):
        data = json.loads(PROPOSE_JSON)
        data["proposals"][2]["subject"] = "/vault/wiki/wiki-0576.md "
        with self.assertRaises(ValueError):
            cc.parse_proposals(json.dumps(data), self.registers)

    def test_not_exactly_three_is_refused(self):
        data = json.loads(PROPOSE_JSON)
        data["proposals"] = data["proposals"][:2]
        with self.assertRaises(ValueError):
            cc.parse_proposals(json.dumps(data), self.registers)

    def test_duplicate_subject_is_refused(self):
        # Each subject grounds one proposal — the same subject twice means the set is a lie.
        data = json.loads(PROPOSE_JSON)
        data["proposals"][1]["subject"] = "next_action"
        data["proposals"][1]["register"] = "next_actions"
        with self.assertRaises(ValueError):
            cc.parse_proposals(json.dumps(data), self.registers)

    def test_bad_json_and_bad_register_are_refused(self):
        with self.assertRaises(ValueError):
            cc.parse_proposals("not json", self.registers)
        data = json.loads(PROPOSE_JSON)
        data["proposals"][0]["register"] = "next_action"
        with self.assertRaises(ValueError):
            cc.parse_proposals(json.dumps(data), self.registers)

    def test_exclude_narrows_the_allow_list(self):
        # A re-propose must not regurgitate a subject resolve already dropped.
        exclude = {"next_action"}
        with self.assertRaises(ValueError):
            cc.parse_proposals(PROPOSE_JSON, self.registers, exclude)
        prompt = cc.build_prompt(self.registers, exclude)
        self.assertNotIn("\n1. next_action\n", prompt)
        self.assertIn("sources:\n1. loop", prompt)


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.sends = []
        self.handovers = []
        self.consumptions = []
        self.stubs = Stubs()
        collabs = card.Collaborators(
            fetch=_fetch,
            propose=self.stubs.propose,
            send=self._send,
            handover=self._handover,
            consumption=self._consumption,
            resolve=self.stubs.resolve,
            approved=self.stubs.approved,
            record=self.stubs.record,
        )
        self.graph = card.build_graph(collabs)
        self.cfg = {"configurable": {"thread_id": "test-card"}}

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

    def test_ascii_has_the_six_nodes(self):
        drawing = (
            card.build_graph(
                card.Collaborators(
                    fetch=_fetch,
                    propose=self.stubs.propose,
                    send=self._send,
                    handover=self._handover,
                    consumption=self._consumption,
                    resolve=self.stubs.resolve,
                    approved=self.stubs.approved,
                    record=self.stubs.record,
                )
            )
            .get_graph()
            .draw_ascii()
        )
        for name in (
            "read_registers",
            "propose",
            "resolve",
            "post_card",
            "await_verdict",
            "record_verdict",
        ):
            self.assertIn(name, drawing)

    def test_unresolved_subject_is_dropped_and_reproposed_once(self):
        out = self.graph.invoke({"verdicts": []}, self.cfg)
        self.assertIn("__interrupt__", out)
        # one subject refused to resolve → re-propose ran once on the remainder
        self.assertEqual(len(self.stubs.propose_calls), 2)
        self.assertIn(("next_action", "next_actions"), self.stubs.resolve_calls)
        # three proposals made the card, every one carrying a note path
        self.assertEqual(len(self.sends), 1)
        self.assertEqual([p.note for p in out["proposals"]], EXPECTED_NOTES)
        for note in EXPECTED_NOTES:
            self.assertTrue(note.startswith("/vault/wiki/"))

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
        self.assertEqual(confirmation.total, 2)
        self.assertEqual(len(confirmation.done), 1)
        self.assertEqual(len(confirmation.pending), 1)
        blocks = self.sends[0]
        self.assertEqual(blocks[1]["type"], "context")
        self.assertEqual(blocks[1]["elements"][0]["text"], "지난 승인 2 · 했다 1 · 아직 1")
        self.assertEqual(
            self.stubs.records,
            [("card_confirmation", {"done": 1, "pending": 1, "session": PAST_SESSION})],
        )

    def test_no_past_approvals_means_no_line_no_event(self):
        stubs = Stubs(approved=[])
        collabs = card.Collaborators(
            fetch=_fetch,
            propose=stubs.propose,
            send=self._send,
            handover=self._handover,
            consumption=self._consumption,
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
        )
        graph = card.build_graph(collabs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-card-empty"}})
        self.assertIsNone(out["confirmation"])
        self.assertNotIn("지난 승인", _blocks_text(self.sends[-1]))
        self.assertEqual(stubs.records, [])

    def test_everything_unresolved_refuses_instead_of_an_empty_card(self):
        # paths_resolve=False models the dead-door world: every subject fails (and the
        # model never picked a recurrence path) — the run refuses, never a 「제안 0」 card.
        stubs = Stubs(resolutions={}, paths_resolve=False)
        collabs = card.Collaborators(
            fetch=_fetch,
            propose=stubs.propose,
            send=self._send,
            handover=self._handover,
            consumption=self._consumption,
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
        )
        graph = card.build_graph(collabs)
        with self.assertRaises(ValueError) as ctx:
            graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-card-zero"}})
        self.assertIn("제안 0", str(ctx.exception))
        self.assertEqual(self.sends, [])  # nothing was posted


if __name__ == "__main__":
    unittest.main()
