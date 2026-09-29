#!/usr/bin/env python3
"""슬랙 게시 어댑터 — 채널은 환경에서, 토큰은 클라이언트로, ts 는 값으로 돌려준다.

Run: python3 src/ohmyboring/adapters/test_slack.py
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ohmyboring.adapters import slack  # noqa: E402


class FakeClient:
    def __init__(self, token=None):
        self.token = token
        self.sent = []

    def chat_postMessage(self, **kwargs):
        self.sent.append(kwargs)
        return {"ts": "1234.5678"}


class PostPayloadTest(unittest.TestCase):
    def test_posts_the_payload_to_the_card_channel_and_returns_the_ts(self):
        made = []

        def factory(token=None):
            made.append(FakeClient(token))
            return made[-1]

        env = {"SLACK_CARD_CHANNEL": "D01", "SLACK_BOT_TOKEN": "tok"}
        with mock.patch.dict(os.environ, env), mock.patch("slack_sdk.web.WebClient", factory):
            ts = slack.post_payload({"text": "t", "blocks": []})
        self.assertEqual(ts, "1234.5678")
        self.assertEqual(
            (made[0].token, made[0].sent), ("tok", [{"channel": "D01", "text": "t", "blocks": []}])
        )

    def test_a_missing_channel_is_a_key_error_not_a_guess(self):
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "tok"}, clear=True), self.assertRaises(KeyError):
            slack.post_payload({"text": "t"})


if __name__ == "__main__":
    unittest.main()
