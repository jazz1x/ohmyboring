#!/usr/bin/env python3
"""card_verdicts — suppressed, confirm_past, parse_action, event field shaping.

Run: python3 agents/slack/test_card_verdicts.py
"""

import os
import sys
import unittest
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import card_types as cc  # noqa: E402
import card_verdicts as cv  # noqa: E402

OWNER = "U_OWNER"
OTHER = "U_OTHER"
CARD_TS = "1.0"
CARD_CH = "C1"

# Past approvals, newest first: the first still resolves from today's registers (아직),
# the other two no longer do (했다 2 · 아직 1 — asymmetric, so a done/pending swap shows).
PAST_APPROVED = [
    cc.PastApproved(session="slack:C1:1759000000.000001", note="/vault/wiki/wiki-0900.md", at="t-1"),
    cc.PastApproved(session="slack:C1:1758000000.000002", note="/vault/wiki/wiki-9999.md", at="t-2"),
    cc.PastApproved(session="slack:C1:1757000000.000003", note="/vault/wiki/wiki-9998.md", at="t-3"),
]
PAST_SESSION = "slack:C1:1759000000.000001"


def _payload(action_id: str, user: str = OWNER) -> dict:
    return {
        "type": "block_actions",
        "actions": [{"action_id": action_id, "value": "/vault/wiki/wiki-0576.md"}],
        "user": {"id": user},
        "message": {"ts": CARD_TS},
        "channel": {"id": CARD_CH},
    }


class ConfirmPastTests(unittest.TestCase):
    def test_note_still_in_today_register_is_pending_otherwise_done(self):
        out = cv.confirm_past(PAST_APPROVED, {"/vault/wiki/wiki-0900.md"})
        self.assertEqual(out.total, 3)
        self.assertEqual(out.done, ["/vault/wiki/wiki-9999.md", "/vault/wiki/wiki-9998.md"])
        self.assertEqual(out.pending, ["/vault/wiki/wiki-0900.md"])
        self.assertEqual(out.unknown, [])
        self.assertEqual(out.session, PAST_SESSION)

    def test_absence_is_done_only_when_the_door_answered(self):
        out = cv.confirm_past(PAST_APPROVED[1:], set())
        self.assertEqual(out.done, ["/vault/wiki/wiki-9999.md", "/vault/wiki/wiki-9998.md"])
        self.assertEqual(out.pending, [])

    def test_door_failure_leaves_the_past_approval_unknown(self):
        out = cv.confirm_past(PAST_APPROVED, set(), failures=["claim-source answered 500"])
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
        out = cv.confirm_past([], {"/vault/wiki/wiki-0900.md"})
        self.assertEqual(out.total, 0)
        self.assertIsNone(out.session)


class ParseActionTests(unittest.TestCase):
    def test_valid_press_is_a_verdict(self):
        out = cv.parse_action(_payload("card:1:do"), owner_id=OWNER, n_total=3, at="t")
        self.assertIsInstance(out, cc.ButtonVerdict)
        self.assertEqual((out.idx, out.choice, out.user, out.at), (1, "do", OWNER, "t"))

    def test_unknown_action_id_is_rejected(self):
        out = cv.parse_action(_payload("card:1:nope"), owner_id=OWNER, n_total=3)
        self.assertIsInstance(out, cc.Rejected)
        out = cv.parse_action(_payload("reaction:x"), owner_id=OWNER, n_total=3)
        self.assertIsInstance(out, cc.Rejected)

    def test_missing_proposal_is_rejected(self):
        out = cv.parse_action(_payload("card:7:do"), owner_id=OWNER, n_total=3)
        self.assertIsInstance(out, cc.Rejected)

    def test_non_owner_press_is_rejected_only_when_owner_configured(self):
        out = cv.parse_action(_payload("card:0:do", user=OTHER), owner_id=OWNER, n_total=3)
        self.assertIsInstance(out, cc.Rejected)
        out = cv.parse_action(_payload("card:0:do", user=OTHER), owner_id=None, n_total=3)
        self.assertIsInstance(out, cc.ButtonVerdict)


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
            kept, dropped = cv.suppressed([candidate], past, now=self.NOW)
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
        kept, dropped = cv.suppressed([candidate], past, now=self.NOW)
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
                at=(self.NOW - timedelta(hours=cv.SUPPRESS_WINDOW_HOURS + 1)).isoformat(),
            )
        ]
        kept, dropped = cv.suppressed([candidate], past, now=self.NOW)
        self.assertEqual(kept, [candidate])
        self.assertEqual(dropped, [])

    def test_a_different_pair_is_unaffected(self):
        candidate = self._proposal("/n1.md", "/e1.md", 3)
        past = [
            cc.PastVerdictPair(
                note="/n1.md", evidence_note="/e1.md", evidence_line=9, choice="drop", at=self.NOW.isoformat()
            )
        ]
        kept, dropped = cv.suppressed([candidate], past, now=self.NOW)
        self.assertEqual(kept, [candidate])
        self.assertEqual(dropped, [])

    def test_a_malformed_timestamp_raises_instead_of_being_silently_skipped(self):
        # F5/ROP: a judged-history row this function cannot place in time is a wrong-card
        # signal, not a quiet gap — the old `except ValueError: continue` hid exactly this.
        candidate = self._proposal("/n1.md", "/e1.md", 3)
        past = [
            cc.PastVerdictPair(
                note="/n1.md", evidence_note="/e1.md", evidence_line=3, choice="drop", at="not-a-timestamp"
            )
        ]
        with self.assertRaises(ValueError):
            cv.suppressed([candidate], past, now=self.NOW)


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
        fields = cv.proposal_event_fields(proposal, "ko", "1234.5", 2)
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
        fields = cv.verdict_event_fields(verdict, "1234.5")
        self.assertEqual(fields, {"card_ts": "1234.5", "idx": 1, "choice": "drop"})


if __name__ == "__main__":
    unittest.main()
