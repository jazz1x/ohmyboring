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


def _repair_reviewed(choice: str) -> cc.Record:
    return cc.Record(
        event="repair_reviewed",
        fields={"card_ts": CARD_TS, "idx": 0, "choice": choice, "subject": "foodspring-front"},
    )


class EffectsTableTests(unittest.TestCase):
    """줄 × 선택 한 칸씩 — card.py와 다른 프로세스가 공유하는 표 전부가 여기 있다.
    used/contested 를 바꾸는 변이는 이 클래스의 시험이 잡는다."""

    def test_repair_lane(self):
        for choice, expected in (
            ("do", [cc.ExecuteRepair(subject="foodspring-front")]),
            ("defer", [_repair_reviewed("defer")]),
            ("drop", [_repair_reviewed("drop")]),
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

    def test_review_lane_delegate_writes_the_agent_judge_never_owner(self):
        # 맡길게요: 모델 판정을 그대로 받는다 — 판정한 세션에 모델이 정한 종류의 간선이되
        # judge 는 agent:delegated, 사건에는 판정 종류와 이유 한 줄. owner 를 쓰는 변이와
        # 낱말 표지(proposed kind)를 그대로 도장 찍는 변이는 이 시험이 빨갛게 끝낸다.
        press = cc.ReviewPress(
            idx=2,
            choice="delegate",
            user=OWNER,
            card_ts=CARD_TS,
            channel=CARD_CH,
            session="sess-agent-1",
            note="wiki-0700",
            kind="contested",
        )
        # 모델이 '맞다' — 판정 종류는 제안 그대로
        effects = cp.effects(press, delegated=cc.DelegatedJudgment(kind="contested", reason="모델 이유"))
        self.assertEqual(
            effects,
            [
                cc.Consumption(
                    session="sess-agent-1",
                    kind="contested",
                    paths=["/vault/wiki/wiki-0700.md"],
                    judge="agent:delegated",
                ),
                cc.Record(
                    event="verdict_reviewed",
                    fields={
                        "session_id": "sess-agent-1",
                        "note": "/vault/wiki/wiki-0700.md",
                        "proposed_kind": "contested",
                        "card_ts": CARD_TS,
                        "choice": "delegate",
                        "judge": "agent:delegated",
                        "kind": "contested",
                        "reason": "모델 이유",
                    },
                ),
            ],
        )
        consumption = effects[0]
        self.assertEqual(consumption.judge, "agent:delegated")
        self.assertNotEqual(consumption.judge, "owner")
        # 모델이 '틀리다' — proposed kind 과 다른 간선: proposed 를 도장 찍는 변이는 여기서 갈린다
        flipped = cp.effects(press, delegated=cc.DelegatedJudgment(kind="used", reason="실제로는 쓰였다"))
        self.assertEqual(flipped[0].kind, "used")
        self.assertEqual(flipped[1].fields["kind"], "used")
        # 판정 값 없는 delegate 누름은 표를 부르는 쪽의 버그 — loud 하게 거절
        with self.assertRaises(ValueError):
            cp.effects(press)

    def test_review_lane_delegate_failure_leaves_no_edge_and_one_event(self):
        # 모델이 못 답하면 판정을 남기지 않는다 — 간선 0, 사건 한 줄에 그 사실. 조용히
        # 낱말 표지로 되돌아가는 변이(제안 종류의 간선을 놓는 변이)는 여기서 잡힌다.
        press = cc.ReviewPress(
            idx=2,
            choice="delegate",
            user=OWNER,
            card_ts=CARD_TS,
            channel=CARD_CH,
            session="sess-agent-1",
            note="wiki-0700",
            kind="used",
        )
        effects = cp.effects(press, delegated=cc.DelegationFailed(reason="모델 답이 JSON 이 아니다"))
        self.assertEqual(
            effects,
            [
                cc.Record(
                    event="verdict_reviewed",
                    fields={
                        "session_id": "sess-agent-1",
                        "note": "/vault/wiki/wiki-0700.md",
                        "proposed_kind": "used",
                        "card_ts": CARD_TS,
                        "choice": "delegate",
                        "judge": "agent:delegated",
                        "error": "모델 답이 JSON 이 아니다",
                    },
                )
            ],
        )
        self.assertFalse(any(isinstance(e, cc.Consumption) for e in effects))

    def test_review_lane_hold_leaves_no_edge_and_one_event(self):
        # 보류: 판정을 남기지 않는다 — 간선 없이 사건 한 줄, 묶인 세션 전부가 그 한 줄에 실린다.
        press = cc.ReviewPress(
            idx=2,
            choice="defer",
            user=OWNER,
            card_ts=CARD_TS,
            channel=CARD_CH,
            session="sess-agent-1",
            sessions=["sess-agent-1", "sess-agent-2"],
            note="wiki-0700",
            kind="used",
        )
        self.assertEqual(
            cp.effects(press),
            [
                cc.Record(
                    event="verdict_reviewed",
                    fields={
                        "session_id": "sess-agent-1",
                        "sessions": ["sess-agent-1", "sess-agent-2"],
                        "note": "/vault/wiki/wiki-0700.md",
                        "proposed_kind": "used",
                        "card_ts": CARD_TS,
                        "choice": "defer",
                    },
                )
            ],
        )

    def test_a_grouped_press_fans_out_to_every_session(self):
        # 묶인 줄의 판정은 묶인 짝 전부에 간다 — 작으면 한 번에 같이 간다.
        base = dict(
            idx=2,
            user=OWNER,
            card_ts=CARD_TS,
            channel=CARD_CH,
            session="sess-agent-1",
            sessions=["sess-agent-1", "sess-agent-2"],
            note="wiki-2498",
            kind="contested",
        )
        path = "/vault/wiki/wiki-2498.md"
        fields1 = {
            "session_id": "sess-agent-1",
            "note": path,
            "proposed_kind": "contested",
            "card_ts": CARD_TS,
        }
        fields2 = {**fields1, "session_id": "sess-agent-2"}
        self.assertEqual(
            cp.effects(cc.ReviewPress(choice="do", **base)),
            [
                cc.Record(event="verdict_reviewed", fields={**fields1, "choice": "agree"}),
                cc.Record(event="verdict_reviewed", fields={**fields2, "choice": "agree"}),
            ],
        )
        self.assertEqual(
            cp.effects(cc.ReviewPress(choice="drop", **base)),
            [
                cc.Consumption(session="sess-agent-1", kind="used", paths=[path]),
                cc.Record(event="verdict_reviewed", fields={**fields1, "choice": "flip"}),
                cc.Consumption(session="sess-agent-2", kind="used", paths=[path]),
                cc.Record(event="verdict_reviewed", fields={**fields2, "choice": "flip"}),
            ],
        )
        # 맡긴 판정은 묶인 세션 전부에 가고, 판정 종류·이유는 모델의 것이 그대로다
        judgment = cc.DelegatedJudgment(kind="used", reason="모델이 본 이유")
        self.assertEqual(
            cp.effects(cc.ReviewPress(choice="delegate", **base), delegated=judgment),
            [
                cc.Consumption(session="sess-agent-1", kind="used", paths=[path], judge="agent:delegated"),
                cc.Record(
                    event="verdict_reviewed",
                    fields={
                        **fields1,
                        "choice": "delegate",
                        "judge": "agent:delegated",
                        "kind": "used",
                        "reason": "모델이 본 이유",
                    },
                ),
                cc.Consumption(session="sess-agent-2", kind="used", paths=[path], judge="agent:delegated"),
                cc.Record(
                    event="verdict_reviewed",
                    fields={
                        **fields2,
                        "choice": "delegate",
                        "judge": "agent:delegated",
                        "kind": "used",
                        "reason": "모델이 본 이유",
                    },
                ),
            ],
        )
        # 묶이지 않은 옛 값(세션 하나, sessions 없음)은 그대로 한 세션에만 간다.
        legacy = cc.ReviewPress(
            idx=2,
            choice="do",
            user=OWNER,
            card_ts=CARD_TS,
            channel=CARD_CH,
            session="sess-agent-1",
            note="wiki-2498",
            kind="contested",
        )
        self.assertEqual(
            cp.effects(legacy),
            [cc.Record(event="verdict_reviewed", fields={**fields1, "choice": "agree"})],
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

    def test_a_delegate_press_is_refused_outside_the_review_lane(self):
        # delegate 는 검토 칸의 넷째 버튼에서만 온다 — execute·조언 칸에서 지어낸 delegate
        # 누름은 받지 않는다. 검토 칸의 defer(보류)는 온전한 누름이다.
        out = cp.parse_press(_payload("card:1:delegate", self.values["card:1:do"]), owner_id=OWNER)
        self.assertIsInstance(out, cc.Rejected)
        self.assertEqual(out.reason, "advice lane has no delegate")
        out = cp.parse_press(_payload("card:0:delegate", self.values["card:0:do"]), owner_id=OWNER)
        self.assertIsInstance(out, cc.Rejected)
        self.assertEqual(out.reason, "repair lane has no delegate")
        out = cp.parse_press(_payload("card:2:defer", self.values["card:2:do"]), owner_id=OWNER)
        self.assertIsInstance(out, cc.ReviewPress)
        self.assertEqual(out.choice, "defer")

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
            {**_payload("card:1:do", value), "user": "U1"},
            {**_payload("card:1:do", value), "user": {"id": 5}},
            {**_payload("card:1:do", value), "actions": [{"action_id": 5, "value": value}]},
        ):
            out = cp.parse_press(payload, owner_id=OWNER)
            self.assertIsInstance(out, cc.Rejected)
        # with no owner configured, the owner check cannot catch a non-string id for us
        numeric_id = {**_payload("card:1:do", value), "user": {"id": 5}}
        out = card_verdicts.parse_action(numeric_id, owner_id=None, n_total=3)
        self.assertEqual(out, cc.Rejected(reason="no user"))

    def test_a_press_without_card_ts_or_channel_is_rejected(self):
        # 짝 없는 card_verdict 는 다음 날 카드를 멈추고, 채널 없음은 소비 세션을
        # "slack::" 로 만든다 — 받지 않는 게 낫다.
        value = self.values["card:1:do"]
        out = cp.parse_press({**_payload("card:1:do", value), "message": {}}, owner_id=OWNER)
        self.assertIsInstance(out, cc.Rejected)
        self.assertEqual(out.reason, "no card ts")
        out = cp.parse_press({**_payload("card:1:do", value), "channel": {}}, owner_id=OWNER)
        self.assertIsInstance(out, cc.Rejected)
        self.assertEqual(out.reason, "no channel")

    def test_the_channel_falls_back_to_the_container(self):
        value = self.values["card:1:do"]
        payload = {**_payload("card:1:do", value), "channel": {}, "container": {"channel_id": "C9"}}
        out = cp.parse_press(payload, owner_id=OWNER)
        self.assertEqual(out.channel, "C9")

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
