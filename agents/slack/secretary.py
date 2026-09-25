#!/usr/bin/env python3
"""The secretary's face: Slack in, Slack out, nothing else.

Three events. An `@mention` is a question — the brain answers it in the thread and ledgers
the notes the answer carried, so a later verdict can find them. A 👍/👎 on the bot's own answer
is that verdict, handed to the engine as `used`/`contested`. A thread reply on the bot's own
answer that starts with "정정: …" is a correction: it becomes a new note that replaces the
answer's notes — the loop's last step, where "wrong, and the right thing is X" finally reaches
the engine. There is no state here: the engine owns the mapping between a posted answer and
its notes, so the transport remembers nothing and can be replaced without losing anything.

Why this file is thin: hermes was the face before, and the face is what broke — a socket
reconnect loop that took the whole container down with it. Everything that can raise is caught
at the dispatch boundary, because one bad envelope must not kill the socket loop. `slack_sdk`
is imported only in `main()`/`dispatch()` (lazily), so the handlers are tested without the
library and without a workspace.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import secretary_core  # noqa: E402

#: 👍/👎 and their alias spellings, to the verdict the engine understands. Anything outside this
#: map is decoration, not a verdict — `eyes` on an answer means someone looked, not that it was used.
VERDICT_BY_REACTION = {"+1": "used", "thumbsup": "used", "-1": "contested", "thumbsdown": "contested"}


def answer_key(channel: str, ts: str) -> str:
    """The transport's opaque name for one posted answer: where it was posted, and where it sits."""
    return f"slack:{channel}:{ts}"


def on_mention(
    event: dict,
    web,
    *,
    ask: Callable[..., secretary_core.Answer] = secretary_core.answer,
    remember: Callable[..., bool] = secretary_core.remember_handed,
) -> str | None:
    """Answer a mention in its thread, then ledger the answer under the posted message's ts so a
    later reaction can find the notes it carried. Returns the posted ts (the tests read it);
    `remember` is called even when the answer carried nothing — S2 owns the empty-hands policy."""
    answer = ask(event["text"])
    posted = web.chat_postMessage(
        channel=event["channel"],
        thread_ts=event.get("thread_ts") or event["ts"],
        text=answer.text,
    )
    ts = posted["ts"]
    remember(
        answer_key(event["channel"], ts),
        secretary_core.strip_mention(event["text"]),
        answer.hits,
    )
    return ts


def on_reaction(
    event: dict,
    bot_user_id: str,
    *,
    judge: Callable[..., dict] = secretary_core.feedback,
    owner_id: str | None = None,
) -> dict | None:
    """Turn a 👍/👎 on the bot's own answer into the engine's verdict. A reaction to anyone
    else's message, an emoji the map does not know, or — when an owner is configured — a
    non-owner's tap are all None: the verdict must come from the person the answer was for."""
    if event.get("item_user") != bot_user_id:
        return None
    verdict = VERDICT_BY_REACTION.get(event.get("reaction"))
    if verdict is None:
        return None
    if owner_id is not None and event.get("user") != owner_id:
        return None
    item = event["item"]
    return judge(answer_key(item["channel"], item["ts"]), verdict)


def _handed_paths_from(answer_text: str) -> list[str]:
    """The note paths one answer carried, rebuilt from the posted body: the `*wiki-NNNN.md*`
    names in order, under the `/vault/wiki/` prefix the uptake hook writes. The inverse of
    `recall_core.source_name` — the body holds names, the engine wants paths."""
    return [f"/vault/wiki/{name}" for name in re.findall(r"\*([^*\n]+\.md)\*", answer_text or "")]


def _correction_line(result: dict) -> str:
    """The one line the thread gets back: what was recorded and how much it replaced, or the error."""
    if result.get("error") == "number required":
        return f"노트가 {result['count']}개입니다 — 「정정 2: …」처럼 번호를 붙여 주세요"
    if result.get("error"):
        return f"정정 기록 실패 — {result['error']}"
    if result.get("duplicate"):
        # Since r4.1 the engine never gates a correction, so a duplicate answer means the
        # write was swallowed — the old note still stands and the thread must hear failure.
        name = result["duplicate"].rsplit("/", 1)[-1]
        return f"정정 기록 안 됨 — 기존 노트 {name} 와 같다고 봄"
    name = (result.get("source_path") or "?").rsplit("/", 1)[-1]
    return f"정정 기록 → {name} (대체 {result['supersedes']})"


def on_thread_reply(
    event: dict,
    web,
    bot_user_id: str,
    *,
    correct: Callable[..., dict] = secretary_core.correct,
    owner_id: str | None = None,
) -> dict | None:
    """A thread reply on the bot's own answer, starting with "정정:", becomes a new note that
    replaces one of the answer's notes — the only one, or the one "정정 2:" names. Slack
    sends thread replies as `message` events, and the transport keeps no state about which ts
    was its own answer: the parent is read back through the API (one call) and recognized by
    the answer header the brain puts on every answer. Everything else is None — threaded
    chatter that is not a correction, a correction on someone else's message, a correction
    from someone who is not the owner — only the owner's correction on the bot's own answer
    becomes a note. Past the owner check a configured owner_id means the reply is the owner's,
    so the note is signed `owner`; with no owner_id nobody is confirmed and the note goes unsigned."""
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return None
    if owner_id is not None and event.get("user") != owner_id:
        return None
    author = None if owner_id is None else secretary_core.OWNER
    if secretary_core.parse_correction(event.get("text") or "") is None:
        return None
    parent = web.conversations_replies(channel=event["channel"], ts=thread_ts, limit=1)["messages"][0]
    if parent.get("user") != bot_user_id:
        return None
    if not (parent.get("text") or "").startswith(secretary_core.ANSWER_HEAD_PREFIX):
        return None
    result = correct(
        answer_key(event["channel"], thread_ts),
        "",
        _handed_paths_from(parent["text"]),
        event["text"],
        author=author,
    )
    web.chat_postMessage(channel=event["channel"], thread_ts=thread_ts, text=_correction_line(result))
    return result


def dispatch(client, req, *, bot_user_id: str, owner_id: str | None = None) -> None:
    """Ack every envelope first (Slack resends what is not acked), then act on `events_api`
    only. Never raises — one malformed event must not cost the socket loop its connection."""
    try:
        from slack_sdk.socket_mode.response import SocketModeResponse

        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
    except Exception as e:  # noqa: BLE001 — an unacked envelope is retried; a dead loop is not
        print(f"[secretary] ack failed: {e}", file=sys.stderr)
        return
    if getattr(req, "type", None) != "events_api":
        return
    event = (getattr(req, "payload", None) or {}).get("event") or {}
    try:
        if event.get("type") == "app_mention":
            on_mention(event, client.web_client)
        elif event.get("type") == "reaction_added":
            on_reaction(event, bot_user_id, owner_id=owner_id)
        elif event.get("type") == "message":
            if event.get("subtype") or event.get("user") == bot_user_id:
                return  # bot echoes and message edits are not conversation
            on_thread_reply(event, client.web_client, bot_user_id, owner_id=owner_id)
    except Exception as e:  # noqa: BLE001 — the brain's stderr lines are the diagnosis; the loop lives
        print(f"[secretary] event failed: {e}", file=sys.stderr)


def main() -> int:
    app_token = os.environ.get("SLACK_APP_TOKEN")
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not app_token or not bot_token:
        print(
            "[secretary] SLACK_APP_TOKEN and SLACK_BOT_TOKEN must be set (see .env.example)", file=sys.stderr
        )
        return 2
    owner_id = os.environ.get("SECRETARY_OWNER_ID") or None

    from threading import Event

    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.web import WebClient

    web_client = WebClient(token=bot_token)
    try:
        bot_user_id = web_client.auth_test()["user_id"]
    except Exception as e:  # noqa: BLE001 — say why in one line, not with a stack trace and no token
        print(f"[secretary] auth_test failed: {e}", file=sys.stderr)
        return 1

    client = SocketModeClient(app_token=app_token, web_client=web_client)
    client.socket_mode_request_listeners.append(
        lambda client, req: dispatch(client, req, bot_user_id=bot_user_id, owner_id=owner_id)
    )
    client.connect()
    Event().wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
