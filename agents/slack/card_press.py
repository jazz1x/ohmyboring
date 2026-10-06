#!/usr/bin/env python3
"""카드 누름 해석 — 버튼이 자기 줄을 싣고, 한 순수 함수가 누름이 뭘 하는지 결정한다.

카드 버튼의 `value`(`{lane, …}` 압축 JSON, card_view가 만든다)에 줄 데이터가 실려 있어
카드의 in-memory 상태 없이도 누름이 해석된다 — card.py와 같은 버튼을 받는 다른 프로세스
(hermes 플러그인)가 `effects`라는 한 결정표를 공유한다(사유: wiki-2049 — 슬랙 앱 하나,
hermes가 전부 받는다). parse_press는 슬랙 페이로드를 Press 값으로, effects는 Press를
Effect 값의 목록으로 바꾼다 — 둘 다 순수하고, 못 믿는 누름은 언제나 Rejected 값이다.
이상한 페이로드(모양이 이상한 message·channel·actions)도 예외가 아니라 거절이다.

이름 맞추기·검토 줄의 버튼은 value 를 들지 않는다 — 줄 값은 게시 때 card_row 사건 한 줄로
남고, 누름은 (card_ts, idx)로 그것을 읽어(row_from_entries) parse_press 에 다시 넣는다.
value 를 든 옛 카드의 버튼과 조언·표본 줄은 이전 그대로 value 를 읽는다. 「💬 코멘트」는
판정이 아니라 모달을 여는 누름이라 이 표 밖(parse_comment_open·parse_comment_submit·
comment_fields)이고, 판정이 그 글을 읽는 자리(owner_comments·comment_prompt_lines)도 여기다.
"""

from __future__ import annotations

import json
import os

import card_types
import card_verdicts
import card_view
from card_types import (
    CHOICES,
    COMMENT,
    COMMENT_ACTION_ID,
    COMMENT_BLOCK_ID,
    COMMENT_CALLBACK_ID,
    COMMENT_MAX_CHARS,
    DELEGATE,
    AdvicePress,
    CommentOpen,
    CommentSubmit,
    NeedsRow,
    OwnerComment,
    Press,
    Rejected,
    RepairPress,
    ReviewPress,
    RowUnread,
)

from ohmyboring.adapters.engine import OWNER

#: 버튼 value의 lane → Press 모형 — parse_press가 여기서 모형을 고른다.
_LANE_MODELS = {"repair": RepairPress, "advice": AdvicePress, "review": ReviewPress}

#: 각 칸이 받는 선택 어휘 — 검토 칸만 맡길게요(DELEGATE)가 있다. execute·조언 칸의 버튼은
#: 여전히 셋뿐이니, 칸 밖에서 지어낸 delegate 누름은 여기서 거절된다.
_LANE_CHOICES = {
    "repair": CHOICES,
    "advice": CHOICES,
    "review": (*CHOICES, DELEGATE),
}

#: 노트 라벨은 평탄한 /vault/wiki/<이름>.md 의 짧은 이름(예: wiki-0576) — "/" 가 들어간
#: 라벨은 note_label이 낼 수 있는 모양이 아니니 경로 새는 구멍으로 본다.
_NOTE_LANES = ("advice", "review")

#: 확인용 표본 줄이 받는 선택 — card_view.SAMPLE_CHOICES 와 같은 둘.
_SAMPLE_CHOICES = ("do", "drop")


#: How long a posted card's buttons are answered. The next card reads its past proposals
#: this much wider than its verdict window, so a press older than this would leave a verdict
#: whose proposal it can no longer see — and that card would refuse to ship.
CARD_ANSWERABLE_HOURS = float(os.environ.get("CARD_ANSWERABLE_HOURS") or "23")


def answerable(card_ts: str, now: float) -> bool:
    """Whether a press on the card posted at `card_ts` (Slack ts, epoch seconds) still counts."""
    return now - float(card_ts) <= CARD_ANSWERABLE_HOURS * 3600


def session_name(channel: str, ts: str) -> str:
    """The engine's name for a Slack card — the key /handover wrote and /consumption
    judges. One place; card.py imports this."""
    return f"slack:{channel}:{ts}"


def _card_where(payload: dict) -> tuple[str, str] | Rejected:
    """(card_ts, channel id) of the card a block_actions payload came from — `message.ts` and
    `channel.id`, else `container.channel_id`. A press that cannot name both would orphan the
    next morning's join (짝 없는 card_verdict 가 남으면 다음 날 카드가 나가지 못한다)."""
    message = payload.get("message")
    card_ts = message.get("ts") if isinstance(message, dict) else None
    if not isinstance(card_ts, str) or not card_ts:
        return Rejected(reason="no card ts")
    channel = payload.get("channel")
    channel_id = channel.get("id") if isinstance(channel, dict) else None
    if not channel_id:
        container = payload.get("container")
        channel_id = container.get("channel_id") if isinstance(container, dict) else None
    if not isinstance(channel_id, str) or not channel_id:
        return Rejected(reason="no channel")
    return card_ts, channel_id


def parse_press(payload: dict, owner_id: str | None, row: dict | None = None) -> Press | NeedsRow | Rejected:
    """A block_actions payload in, one press out — or Rejected with the reason. The shared
    envelope/owner checks live in card_verdicts.parse_action_common; this layer reads the
    row's value and validates it against the lane model. The value is the button's own when
    it carries one (a card posted before card_row: the old way), else `row` — the card_row
    value the caller read by (card_ts, idx); a valueless button with no `row` yet is
    NeedsRow, the one answer that says "read the row, then ask again". Notes stay in the
    `wiki-NNNN` label form the card carried, and a label must look exactly like what
    note_label produces for a flat /vault/wiki/<name>.md note. Never raises: every
    malformed shape — envelope, value, lane, label, missing ts or channel, a choice the
    lane's buttons never emitted (execute·조언 칸의 delegate), a non-owner — is a Rejected,
    because a press that cannot write a trustworthy card_ts would orphan the next morning's
    join and refuse that card."""
    common = card_verdicts.parse_action_common(payload, owner_id=owner_id)
    if isinstance(common, Rejected):
        return common
    (idx, choice), user = common
    where = _card_where(payload)
    if isinstance(where, Rejected):
        return where
    card_ts, channel_id = where
    raw = payload["actions"][0].get("value")
    if raw is None or raw == "":
        if row is None:
            return NeedsRow(idx=idx, choice=choice, user=user, card_ts=card_ts, channel=channel_id)
        value = row
    else:
        try:
            value = json.loads(raw)
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
    if choice not in _LANE_CHOICES[lane]:
        return Rejected(reason=f"{lane} lane has no {choice}")
    if value.get("sample") is not None and (lane != "review" or choice not in _SAMPLE_CHOICES):
        # 확인 표본 줄의 버튼은 맞아요/아니에요뿐 — 맡길게요·보류는 오너 표본이 아니다
        return Rejected(reason=f"sample row has no {choice}")
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
    return press


def is_comment_action(payload: dict) -> bool:
    """Whether a block_actions payload is a 「💬 코멘트」 press — by its action_id's last word."""
    actions = payload.get("actions")
    first = actions[0] if isinstance(actions, list) and actions else None
    action_id = first.get("action_id") if isinstance(first, dict) else None
    return isinstance(action_id, str) and action_id.endswith(f":{COMMENT}")


def _owner_user(payload: dict, owner_id: str | None) -> str | Rejected:
    user_obj = payload.get("user")
    user = user_obj.get("id") if isinstance(user_obj, dict) else None
    if not isinstance(user, str) or not user:
        return Rejected(reason="no user")
    if owner_id is not None and user != owner_id:
        return Rejected(reason=f"user {user} is not the owner")
    return user


def parse_comment_open(payload: dict, owner_id: str | None) -> CommentOpen | Rejected:
    """A 「💬 코멘트」 press in, the modal's order out — or Rejected. Needs only the row's key
    and the click's trigger_id: the row itself is read when the text comes back."""
    if payload.get("type") != "block_actions":
        return Rejected(reason="not block_actions")
    actions = payload.get("actions")
    if not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], dict):
        return Rejected(reason="not exactly one action")
    action_id = str(actions[0].get("action_id"))
    parts = action_id.split(":")
    if len(parts) != 3 or parts[0] != "card" or parts[2] != COMMENT or not parts[1].isdigit():
        return Rejected(reason=f"unknown action_id {action_id!r}")
    user = _owner_user(payload, owner_id)
    if isinstance(user, Rejected):
        return user
    where = _card_where(payload)
    if isinstance(where, Rejected):
        return where
    trigger_id = payload.get("trigger_id")
    if not isinstance(trigger_id, str) or not trigger_id:
        return Rejected(reason="no trigger_id")
    return CommentOpen(
        idx=int(parts[1]), user=user, card_ts=where[0], channel=where[1], trigger_id=trigger_id
    )


def comment_metadata(opened: CommentOpen) -> str:
    """The modal's private_metadata — the row's key, nothing else."""
    return json.dumps({"card_ts": opened.card_ts, "channel": opened.channel, "idx": opened.idx})


def parse_comment_submit(payload: dict, owner_id: str | None) -> CommentSubmit | Rejected:
    """A view_submission payload in, the owner's comment out — or Rejected. The row's key
    comes back from private_metadata, the text from the modal's one input."""
    if payload.get("type") != "view_submission":
        return Rejected(reason="not view_submission")
    view = payload.get("view")
    if not isinstance(view, dict) or view.get("callback_id") != COMMENT_CALLBACK_ID:
        return Rejected(reason="not the comment modal")
    user = _owner_user(payload, owner_id)
    if isinstance(user, Rejected):
        return user
    try:
        meta = json.loads(view.get("private_metadata"))
        card_ts, channel, idx = meta["card_ts"], meta["channel"], meta["idx"]
        text = view["state"]["values"][COMMENT_BLOCK_ID][COMMENT_ACTION_ID]["value"]
    except (TypeError, ValueError, KeyError):
        return Rejected(reason="malformed comment modal")
    if not (isinstance(card_ts, str) and card_ts and isinstance(channel, str) and channel):
        return Rejected(reason="malformed comment modal")
    if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0 or not isinstance(text, str):
        return Rejected(reason="malformed comment modal")
    text = text.strip()
    if not text:
        return Rejected(reason="empty comment")
    if len(text) > COMMENT_MAX_CHARS:
        return Rejected(reason=f"comment over {COMMENT_MAX_CHARS} chars")
    return CommentSubmit(idx=idx, user=user, card_ts=card_ts, channel=channel, text=text)


def row_from_entries(entries: list[dict], card_ts: str, idx: int) -> dict | RowUnread:
    """The card_row value for (card_ts, idx) out of /events entries — the newest one when a
    row was written twice. No such row, or a row whose value is no object, is RowUnread: the
    caller shows the failure instead of guessing a lane."""
    found = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("attributes"), dict)
        and entry["attributes"].get("card_ts") == card_ts
        and entry["attributes"].get("idx") == idx
    ]
    if not found:
        return RowUnread(reason=f"card_row 가 없다: card_ts={card_ts} idx={idx}")
    value = max(found, key=lambda entry: str(entry.get("observed_at")))["attributes"].get("value")
    if not isinstance(value, dict):
        return RowUnread(reason=f"card_row 의 value 가 객체가 아니다: card_ts={card_ts} idx={idx}")
    return value


def comment_fields(submit: CommentSubmit, row: dict) -> dict | Rejected:
    """The card_comment 사건's fields for one submission on `row` (a card_row value). The
    owner is the judge — this is the one place the word is written. Lane keys: a review row
    names its session(s) and note, an 이름 맞추기 row its subject and spellings; any other
    lane has no comment path."""
    base = {"judge": OWNER, "card_ts": submit.card_ts, "idx": submit.idx, "text": submit.text}
    lane = row.get("lane")
    if lane == "review":
        note, session = row.get("note"), row.get("session")
        if (
            not isinstance(note, str)
            or not note
            or "/" in note
            or not isinstance(session, str)
            or not session
        ):
            return Rejected(reason="review row without session or note")
        sessions = row.get("sessions")
        return {
            **base,
            "lane": "review",
            "session_id": session,
            "sessions": list(sessions) if isinstance(sessions, list) and sessions else [session],
            "note": card_view.note_path(note),
            "proposed_kind": row.get("kind"),
        }
    if lane == "repair":
        subject = row.get("subject")
        if not isinstance(subject, str) or not subject:
            return Rejected(reason="repair row without subject")
        variants = row.get("variants")
        return {
            **base,
            "lane": "repair",
            "subject": subject,
            "variants": variants if isinstance(variants, list) else [],
        }
    return Rejected(reason=f"{lane!r} rows take no comment")


def owner_comments(entries: list[dict], lane: str, **key: object) -> list[OwnerComment]:
    """The owner's comments on one lane whose 사건 fields equal `key` (note=… / subject=…),
    newest first — what a judge reads before it judges. Only judge=owner rows count: nothing
    else may speak in the owner's voice. A matching row missing its id, time or text raises
    ValueError (a dropped comment would be an owner's word silently unread)."""
    out: list[OwnerComment] = []
    for entry in entries:
        attrs = entry.get("attributes") if isinstance(entry, dict) else None
        if not isinstance(attrs, dict) or attrs.get("judge") != OWNER or attrs.get("lane") != lane:
            continue
        if any(attrs.get(name) != want for name, want in key.items()):
            continue
        ident, at, text = entry.get("id"), entry.get("observed_at"), attrs.get("text")
        if not isinstance(ident, int) or not isinstance(at, str) or not isinstance(text, str) or not text:
            raise ValueError(f"malformed card_comment row: {entry!r}")
        out.append(OwnerComment(id=ident, at=at, text=text))
    return sorted(out, key=lambda comment: comment.at, reverse=True)


def comment_prompt_lines(comments: list[OwnerComment], header: str) -> list[str]:
    """The owner's words as prompt lines under `header`: verbatim, newest first, a multi-line
    comment indented under its dash. No comments, no lines — the prompt stays what it was."""
    if not comments:
        return []
    lines = ["", header]
    for comment in comments:
        first, *rest = comment.text.split("\n")
        lines.append(f"- {first}")
        lines.extend(f"  {line}" for line in rest)
    return lines


def effects(press: Press, delegated: card_types.Delegated | None = None) -> list[card_types.Effect]:
    """The one decision table: press in, what it does out. Both processes that can receive
    the same press (card.py, the hermes plugin) fold this list through their own
    collaborators — the list's order is the execution order. Pure: no I/O, never raises.
    Notes expand back to `/vault/wiki/...` paths here, never in the press itself.

    `delegated` is the model's judgment for a review-lane 「맡길게요」 press, judged before
    the table runs (한 누름의 모델 호출 상한 1 — the call happens once, in the plugin's
    judgment seat, and this pure table only carries the value). A delegate press without
    it is a programming error, and a DelegationFailed value writes no 판정 edge — only the
    사건 one line that says the model could not judge. judge on every delegated edge is
    the agent lineage (AGENT_DELEGATED), never owner: the owner handed this call back."""
    if isinstance(press, RepairPress):
        if press.choice != "do":
            return [
                card_types.Record(
                    event="repair_reviewed",
                    fields={
                        "card_ts": press.card_ts,
                        "idx": press.idx,
                        "choice": press.choice,
                        "subject": press.subject,
                    },
                )
            ]
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
        "note": card_view.note_path(press.note),
        "proposed_kind": press.kind,
        "card_ts": press.card_ts,
    }
    sessions = list(press.sessions) or [press.session]
    if press.sample is not None:
        fields = {**fields, "sample": press.sample}
    if press.choice == "defer":
        # 보류: 판정을 남기지 않는다 — 간선 없이 사건 한 줄. 묶인 세션 전부가 이 한 줄에
        # 실리고, card_live 가 이 사건을 읽어 이레 창(고른 짝은 이레 동안 다시 안 올라옴,
        # card_verdicts.SUPPRESS_WINDOW_HOURS 와 같은 창) 동안 이 짝을 다시 올리지 않는다.
        return [
            card_types.Record(
                event="verdict_reviewed",
                fields={**fields, "session_id": press.session, "sessions": sessions, "choice": "defer"},
            )
        ]
    if press.choice == "delegate":
        return _delegate_effects(press, fields, sessions, delegated)
    out: list[card_types.Effect] = []
    for sid in sessions:
        row_fields = {**fields, "session_id": sid}
        if press.choice == "do":
            out.append(card_types.Record(event="verdict_reviewed", fields={**row_fields, "choice": "agree"}))
        else:
            out.extend(
                [
                    card_types.Consumption(
                        session=sid,
                        kind="contested" if press.kind == "used" else "used",
                        paths=[card_view.note_path(press.note)],
                    ),
                    card_types.Record(event="verdict_reviewed", fields={**row_fields, "choice": "flip"}),
                ]
            )
    return out


def _delegate_effects(
    press: ReviewPress,
    fields: dict,
    sessions: list[str],
    delegated: card_types.Delegated | None,
) -> list[card_types.Effect]:
    """맡긴다: 에이전트(모델) 판정을 받는다 — 낱말 표지를 그대로 도장 찍는 게 아니라
    누를 때 모델이 내린 판정(kind)과 이유 한 줄(reason)이 그대로 실린다. judge 는 언제나
    AGENT_DELEGATED: 오너 판정 계열(owner · None 의 기본값)과 섞이는 변이는 시험이 빨갛게
    끝낸다. 모델이 못 답한 누름은 간선 없이 사건 한 줄 — proposed kind 을 도장 찍어 조용히
    되돌아가는 변이를 허용하지 않는 게 이 분기다."""
    if delegated is None:
        raise ValueError("delegate press needs the delegated judgment")
    if isinstance(delegated, card_types.DelegationFailed):
        return [
            card_types.Record(
                event="verdict_reviewed",
                fields={
                    **fields,
                    "session_id": press.session,
                    "choice": "delegate",
                    "judge": card_types.AGENT_DELEGATED,
                    "error": delegated.reason,
                },
            )
        ]
    path = card_view.note_path(press.note)
    out: list[card_types.Effect] = []
    for sid in sessions:
        row_fields = {**fields, "session_id": sid}
        out.extend(
            [
                card_types.Consumption(
                    session=sid,
                    kind=delegated.kind,
                    paths=[path],
                    judge=card_types.AGENT_DELEGATED,
                ),
                card_types.Record(
                    event="verdict_reviewed",
                    fields={
                        **row_fields,
                        "choice": "delegate",
                        "judge": card_types.AGENT_DELEGATED,
                        "kind": delegated.kind,
                        "reason": delegated.reason,
                        "comment_ids": delegated.comment_ids,
                    },
                ),
            ]
        )
    return out
