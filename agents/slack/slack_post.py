"""The one chat.postMessage both cards post through.

The morning card (`card.py` `_env_send`) and the weekly card (`weekly_card.py`) send through
this helper rather than each owning a WebClient call: one poster, one payload shape, so a
formatting drift like the hermes cron wrapper (our mrkdwn re-read as markdown, `*bold*`
arriving as italics) has one place to live and one place to be fixed.
"""

from __future__ import annotations

import os
from typing import Any


def post_payload(payload: dict[str, Any]) -> str:
    """Post one chat.postMessage payload to SLACK_CARD_CHANNEL; return the message ts.

    Slack answers HTTP 200 even when the post fails, so this raises on `ok: false`
    (slack_sdk.errors.SlackApiError) — a caller that wants to say why in one line catches it.
    Callers validate SLACK_BOT_TOKEN/SLACK_CARD_CHANNEL before posting; a post to a guessed
    channel is worse than no post.
    """
    from slack_sdk.web import WebClient

    channel = os.environ["SLACK_CARD_CHANNEL"]
    resp = WebClient(token=os.environ.get("SLACK_BOT_TOKEN")).chat_postMessage(channel=channel, **payload)
    return resp["ts"]
