#!/usr/bin/env python3
"""card_core's parsers and builders, and the g_card graph — no network, no LLM, no Slack.

The graph test drives build_graph with stub collaborators through MemorySaver + thread_id:
first invoke pauses at the interrupt with no verdicts, each Command(resume=…) records one
verdict, and the run ends after the last proposal is judged.

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

PROPOSE_JSON = json.dumps(
    {
        "proposals": [
            {
                "title": "정리부터",
                "why": "정해진 것부터 닫는다",
                "source_note": "next_action",
                "register": "next_actions",
            },
            {
                "title": "지연 방어",
                "why": "문 타임아웃을 늘려야 한다",
                "source_note": "f64_risk",
                "register": "risks",
            },
            {
                "title": "재발 수정",
                "why": "같은 사고가 또 나왔다",
                "source_note": "/vault/wiki/wiki-0576.md",
                "register": "recurrences",
            },
        ]
    }
)

SOURCE_NOTES = ["next_action", "f64_risk", "/vault/wiki/wiki-0576.md"]


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


class BuildBlocksTests(unittest.TestCase):
    def setUp(self):
        self.proposals = [
            cc.Proposal(
                title=f"제안 {i}",
                why=f"이유 {i}",
                source_note=note,
                register="next_actions",
            )
            for i, note in enumerate(SOURCE_NOTES)
        ]

    def test_every_proposal_has_a_button_row_with_card_action_ids(self):
        blocks = cc.build_blocks(self.proposals)
        self.assertEqual(blocks[0]["type"], "header")
        action_rows = [b for b in blocks if b["type"] == "actions"]
        self.assertEqual(len(action_rows), 3)
        for idx, row in enumerate(action_rows):
            buttons = {b["action_id"]: b for b in row["elements"]}
            self.assertEqual(set(buttons), {f"card:{idx}:do", f"card:{idx}:defer", f"card:{idx}:drop"})
            for button in row["elements"]:
                self.assertEqual(button["value"], SOURCE_NOTES[idx])
            labels = [b["text"]["text"] for b in row["elements"]]
            self.assertEqual(labels, ["해", "미뤄", "빼"])

    def test_judged_row_shows_its_mark_instead_of_buttons(self):
        verdict = cc.ButtonVerdict(idx=1, choice="do", user=OWNER, at="t")
        blocks = cc.build_blocks(self.proposals, [verdict])
        action_rows = [b for b in blocks if b["type"] == "actions"]
        marks = [b for b in blocks if b["type"] == "context"]
        self.assertEqual(len(action_rows), 2)
        self.assertEqual(len(marks), 1)
        self.assertIn("✓ 해", marks[0]["elements"][0]["text"])
        self.assertNotIn("card:1:", json.dumps(blocks, ensure_ascii=False))


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

    def test_source_note_outside_allow_list_is_refused(self):
        data = json.loads(PROPOSE_JSON)
        data["proposals"][0]["source_note"] = "지어낸 것"
        with self.assertRaises(ValueError):
            cc.parse_proposals(json.dumps(data, ensure_ascii=False), self.registers)

    def test_bad_json_and_bad_register_are_refused(self):
        with self.assertRaises(ValueError):
            cc.parse_proposals("not json", self.registers)
        data = json.loads(PROPOSE_JSON)
        data["proposals"][0]["register"] = "next_action"
        with self.assertRaises(ValueError):
            cc.parse_proposals(json.dumps(data), self.registers)


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.sends = []
        self.handovers = []
        self.feedbacks = []
        collabs = card.Collaborators(
            fetch=_fetch,
            propose=lambda prompt: PROPOSE_JSON,
            send=self._send,
            handover=self._handover,
            feedback=self._feedback,
        )
        self.graph = card.build_graph(collabs)
        self.cfg = {"configurable": {"thread_id": "test-card"}}

    def _send(self, blocks):
        self.sends.append(blocks)
        return cc.PostedCard(channel=CARD_CH, ts=CARD_TS)

    def _handover(self, session, at, paths):
        self.handovers.append((session, at, paths))
        return {}

    def _feedback(self, key, verdict):
        self.feedbacks.append((key, verdict))
        return {"used": 1}

    def _resume(self, idx, choice):
        verdict = cc.ButtonVerdict(idx=idx, choice=choice, user=OWNER, at="t")
        return self.graph.invoke(Command(resume=verdict), self.cfg)

    def test_ascii_has_the_five_nodes(self):
        drawing = (
            card.build_graph(
                card.Collaborators(
                    fetch=_fetch,
                    propose=lambda p: PROPOSE_JSON,
                    send=self._send,
                    handover=self._handover,
                    feedback=self._feedback,
                )
            )
            .get_graph()
            .draw_ascii()
        )
        for name in ("read_registers", "propose", "post_card", "await_verdict", "record_verdict"):
            self.assertIn(name, drawing)

    def test_card_then_interrupt_then_verdicts(self):
        out = self.graph.invoke({"verdicts": []}, self.cfg)
        self.assertIn("__interrupt__", out)
        self.assertEqual(out["verdicts"], [])
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(len(self.handovers), 1)
        self.assertEqual(self.handovers[0][0], SESSION)
        self.assertEqual(self.handovers[0][2], SOURCE_NOTES)

        out = self._resume(0, "do")
        self.assertIn("__interrupt__", out)
        self.assertEqual([v.choice for v in out["verdicts"]], ["do"])
        self.assertEqual(self.feedbacks, [(SESSION, "used")])

        out = self._resume(1, "drop")
        self.assertEqual(self.feedbacks, [(SESSION, "used"), (SESSION, "contested")])

        out = self._resume(2, "defer")
        self.assertNotIn("__interrupt__", out)
        self.assertEqual(len(self.feedbacks), 2)  # defer adds no engine call
        self.assertEqual(len(self.sends), 1)  # the card went out once


if __name__ == "__main__":
    unittest.main()
