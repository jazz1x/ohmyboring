#!/usr/bin/env python3
"""The secretary's face: Slack in, Slack out, nothing else.

Two events only. An `@mention` is a question — the brain answers it in the thread and ledgers
the notes the answer carried, so a later verdict can find them. A 👍/👎 on the bot's own answer
is that verdict, handed to the engine as `used`/`contested`. There is no state here: S2's ledger
already owns the mapping between a posted answer and its notes, so the transport remembers
nothing and can be replaced without losing anything.

Why this file is thin: hermes was the face before, and the face is what broke — a socket
reconnect loop that took the whole container down with it. Everything that can raise is caught
at the dispatch boundary, because one bad envelope must not kill the socket loop. `slack_sdk`
is imported only in `main()`/`dispatch()` (lazily), so the handlers are tested without the
library and without a workspace.
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Optional

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
) -> Optional[str]:
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
    owner_id: Optional[str] = None,
) -> Optional[dict]:
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


def dispatch(client, req, *, bot_user_id: str, owner_id: Optional[str] = None) -> None:
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
    except Exception as e:  # noqa: BLE001 — the brain's stderr lines are the diagnosis; the loop lives
        print(f"[secretary] event failed: {e}", file=sys.stderr)


def main() -> int:
    app_token = os.environ.get("SLACK_APP_TOKEN")
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not app_token or not bot_token:
        print("[secretary] SLACK_APP_TOKEN and SLACK_BOT_TOKEN must be set (see .env.example)", file=sys.stderr)
        return 2
    owner_id = os.environ.get("SECRETARY_OWNER_ID") or None

    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.web import WebClient
    from threading import Event

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
