#!/usr/bin/env python3
"""The secretary's face, tested without Slack and without slack_sdk.

The handlers take the brain and the web client as arguments, so a fake `web` plus fake
`ask`/`remember`/`judge` cover everything except the socket itself — which stays unopened
here on purpose. `dispatch` gets a fake `slack_sdk` in `sys.modules` for its ack, and fake
client/request objects for the rest.

Run: BORING_INJECTION_LEDGER=$(mktemp) python3 agents/slack/test_secretary.py
"""
import os
import sys
import tempfile
import types

os.environ.setdefault(
    "BORING_INJECTION_LEDGER",
    os.path.join(tempfile.mkdtemp(), "injections.jsonl"),
)

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import secretary_core as sc  # noqa: E402
import secretary as sec  # noqa: E402

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
    """Records chat_postMessage and answers like Slack: a posted message has a ts."""

    def __init__(self, order=None):
        self.posts = []
        self._order = order

    def chat_postMessage(self, **kw):
        if self._order is not None:
            self._order.append("post")
        self.posts.append(kw)
        return {"ts": POSTED_TS}


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

    assert asked == ["<@U_BOT> 브랜치 이름 어떻게 정했더라"], "the brain receives the raw mention and strips it itself"
    assert web.posts == [{
        "channel": CH,
        "thread_ts": TS,  # no thread yet: the mention's own ts opens it
        "text": "답 본문",
    }]
    assert posted == POSTED_TS, "the posted ts is returned so the caller knows where the answer lives"
    assert remembered == [{
        "key": f"slack:{CH}:{POSTED_TS}",  # ledgered under the POSTED message, not the question
        "question": "브랜치 이름 어떻게 정했더라",
        "hits": given,
    }], "the ledger gets exactly the notes the reader saw, under the answer's own key"


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
    """The integration the transport exists for: mention → posted answer → 👍 → engine edges
    on exactly the notes the answer carried. The ledger is the only bridge."""
    given = [{"source_path": "/vault/wiki/wiki-0435.md", "snippet": "branch naming settled " * 3}]
    web = FakeWeb()
    sec.on_mention(_mention(), web, ask=lambda q: sc.Answer("답 본문", given))

    seen = []

    def consumption(session_id, observed_at, used, contested, supersedes=None):
        seen.append({"session_id": session_id, "used": list(used), "contested": list(contested)})
        return {"used": len(used), "contested": len(contested), "unknown": []}

    out = sec.on_reaction(
        _reaction("thumbsup"),
        BOT,
        judge=lambda key, verdict: sc.feedback(key, verdict, consumption=consumption),
    )
    assert out.get("unknown_answer") is not True, "the ledger connects the posted answer to the reaction"
    assert seen == [{
        "session_id": f"slack:{CH}:{POSTED_TS}",
        "used": ["/vault/wiki/wiki-0435.md"],
        "contested": [],
    }]


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
        sec.dispatch(client, FakeReq("env-2", payload={"event": _reaction("thumbsup")}), bot_user_id=BOT, owner_id=OWNER)
        sec.dispatch(client, FakeReq("env-3", type="slash_commands"), bot_user_id=BOT, owner_id=OWNER)
        sec.on_mention = boom
        sec.dispatch(client, FakeReq("env-4", payload={"event": _mention()}), bot_user_id=BOT, owner_id=OWNER)
    finally:
        sec.on_mention, sec.on_reaction = real_mention, real_reaction

    assert client.acks == ["env-1", "env-2", "env-3", "env-4"], "every envelope is acked, slash commands included"
    assert order.index("post") > order.index("ack"), "the ack lands before the answer — Slack resends the un-acked"
    assert handled == ["mention", "reaction"], "env-3 is not an event, env-4 raised and was swallowed, not propagated"


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - secretary: mentions answered in threads, reactions judged, no socket opened")
