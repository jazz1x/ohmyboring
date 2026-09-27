#!/usr/bin/env python3
"""card_press — 버튼 value의 줄 해석(parse_press)과 누름→할 일 결정표(effects).

value round-trip: card_view가 만든 버튼 value를 card_press가 카드 상태 없이 되돌린다.
effects 표: 줄 × 선택 한 칸씩 — 쓰는 이벤트 이름·필드 이름·호출 순서를 그대로 고정한다.
거절: 주인이 아닌 누름, 깨진 value, 모르는 줄, defer를 둔 검토 칸 — 전부 Rejected 값.
변이 표적: effects의 used/contested를 바꾸면 effects 표 시험이, 주인 검사를 빼면
거절 시험이 각각 죽는다.

Run: python3 agents/slack/test_card_press.py
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import card_press as cp  # noqa: E402
import card_types as cc  # noqa: E402
import card_verdicts  # noqa: E402
import card_view as cv  # noqa: E402

OWNER = "U_OWNER"
OTHER = "U_OTHER"
CARD_TS = "1.0"
CARD_CH = "C1"
SESSION = f"slack:{CARD_CH}:{CARD_TS}"

REPAIR = cc.Repair(
    subject="foodspring-front",
    variants=["foodspring front", "foodspring-front"],
    rows=3218,
    notes=212,
)
PROPOSAL = cc.Proposal(
    subject="주어",
    note="/vault/wiki/wiki-0576.md",
    register="stalled",
    bottleneck="병목 문장 열자 이상입니다",
    advice="조언 문장 열자 이상입니다",
    evidence=[cc.Evidence(note="/vault/wiki/wiki-0576.md", quote="근거 인용문 열두자 이상", line=1)],
)
REVIEW = cc.ProposedVerdict(session_id="sess-agent-1", note="/vault/wiki/wiki-0700.md", kind="used", at="t-1")


def _card_values() -> dict[str, str]:
    """One card per lane, every action_id → its button value."""
    blocks = cv.build_blocks(
        [PROPOSAL], repairs=[REPAIR], repairs_total_groups=1, reviews=[REVIEW], lang="ko"
    )
    return {el["action_id"]: el["value"] for b in blocks if b["type"] == "actions" for el in b["elements"]}


def _payload(action_id: str, value, user: str = OWNER) -> dict:
    return {
        "type": "block_actions",
        "actions": [{"action_id": action_id, "value": value}],
        "user": {"id": user},
        "message": {"ts": CARD_TS},
        "channel": {"id": CARD_CH},
    }


def _press(action_id: str, value) -> cc.Press:
    out = cp.parse_press(_payload(action_id, value), owner_id=OWNER)
    assert not isinstance(out, cc.Rejected), out.reason
    return out


class ValueRoundTripTests(unittest.TestCase):
    """card_view가 싣는 값 = card_press가 읽는 값. 카드의 상태(몇 번째 줄인지, 노트가
    뭐였는지)를 몰라도 버튼 혼자 자기 줄을 설명한다."""

    def setUp(self):
        self.values = _card_values()

    def test_repair_value_round_trips(self):
        press = _press("card:0:do", self.values["card:0:do"])
        self.assertEqual(
            press,
            cc.RepairPress(
                idx=0,
                choice="do",
                user=OWNER,
                card_ts=CARD_TS,
                channel=CARD_CH,
                subject="foodspring-front",
            ),
        )

    def test_advice_value_round_trips(self):
        press = _press("card:1:drop", self.values["card:1:drop"])
        self.assertEqual(
            press,
            cc.AdvicePress(
                idx=1,
                choice="drop",
                user=OWNER,
                card_ts=CARD_TS,
                channel=CARD_CH,
                note="wiki-0576",
            ),
        )

    def test_review_value_round_trips(self):
        press = _press("card:2:do", self.values["card:2:do"])
        self.assertEqual(
            press,
            cc.ReviewPress(
                idx=2,
                choice="do",
                user=OWNER,
                card_ts=CARD_TS,
                channel=CARD_CH,
                session="sess-agent-1",
                note="wiki-0700",
                kind="used",
            ),
        )

    def test_note_label_and_path_are_one_pair(self):
        # 라벨↔경로 쌍은 한 곳만이 안다: 경로→라벨은 카드가, 라벨→경로는 effects가 쓴다.
        path = "/vault/wiki/wiki-0576.md"
        self.assertEqual(cv.note_label(path), "wiki-0576")
        self.assertEqual(cv.note_path("wiki-0576"), path)
        self.assertEqual(cv.note_path(cv.note_label(path)), path)


class EffectsTableTests(unittest.TestCase):
    """줄 × 선택 한 칸씩 — card.py와 다른 프로세스가 공유하는 표 전부가 여기 있다.
    used/contested 를 바꾸는 변이는 이 클래스의 시험이 잡는다."""

    def test_repair_lane(self):
        for choice, expected in (
            ("do", [cc.ExecuteRepair(subject="foodspring-front")]),
            ("defer", []),
            ("drop", []),
        ):
            press = cc.RepairPress(
                idx=0,
                choice=choice,
                user=OWNER,
                card_ts=CARD_TS,
                channel=CARD_CH,
                subject="foodspring-front",
            )
            self.assertEqual(cp.effects(press), expected, choice)

    def test_advice_lane(self):
        base = dict(idx=1, user=OWNER, card_ts=CARD_TS, channel=CARD_CH, note="wiki-0576")
        self.assertEqual(
            cp.effects(cc.AdvicePress(choice="do", **base)),
            [
                cc.Record(event="card_verdict", fields={"card_ts": CARD_TS, "idx": 1, "choice": "do"}),
                cc.Consumption(session=SESSION, kind="used", paths=["/vault/wiki/wiki-0576.md"]),
            ],
        )
        self.assertEqual(
            cp.effects(cc.AdvicePress(choice="drop", **base)),
            [
                cc.Record(event="card_verdict", fields={"card_ts": CARD_TS, "idx": 1, "choice": "drop"}),
                cc.Consumption(session=SESSION, kind="contested", paths=["/vault/wiki/wiki-0576.md"]),
            ],
        )
        # 미뤄: 이벤트만 — 소비 호출 없음, 억제도 없다
        self.assertEqual(
            cp.effects(cc.AdvicePress(choice="defer", **base)),
            [cc.Record(event="card_verdict", fields={"card_ts": CARD_TS, "idx": 1, "choice": "defer"})],
        )

    def test_review_lane(self):
        base = dict(
            idx=2,
            user=OWNER,
            card_ts=CARD_TS,
            channel=CARD_CH,
            session="sess-agent-1",
            note="wiki-0700",
            kind="used",
        )
        fields = {
            "session_id": "sess-agent-1",
            "note": "/vault/wiki/wiki-0700.md",
            "proposed_kind": "used",
            "card_ts": CARD_TS,
        }
        self.assertEqual(
            cp.effects(cc.ReviewPress(choice="do", **base)),
            [cc.Record(event="verdict_reviewed", fields={**fields, "choice": "agree"})],
        )
        # 뒤집기: 반대 판정을 먼저 놓고 같은 이벤트 — 순서가 곧 실행 순서다
        self.assertEqual(
            cp.effects(cc.ReviewPress(choice="drop", **base)),
            [
                cc.Consumption(session="sess-agent-1", kind="contested", paths=["/vault/wiki/wiki-0700.md"]),
                cc.Record(event="verdict_reviewed", fields={**fields, "choice": "flip"}),
            ],
        )
        flipped = dict(base, kind="contested")
        self.assertEqual(
            cp.effects(cc.ReviewPress(choice="drop", **flipped))[0],
            cc.Consumption(session="sess-agent-1", kind="used", paths=["/vault/wiki/wiki-0700.md"]),
        )


class RejectedTests(unittest.TestCase):
    """믿을 수 없는 누름은 언제나 값으로 돌아온다 — 예외는 없다. 주인 검사를 빼는
    변이는 이 클래스의 첫 시험이 잡는다."""

    def setUp(self):
        self.values = _card_values()

    def test_a_non_owner_press_is_rejected_only_when_owner_configured(self):
        out = cp.parse_press(_payload("card:1:do", self.values["card:1:do"], user=OTHER), owner_id=OWNER)
        self.assertIsInstance(out, cc.Rejected)
        out = cp.parse_press(_payload("card:1:do", self.values["card:1:do"], user=OTHER), owner_id=None)
        self.assertIsInstance(out, cc.AdvicePress)

    def test_a_malformed_value_is_rejected(self):
        for bad in ("not json", json.dumps(["not", "a", "dict"]), json.dumps({"note": "wiki-0576"})):
            out = cp.parse_press(_payload("card:1:do", bad), owner_id=OWNER)
            self.assertIsInstance(out, cc.Rejected)
            self.assertEqual(out.reason, "malformed button value")

    def test_an_unknown_lane_is_rejected(self):
        value = json.dumps({"lane": "mystery", "subject": "x"})
        out = cp.parse_press(_payload("card:0:do", value), owner_id=OWNER)
        self.assertIsInstance(out, cc.Rejected)
        self.assertIn("unknown lane", out.reason)

    def test_a_review_lane_defer_is_rejected(self):
        # 검토 칸엔 맞음/뒤집기 두 버튼뿐이니, defer 누름은 가짜다 — 실행하면 안 된다.
        out = cp.parse_press(_payload("card:2:defer", self.values["card:2:do"]), owner_id=OWNER)
        self.assertIsInstance(out, cc.Rejected)
        self.assertEqual(out.reason, "review lane has no defer")

    def test_a_weird_envelope_is_rejected_not_raised(self):
        out = cp.parse_press({"type": "reaction_added"}, owner_id=OWNER)
        self.assertIsInstance(out, cc.Rejected)

    def test_malformed_envelope_shapes_are_rejected_not_raised(self):
        # message이 문자열이거나 channel이 목록이면 .get 에서 AttributeError — 이상한
        # 페이로드는 언제나 Rejected 값이지 예외가 아니다.
        value = self.values["card:1:do"]
        for payload in (
            {**_payload("card:1:do", value), "message": "not-a-dict"},
            {**_payload("card:1:do", value), "channel": ["C1"]},
            {**_payload("card:1:do", value), "actions": "not-a-list"},
        ):
            out = cp.parse_press(payload, owner_id=OWNER)
            self.assertIsInstance(out, cc.Rejected)

    def test_a_press_without_card_ts_or_channel_is_rejected(self):
        # card_ts "" 는 card_verdict 를 고아로 만들어 다음 카드가 배를 거부하고, 채널
        # 없음은 소비 세션을 "slack::" 로 만든다 — 받지 않는 게 낫다.
        value = self.values["card:1:do"]
        out = cp.parse_press({**_payload("card:1:do", value), "message": {}}, owner_id=OWNER)
        self.assertIsInstance(out, cc.Rejected)
        self.assertEqual(out.reason, "no card ts")
        out = cp.parse_press({**_payload("card:1:do", value), "channel": {}}, owner_id=OWNER)
        self.assertIsInstance(out, cc.Rejected)
        self.assertEqual(out.reason, "no channel")

    def test_a_negative_idx_is_rejected_in_both_parsers(self):
        value = self.values["card:0:do"]
        out = cp.parse_press(_payload("card:-5:do", value), owner_id=OWNER)
        self.assertIsInstance(out, cc.Rejected)
        self.assertEqual(out.reason, "no proposal -5")
        out = card_verdicts.parse_action(_payload("card:-5:do", value), owner_id=OWNER, n_total=3)
        self.assertIsInstance(out, cc.Rejected)

    def test_a_note_label_with_a_slash_is_rejected(self):
        # 라벨에 "/" 가 들어가면 note_label이 낼 수 있는 모양이 아니다 — 경로 새는
        # 구멍으로 보고 받지 않는다.
        for note in ("../../etc/passwd", "/abs/other.md"):
            value = json.dumps({"lane": "advice", "note": note})
            out = cp.parse_press(_payload("card:1:do", value), owner_id=OWNER)
            self.assertIsInstance(out, cc.Rejected)
            self.assertIn("malformed note label", out.reason)


if __name__ == "__main__":
    unittest.main()
