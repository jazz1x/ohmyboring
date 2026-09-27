#!/usr/bin/env python3
"""카드 누름 해석 — 버튼이 자기 줄을 싣고, 한 순수 함수가 누름이 뭘 하는지 결정한다.

카드 버튼의 `value`(`{lane, …}` 압축 JSON, card_view가 만든다)에 줄 데이터가 실려 있어
카드의 in-memory 상태 없이도 누름이 해석된다 — card.py와 같은 버튼을 받는 다른 프로세스
(hermes 플러그인)가 `effects`라는 한 결정표를 공유한다(사유: wiki-2049 — 슬랙 앱 하나,
hermes가 전부 받는다). parse_press는 슬랙 페이로드를 Press 값으로, effects는 Press를
Effect 값의 목록으로 바꾼다 — 둘 다 순수하고, 못 믿는 누름은 언제나 Rejected 값이다.
이상한 페이로드(모양이 이상한 message·channel·actions)도 예외가 아니라 거절이다.
"""

from __future__ import annotations

import json

import card_types
import card_verdicts
import card_view
from card_types import AdvicePress, Press, Rejected, RepairPress, ReviewPress

#: 버튼 value의 lane → Press 모형 — parse_press가 여기서 모형을 고른다.
_LANE_MODELS = {"repair": RepairPress, "advice": AdvicePress, "review": ReviewPress}

#: 노트 라벨은 평탄한 /vault/wiki/<이름>.md 의 짧은 이름(예: wiki-0576) — "/" 가 들어간
#: 라벨은 note_label이 낼 수 있는 모양이 아니니 경로 새는 구멍으로 본다.
_NOTE_LANES = ("advice", "review")


def session_name(channel: str, ts: str) -> str:
    """The engine's name for a Slack card — the key /handover wrote and /consumption
    judges. One place; card.py imports this."""
    return f"slack:{channel}:{ts}"


def parse_press(payload: dict, owner_id: str | None) -> Press | Rejected:
    """A block_actions payload in, one press out — or Rejected with the reason. The shared
    envelope/owner checks live in card_verdicts.parse_action_common; this layer reads the
    button value (`message.ts` as card_ts, the channel id — `channel.id`, else
    `container.channel_id`) and validates it against the lane model. Notes stay in the
    `wiki-NNNN` label form the card carried, and a label must look exactly like what
    note_label produces for a flat /vault/wiki/<name>.md note. Never raises: every
    malformed shape — envelope, value, lane, label, missing ts or channel, a review lane
    asked to defer, a non-owner — is a Rejected, because a press that cannot write a
    trustworthy card_ts would orphan the next morning's join and refuse that card."""
    common = card_verdicts.parse_action_common(payload, owner_id=owner_id)
    if isinstance(common, Rejected):
        return common
    (idx, choice), user = common
    message = payload.get("message")
    card_ts = message.get("ts") if isinstance(message, dict) else None
    if not isinstance(card_ts, str) or not card_ts:
        # 짝 없는 card_verdict 가 남으면 다음 날 카드가 나가지 못한다
        return Rejected(reason="no card ts")
    channel = payload.get("channel")
    channel_id = channel.get("id") if isinstance(channel, dict) else None
    if not channel_id:
        container = payload.get("container")
        channel_id = container.get("channel_id") if isinstance(container, dict) else None
    if not isinstance(channel_id, str) or not channel_id:
        return Rejected(reason="no channel")
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
    if lane in _NOTE_LANES:
        note = value.get("note")
        if not isinstance(note, str) or not note or "/" in note:
            return Rejected(reason=f"malformed note label {note!r}")
    try:
        press = model(
            idx=idx,
            choice=choice,
            user=user,
            card_ts=card_ts,
            channel=channel_id,
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
                session=session_name(press.channel, press.card_ts),
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
