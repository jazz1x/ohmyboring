#!/usr/bin/env python3
"""The secretary's face, tested without Slack and without slack_sdk.

The handlers take the brain and the web client as arguments, so a fake `web` plus fake
`ask`/`remember`/`judge` cover everything except the socket itself — which stays unopened
here on purpose. `dispatch` gets a fake `slack_sdk` in `sys.modules` for its ack, and fake
client/request objects for the rest.

Run: python3 agents/slack/test_secretary.py
"""

import io
import json
import os
import sys
import types
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import secretary as sec  # noqa: E402
import secretary_core as sc  # noqa: E402

BOT = "U_BOT"
OWNER = "U_OWNER"
CH = "C123"
TS = "1726900000.000100"
POSTED_TS = "1.000"


def _mention(text="<@U_BOT> 브랜치 이름 어떻게 정했더라", thread_ts=None):
    event = {"type": "app_mention", "user": OWNER, "text": text, "channel": CH, "ts": TS}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


def _reaction(reaction, user=OWNER, item_user=BOT, ts=POSTED_TS):
    return {
        "type": "reaction_added",
        "user": user,
        "reaction": reaction,
        "item": {"type": "message", "channel": CH, "ts": ts},
        "item_user": item_user,
    }


class FakeWeb:
    """Records chat_postMessage and conversations_replies, and answers like Slack: a posted
    message has a ts, a replies call returns the parent message it was given."""

    def __init__(self, order=None, parent=None):
        self.posts = []
        self.replies_calls = []
        self._order = order
        self._parent = parent

    def chat_postMessage(self, **kw):
        if self._order is not None:
            self._order.append("post")
        self.posts.append(kw)
        return {"ts": POSTED_TS}

    def conversations_replies(self, **kw):
        self.replies_calls.append(kw)
        return {"messages": [self._parent] if self._parent is not None else []}


def _recorder():
    calls = []

    def fake(answer_key, verdict):
        calls.append({"key": answer_key, "verdict": verdict})
        return {"recorded": verdict}

    return calls, fake


def _install_fake_slack_sdk():
    """dispatch() acks through slack_sdk; give it a stand-in so no real package is imported."""
    sdk = types.ModuleType("slack_sdk")
    socket_mode = types.ModuleType("slack_sdk.socket_mode")
    response = types.ModuleType("slack_sdk.socket_mode.response")

    class SocketModeResponse:
        def __init__(self, envelope_id):
            self.envelope_id = envelope_id

    response.SocketModeResponse = SocketModeResponse
    socket_mode.response = response
    sdk.socket_mode = socket_mode
    sys.modules.setdefault("slack_sdk", sdk)
    sys.modules.setdefault("slack_sdk.socket_mode", socket_mode)
    sys.modules.setdefault("slack_sdk.socket_mode.response", response)


class FakeReq:
    def __init__(self, envelope_id, type="events_api", payload=None):
        self.envelope_id = envelope_id
        self.type = type
        self.payload = payload or {}


class FakeClient:
    def __init__(self, order=None, web=None):
        self.web_client = web if web is not None else FakeWeb()
        self.order = order if order is not None else []
        self.acks = []

    def send_socket_mode_response(self, response):
        self.order.append("ack")
        self.acks.append(response.envelope_id)


def test_a_mention_is_answered_in_its_thread_and_then_ledgered():
    given = [{"source_path": "/vault/wiki/wiki-0435.md", "snippet": "branch naming settled " * 3}]
    asked, remembered = [], []

    def ask(q):
        asked.append(q)
        return sc.Answer("답 본문", given)

    def remember(key, question, hits):
        remembered.append({"key": key, "question": question, "hits": hits})
        return True

    web = FakeWeb()
    posted = sec.on_mention(_mention(), web, ask=ask, remember=remember)

    assert asked == ["<@U_BOT> 브랜치 이름 어떻게 정했더라"], (
        "the brain receives the raw mention and strips it itself"
    )
    assert web.posts == [
        {
            "channel": CH,
            "thread_ts": TS,  # no thread yet: the mention's own ts opens it
            "text": "답 본문",
        }
    ]
    assert posted == POSTED_TS, "the posted ts is returned so the caller knows where the answer lives"
    assert remembered == [
        {
            "key": f"slack:{CH}:{POSTED_TS}",  # ledgered under the POSTED message, not the question
            "question": "브랜치 이름 어떻게 정했더라",
            "hits": given,
        }
    ], "the ledger gets exactly the notes the reader saw, under the answer's own key"


def test_a_threaded_mention_answers_inside_that_thread():
    web = FakeWeb()
    sec.on_mention(
        _mention(thread_ts="1726900000.000077"),
        web,
        ask=lambda q: sc.Answer("답", []),
        remember=lambda *a: True,
    )
    assert web.posts[0]["thread_ts"] == "1726900000.000077"


def test_an_answer_with_empty_hands_is_still_ledgered():
    remembered = []
    sec.on_mention(
        _mention(),
        FakeWeb(),
        ask=lambda q: sc.Answer(sc.NOTHING_FOUND, []),
        remember=lambda key, q, hits: remembered.append(hits) or False,
    )
    assert remembered == [[]], "S2 receives the empty list and decides nothing was handed"


def test_a_thumbsup_on_the_bots_own_answer_is_used():
    for emoji in ("+1", "thumbsup"):
        calls, judge = _recorder()
        out = sec.on_reaction(_reaction(emoji), BOT, judge=judge)
        assert calls == [{"key": f"slack:{CH}:{POSTED_TS}", "verdict": "used"}], emoji
        assert out == {"recorded": "used"}


def test_a_thumbsdown_is_contested():
    for emoji in ("-1", "thumbsdown"):
        calls, judge = _recorder()
        sec.on_reaction(_reaction(emoji), BOT, judge=judge)
        assert calls == [{"key": f"slack:{CH}:{POSTED_TS}", "verdict": "contested"}], emoji


def test_a_reaction_to_someone_elses_message_is_no_verdict():
    calls, judge = _recorder()
    out = sec.on_reaction(_reaction("thumbsup", item_user="U_SOMEONE_ELSE"), BOT, judge=judge)
    assert out is None and calls == [], "a tap on another person's message judges nothing here"


def test_a_reaction_the_map_does_not_know_is_no_verdict():
    calls, judge = _recorder()
    assert sec.on_reaction(_reaction("eyes"), BOT, judge=judge) is None
    assert sec.on_reaction(_reaction("heart"), BOT, judge=judge) is None
    assert calls == [], "looking at an answer is not a verdict on it"


def test_without_an_owner_configured_any_tap_counts():
    calls, judge = _recorder()
    sec.on_reaction(_reaction("thumbsup", user="U_ANYONE"), BOT, judge=judge)
    assert calls == [{"key": f"slack:{CH}:{POSTED_TS}", "verdict": "used"}]


def test_with_an_owner_configured_only_the_owner_taps_count():
    calls, judge = _recorder()
    out = sec.on_reaction(_reaction("thumbsup", user="U_STRANGER"), BOT, judge=judge, owner_id=OWNER)
    assert out is None and calls == [], "the verdict belongs to the person the answer was for"
    sec.on_reaction(_reaction("thumbsup", user=OWNER), BOT, judge=judge, owner_id=OWNER)
    assert calls == [{"key": f"slack:{CH}:{POSTED_TS}", "verdict": "used"}]


def test_a_reaction_after_a_real_answer_reaches_the_engine_verdict():
    """The integration the transport exists for: mention → posted answer → 👍 → one verdict
    on exactly the notes the answer carried. The engine's handover record is the only bridge."""
    given = [{"source_path": "/vault/wiki/wiki-0435.md", "snippet": "branch naming settled " * 3}]
    web = FakeWeb()
    engine = []

    def handover(session_id, observed_at, paths):
        engine.append({"kind": "handover", "session_id": session_id, "paths": list(paths)})
        return {"session": session_id, "handed": len(paths), "unknown": []}

    def consumption(session_id, observed_at, verdict=None):
        engine.append({"kind": "consumption", "session_id": session_id, "verdict": verdict})
        return {"used": 1, "contested": 0}

    sec.on_mention(
        _mention(),
        web,
        ask=lambda q: sc.Answer("답 본문", given),
        remember=lambda key, q, hits: sc.remember_handed(key, q, hits, handover=handover),
    )

    out = sec.on_reaction(
        _reaction("thumbsup"),
        BOT,
        judge=lambda key, verdict: sc.feedback(key, verdict, consumption=consumption),
    )
    assert out.get("unknown_answer") is not True, "the handover connects the posted answer to the reaction"
    assert engine == [
        {"kind": "handover", "session_id": f"slack:{CH}:{POSTED_TS}", "paths": ["/vault/wiki/wiki-0435.md"]},
        {"kind": "consumption", "session_id": f"slack:{CH}:{POSTED_TS}", "verdict": "used"},
    ]


def test_dispatch_acks_first_then_dispatches_and_swallows_handler_errors():
    _install_fake_slack_sdk()
    order = []
    client = FakeClient(order=order, web=FakeWeb(order=order))
    handled = []
    real_mention, real_reaction = sec.on_mention, sec.on_reaction
    sec.on_mention = lambda event, web: handled.append("mention") or order.append("post") or POSTED_TS
    sec.on_reaction = lambda event, bot, owner_id=None: handled.append("reaction")

    def boom(event, web):
        raise RuntimeError("bad envelope")

    try:
        sec.dispatch(client, FakeReq("env-1", payload={"event": _mention()}), bot_user_id=BOT, owner_id=OWNER)
        sec.dispatch(
            client,
            FakeReq("env-2", payload={"event": _reaction("thumbsup")}),
            bot_user_id=BOT,
            owner_id=OWNER,
        )
        sec.dispatch(client, FakeReq("env-3", type="slash_commands"), bot_user_id=BOT, owner_id=OWNER)
        sec.on_mention = boom
        sec.dispatch(client, FakeReq("env-4", payload={"event": _mention()}), bot_user_id=BOT, owner_id=OWNER)
    finally:
        sec.on_mention, sec.on_reaction = real_mention, real_reaction

    assert client.acks == ["env-1", "env-2", "env-3", "env-4"], (
        "every envelope is acked, slash commands included"
    )
    assert order.index("post") > order.index("ack"), (
        "the ack lands before the answer — Slack resends the un-acked"
    )
    assert handled == ["mention", "reaction"], (
        "env-3 is not an event, env-4 raised and was swallowed, not propagated"
    )


def test_dispatch_a_failed_ack_means_no_handling():
    _install_fake_slack_sdk()
    client = FakeClient()
    client.send_socket_mode_response = lambda response: (_ for _ in ()).throw(RuntimeError("socket closed"))
    handled = []
    real = sec.on_mention
    sec.on_mention = lambda event, web: handled.append(event)
    try:
        sec.dispatch(client, FakeReq("env-9", payload={"event": _mention()}), bot_user_id=BOT)
    finally:
        sec.on_mention = real
    assert handled == [], "an un-acked envelope comes back as a retry; answering now would double-post"


def _reply(text="정정: 재시작은 2시에 한다", user=OWNER, thread_ts=POSTED_TS):
    event = {"type": "message", "user": user, "text": text, "channel": CH, "ts": "1726900000.000200"}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


def _answer_parent():
    text = sc.render(
        [
            {"source_path": "/vault/wiki/wiki-0435.md", "snippet": "branch naming settled " * 3},
            {"source_path": "/vault/wiki/wiki-1000.md", "snippet": "pool question settled " * 3},
        ]
    )
    return {"user": BOT, "text": text}


def _recording_correct(calls):
    def correct(key, question, handed_paths, text, author=None):
        calls.append({"key": key, "question": question, "handed_paths": handed_paths, "text": text})
        return {
            "source_path": "/vault/wiki/wiki-1077.md",
            "wiki_id": "wiki-1077",
            "duplicate": None,
            "supersedes": 1,
            "unknown": 0,
        }

    return correct


def test_a_correction_without_a_number_on_many_notes_asks_for_one():
    remembered = []
    web = FakeWeb(parent=_answer_parent())
    out = sec.on_thread_reply(
        _reply(),
        web,
        BOT,
        correct=lambda *a, **k: sc.correct(*a, remember=lambda *x, **kw: remembered.append(kw), **k),
        owner_id=OWNER,
    )
    assert remembered == [], "two notes and no number: nothing may be superseded"
    assert out == {"error": "number required", "count": 2}
    assert web.posts == [
        {
            "channel": CH,
            "thread_ts": POSTED_TS,
            "text": "노트가 2개입니다 — 「정정 2: …」처럼 번호를 붙여 주세요",
        }
    ]


def test_a_correction_on_the_bots_answer_becomes_a_note():
    calls = []
    web = FakeWeb(parent=_answer_parent())
    out = sec.on_thread_reply(
        _reply(text="정정 2: 재시작은 2시에 한다"),
        web,
        BOT,
        correct=_recording_correct(calls),
        owner_id=OWNER,
    )

    assert web.replies_calls == [{"channel": CH, "ts": POSTED_TS, "limit": 1}]
    assert calls == [
        {
            "key": f"slack:{CH}:{POSTED_TS}",
            "question": "",
            "handed_paths": ["/vault/wiki/wiki-0435.md", "/vault/wiki/wiki-1000.md"],
            "text": "정정 2: 재시작은 2시에 한다",
        }
    ], "the paths are rebuilt from the parent body's names, in the order the answer listed them"
    assert web.posts == [
        {
            "channel": CH,
            "thread_ts": POSTED_TS,
            "text": "정정 기록 → wiki-1077.md (대체 1)",
        }
    ]
    assert out["wiki_id"] == "wiki-1077"


def test_a_correction_the_engine_duplicated_is_a_failure_not_a_recorded_line():
    calls = []

    def duplicating_correct(key, question, handed_paths, text, author=None):
        calls.append(text)
        return {
            "source_path": "/vault/wiki/wiki-0435.md",
            "wiki_id": "wiki-0435",
            "duplicate": "/vault/wiki/wiki-0435.md",
            "supersedes": 0,
            "unknown": 0,
        }

    web = FakeWeb(parent=_answer_parent())
    sec.on_thread_reply(_reply(), web, BOT, correct=duplicating_correct, owner_id=OWNER)
    assert web.posts == [
        {
            "channel": CH,
            "thread_ts": POSTED_TS,
            "text": "정정 기록 안 됨 — 기존 노트 wiki-0435.md 와 같다고 봄",
        }
    ], "a swallowed correction must read as failure — the old note still stands"


def test_a_message_without_a_thread_is_just_conversation():
    calls = []
    web = FakeWeb(parent=_answer_parent())
    out = sec.on_thread_reply(
        _reply(thread_ts=None), web, BOT, correct=_recording_correct(calls), owner_id=OWNER
    )
    assert out is None and calls == [] and web.replies_calls == [] and web.posts == []


def test_a_correction_from_someone_else_is_ignored_when_an_owner_is_configured():
    calls = []
    web = FakeWeb(parent=_answer_parent())
    out = sec.on_thread_reply(
        _reply(user="U_STRANGER"), web, BOT, correct=_recording_correct(calls), owner_id=OWNER
    )
    assert out is None and calls == [] and web.replies_calls == []


def test_thread_chatter_that_is_not_a_correction_is_left_alone():
    calls = []
    web = FakeWeb(parent=_answer_parent())
    out = sec.on_thread_reply(
        _reply(text="고마워, 도움 됐어!"), web, BOT, correct=_recording_correct(calls), owner_id=OWNER
    )
    assert out is None and calls == [] and web.replies_calls == []


def test_a_correction_on_someone_elses_thread_is_not_ours_to_keep():
    parent = dict(_answer_parent())
    parent["user"] = "U_SOMEONE_ELSE"
    calls = []
    web = FakeWeb(parent=parent)
    out = sec.on_thread_reply(_reply(), web, BOT, correct=_recording_correct(calls), owner_id=OWNER)
    assert out is None and calls == [] and web.posts == [], (
        "남의 메시지에 정정이 달려도 우리 노트가 되지 않는다"
    )


def test_a_thread_on_a_message_that_is_not_our_answer_is_ignored():
    calls = []
    web = FakeWeb(parent={"user": BOT, "text": "그냥 봇이 한 말"})
    out = sec.on_thread_reply(_reply(), web, BOT, correct=_recording_correct(calls), owner_id=OWNER)
    assert out is None and calls == [] and web.posts == []


def test_dispatch_routes_thread_replies_and_skips_bot_echoes():
    _install_fake_slack_sdk()
    handled = []
    real = sec.on_thread_reply
    sec.on_thread_reply = lambda event, web, bot, owner_id=None: handled.append(event) or {}
    try:
        client = FakeClient(web=FakeWeb(parent=_answer_parent()))
        sec.dispatch(client, FakeReq("env-m1", payload={"event": _reply()}), bot_user_id=BOT, owner_id=OWNER)
        echo = _reply()
        echo["subtype"] = "bot_message"
        sec.dispatch(client, FakeReq("env-m2", payload={"event": echo}), bot_user_id=BOT, owner_id=OWNER)
        sec.dispatch(
            client, FakeReq("env-m3", payload={"event": _reply(user=BOT)}), bot_user_id=BOT, owner_id=OWNER
        )
    finally:
        sec.on_thread_reply = real
    assert len(handled) == 1 and handled[0].get("subtype") is None, (
        "only a plain human reply reaches the handler; echoes and the bot's own messages do not"
    )


def _remember_on_the_wire(owner_id):
    """One correction through the real client, the engine replaced at urlopen: what /remember
    would receive — its body and its headers."""
    sent = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout=None):
        sent.append({"body": json.loads(req.data), "headers": dict(req.header_items())})
        return _Resp(
            json.dumps(
                {
                    "source_path": "/vault/wiki/wiki-1077.md",
                    "wiki_id": "wiki-1077",
                    "duplicate": None,
                    "supersedes": 1,
                    "unknown": 0,
                }
            ).encode()
        )

    web = FakeWeb(parent=_answer_parent())
    with (
        mock.patch.dict(os.environ, {"BORING_OWNER_TOKEN": "tok-owner"}),
        mock.patch("urllib.request.urlopen", urlopen),
    ):
        sec.on_thread_reply(_reply(text="정정 2: 재시작은 2시에 한다"), web, BOT, owner_id=owner_id)
    return sent[0]


def test_an_owner_confirmed_correction_is_signed_owner_and_carries_the_token():
    wire = _remember_on_the_wire(OWNER)
    assert wire["body"]["author"] == "owner" and wire["body"]["judge"] == "owner"
    assert wire["headers"].get("X-boring-owner-token") == "tok-owner"

    unconfirmed = _remember_on_the_wire(None)
    assert "author" not in unconfirmed["body"] and "judge" not in unconfirmed["body"], (
        "no owner_id configured: nobody is confirmed, the engine records unknown"
    )
    assert "X-boring-owner-token" not in unconfirmed["headers"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - secretary: mentions answered in threads, reactions judged, no socket opened")
