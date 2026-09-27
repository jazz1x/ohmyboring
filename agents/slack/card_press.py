#!/usr/bin/env python3
"""카드 누름 해석 — 버튼이 자기 줄을 싣고, 한 순수 함수가 누름이 뭘 하는지 결정한다.

카드 버튼의 `value`(`{lane, …}` 압축 JSON, card_view가 만든다)에 줄 데이터가 실려 있어
카드의 in-memory 상태 없이도 누름이 해석된다 — card.py와 같은 버튼을 받는 다른 프로세스
(hermes 플러그인)가 `effects`라는 한 결정표를 공유한다(사유: wiki-2049 — 슬랙 앱 하나,
hermes가 전부 받는다). parse_press는 슬랙 페이로드를 Press 값으로, effects는 Press를
Effect 값의 목록으로 바꾼다 — 둘 다 순수하고, 못 믿는 누름은 언제나 Rejected 값이다.
"""

from __future__ import annotations

import json

import card_types
import card_verdicts
import card_view
from card_types import AdvicePress, Press, Rejected, RepairPress, ReviewPress

#: 버튼 value의 lane → Press 모형 — parse_press가 여기서 모형을 고른다.
_LANE_MODELS = {"repair": RepairPress, "advice": AdvicePress, "review": ReviewPress}


def parse_press(payload: dict, owner_id: str | None = None) -> Press | Rejected:
    """A block_actions payload in, one press out — or Rejected with the reason. The shared
    envelope/owner checks live in card_verdicts.parse_action_common; this layer reads the
    button value (`message.ts` as card_ts, the channel id) and validates it against the lane
    model. Notes stay in the `wiki-NNNN` label form the card carried. Never raises:
    a malformed value, an unknown lane, a review lane asked to defer, a non-owner — each is
    a Rejected, like every other untrustworthy press."""
    common = card_verdicts.parse_action_common(payload, owner_id=owner_id)
    if isinstance(common, Rejected):
        return common
    (idx, choice), user = common
    try:
        value = json.loads(payload["actions"][0].get("value"))
    except (TypeError, ValueError):
        return Rejected(reason="malformed button value")
    if not isinstance(value, dict):
        return Rejected(reason="malformed button value")
    lane = value.get("lane")
    if not isinstance(lane, str):
        return Rejected(reason="malformed button value")
    model = _LANE_MODELS.get(lane)
    if model is None:
        return Rejected(reason=f"unknown lane {lane!r}")
    try:
        press = model(
            idx=idx,
            choice=choice,
            user=user,
            card_ts=(payload.get("message") or {}).get("ts") or "",
            channel=(payload.get("channel") or {}).get("id") or "",
            **value,
        )
    except (TypeError, ValueError):  # 모형 검증 실패 / value 키가 공통 필드를 덮음
        return Rejected(reason="malformed button value")
    if isinstance(press, ReviewPress) and press.choice == "defer":
        return Rejected(reason="review lane has no defer")
    return press


def effects(press: Press) -> list[card_types.Effect]:
    """The one decision table: press in, what it does out. Both processes that can receive
    the same press (card.py, the hermes plugin) fold this list through their own
    collaborators — the list's order is the execution order. Pure: no I/O, never raises.
    Notes expand back to `/vault/wiki/...` paths here, never in the press itself."""
    if isinstance(press, RepairPress):
        if press.choice != "do":
            return []  # hold/reject leave no trace at all — nothing to suppress in this lane
        return [card_types.ExecuteRepair(subject=press.subject)]
    if isinstance(press, AdvicePress):
        out = [
            card_types.Record(
                event="card_verdict", fields=card_verdicts.verdict_event_fields(press, press.card_ts)
            )
        ]
        if press.choice == "defer":
            return out  # the event, but no consumption call — never suppresses
        return [
            *out,
            card_types.Consumption(
                session=f"slack:{press.channel}:{press.card_ts}",
                kind="used" if press.choice == "do" else "contested",
                paths=[card_view.note_path(press.note)],
            ),
        ]
    fields = {
        "session_id": press.session,
        "note": card_view.note_path(press.note),
        "proposed_kind": press.kind,
        "card_ts": press.card_ts,
    }
    if press.choice == "do":
        return [card_types.Record(event="verdict_reviewed", fields={**fields, "choice": "agree"})]
    return [
        card_types.Consumption(
            session=press.session,
            kind="contested" if press.kind == "used" else "used",
            paths=[card_view.note_path(press.note)],
        ),
        card_types.Record(event="verdict_reviewed", fields={**fields, "choice": "flip"}),
    ]
