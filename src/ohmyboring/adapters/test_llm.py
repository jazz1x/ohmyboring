#!/usr/bin/env python3
"""LLM 어댑터 — JSON 뽑기와 나가는 요청의 모양.

Run: python3 src/ohmyboring/adapters/test_llm.py   (no pytest dependency)
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ohmyboring import config as omb_env  # noqa: E402
from ohmyboring.adapters import embed as embedder  # noqa: E402
from ohmyboring.adapters import llm  # noqa: E402
from ohmyboring.result import Ok  # noqa: E402


class ExtractJsonTests(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(llm.extract_json('{"a": 1}'), {"a": 1})

    def test_markdown_fenced(self):
        text = '```json\n{"title": "x", "body": "y"}\n```'
        self.assertEqual(llm.extract_json(text), {"title": "x", "body": "y"})

    def test_trailing_prose_ignored(self):
        # raw_decode stops at the first complete object; trailing garbage is dropped.
        text = '{"skip": true}\nHere is why I skipped it.'
        self.assertEqual(llm.extract_json(text), {"skip": True})

    def test_leading_prose_before_object(self):
        text = 'Sure! Here is the JSON:\n{"k": "v"}'
        self.assertEqual(llm.extract_json(text), {"k": "v"})

    def test_no_object_returns_none(self):
        self.assertIsNone(llm.extract_json("no json here at all"))

    def test_malformed_returns_none(self):
        self.assertIsNone(llm.extract_json('{"a": '))


class _Reply:
    def __init__(self, content):
        self._body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class CallLlmTests(unittest.TestCase):
    def _call(self, content):
        seen = {}

        def fake_urlopen(req, timeout):
            seen["req"], seen["timeout"] = req, timeout
            return _Reply(content)

        with (
            mock.patch.object(llm, "LLM_BASE_URL", "http://llm.test/v1/"),
            mock.patch.object(llm, "LLM_MODEL", "m"),
            mock.patch.object(llm, "LLM_API_KEY", ""),
            mock.patch.object(llm.urllib.request, "urlopen", fake_urlopen),
        ):
            parsed = llm.call_llm("PROMPT")
        return parsed, seen

    def test_request_keeps_the_wire_shape_the_model_was_tuned_against(self):
        parsed, seen = self._call('{"title": "t"}')
        self.assertEqual(parsed, {"title": "t"})
        self.assertEqual(seen["timeout"], 120)
        self.assertEqual(seen["req"].full_url, "http://llm.test/v1/chat/completions")
        self.assertNotIn("Authorization", seen["req"].headers)
        self.assertEqual(
            json.loads(seen["req"].data),
            {
                "model": "m",
                "messages": [
                    {
                        "role": "system",
                        "content": "You emit only compact, valid JSON. No prose outside JSON.",
                    },
                    {"role": "user", "content": "PROMPT"},
                ],
                "temperature": 0.3,
                "stream": False,
                "response_format": {"type": "json_object"},
                "reasoning_effort": "none",
            },
        )

    def test_unparseable_reply_is_none_and_announced(self):
        err = io.StringIO()
        with redirect_stderr(err):
            parsed, _seen = self._call("not json")
        self.assertIsNone(parsed)
        self.assertIn("failed to parse LLM output", err.getvalue())


class DistillSplitTests(unittest.TestCase):
    def test_distill_settings_move_chat_only_and_embed_keeps_the_local_address(self):
        env = {
            "BORING_LLM_BASE_URL": "http://local.test/v1",
            "BORING_LLM_MODEL": "local-chat",
            "BORING_LLM_API_KEY": "local-key",
            "BORING_DISTILL_BASE_URL": "http://remote.test/v1",
            "BORING_DISTILL_MODEL": "remote-chat",
            "BORING_DISTILL_API_KEY": "remote-key",
        }
        seen = {}

        class _Opened:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"data": [{"embedding": [1.0]}]}'

        def opener(req, timeout):
            seen["embed"] = req
            return _Opened()

        try:
            with mock.patch.dict(os.environ, env):
                importlib.reload(llm)
                embedder.embed("x", opener=opener)
                chat = (llm.LLM_BASE_URL, llm.LLM_MODEL, llm.LLM_API_KEY)
        finally:
            importlib.reload(llm)
        self.assertEqual(chat, ("http://remote.test/v1", "remote-chat", "remote-key"))
        self.assertEqual(seen["embed"].full_url, "http://local.test/v1/embeddings")
        self.assertEqual(seen["embed"].get_header("Authorization"), "Bearer local-key")


class AnthropicProviderTests(unittest.TestCase):
    def test_anthropic_branch_asks_for_json_schema_output_without_openai_knobs(self):
        # The SDK is not a locked dependency (only the runtime image ships it), so the test hands
        # call_llm a stand-in module instead of importing the real one.
        sdk = types.ModuleType("anthropic")
        seen = {}

        class _Block:
            type = "text"
            text = '{"title": "t", "body": "b"}'

        class _Resp:
            stop_reason = "end_turn"
            content = [_Block()]

            class usage:
                input_tokens = 7
                output_tokens = 3

        class _Messages:
            def create(self, **kwargs):
                seen["kwargs"] = kwargs
                return _Resp()

        class _Client:
            def __init__(self, **kwargs):
                seen["client"] = kwargs
                self.messages = _Messages()

        sdk.Anthropic = _Client
        err = io.StringIO()
        with (
            mock.patch.object(llm, "DISTILL_PROVIDER", Ok(omb_env.AnthropicProvider())),
            mock.patch.object(llm, "LLM_MODEL", "claude-haiku-5-5"),
            mock.patch.object(llm, "LLM_API_KEY", "k"),
            mock.patch.dict(sys.modules, {"anthropic": sdk}),
            redirect_stderr(err),
        ):
            parsed = llm.call_llm("PROMPT")
        kwargs = seen["kwargs"]
        self.assertEqual(parsed, {"title": "t", "body": "b"})
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(kwargs["output_config"]["effort"], "low")
        for forbidden in ("temperature", "response_format", "reasoning_effort", "thinking"):
            self.assertNotIn(forbidden, kwargs)
        self.assertIn("[distill-session] usage input_tokens=7 output_tokens=3", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
