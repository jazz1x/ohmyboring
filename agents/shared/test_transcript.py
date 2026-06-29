#!/usr/bin/env python3
"""Regression tests for the shared transcript parser."""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import transcript


def _write(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def test_extract_claude_jsonl_text_and_list_content():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        _write(
            f.name,
            [
                {"message": {"role": "user", "content": "hello"}},
                {"message": {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]}},
                {"message": {"role": "system", "content": "ignored"}},
            ],
        )
        path = f.name
    try:
        out = transcript.extract(path, "claude-json")
        assert "[user] hello" in out
        assert "[assistant] hi there" in out
        assert "[system]" not in out
    finally:
        os.unlink(path)


def test_extract_claude_jsonl_ignores_malformed_lines():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("not json\n")
        f.write(json.dumps({"message": {"role": "user", "content": "ok"}}) + "\n")
        path = f.name
    try:
        out = transcript.extract(path, "claude-json")
        assert out == "[user] ok"
    finally:
        os.unlink(path)


def test_extract_kimi_wire_user_and_assistant():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        _write(
            f.name,
            [
                {"type": "metadata", "protocol_version": "1.4"},
                {"type": "turn.prompt", "input": [{"type": "text", "text": "fix the build"}]},
                {
                    "type": "context.append_message",
                    "message": {
                        "role": "user",
                        "origin": {"kind": "user"},
                        "content": [{"type": "text", "text": "fix the build"}],
                    },
                },
                {
                    "type": "context.append_loop_event",
                    "event": {"type": "content.part", "part": {"type": "text", "text": "done"}},
                },
                {
                    "type": "context.append_message",
                    "message": {
                        "role": "user",
                        "origin": {"kind": "injection"},
                        "content": [{"type": "text", "text": "system reminder"}],
                    },
                },
            ],
        )
        path = f.name
    try:
        out = transcript.extract(path, "kimi-wire")
        assert "[user] fix the build" in out
        assert "[assistant] done" in out
        assert "system reminder" not in out
    finally:
        os.unlink(path)


def test_extract_codex_jsonl_user_and_assistant():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        _write(
            f.name,
            [
                {
                    "type": "session_meta",
                    "payload": {"cwd": "/tmp/project", "id": "session-123"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "# AGENTS.md instructions\nbe brief"},
                            {"type": "input_text", "text": "what is the migration plan?"},
                        ],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "use the new schema."},
                            {"type": "reasoning", "text": "internal thought"},
                        ],
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "last_agent_message": "done"},
                },
            ],
        )
        path = f.name
    try:
        out = transcript.extract(path, "codex-jsonl")
        assert "what is the migration plan?" in out
        assert "use the new schema" in out
        assert "done" in out
        assert "AGENTS.md" not in out
        assert "internal thought" not in out
    finally:
        os.unlink(path)


def test_extract_unknown_format_raises():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("x")
        path = f.name
    try:
        try:
            transcript.extract(path, "unknown-format")
        except ValueError as e:
            assert "unsupported" in str(e).lower()
        else:
            raise AssertionError("expected ValueError for unknown format")
    finally:
        os.unlink(path)


if __name__ == "__main__":
    test_extract_claude_jsonl_text_and_list_content()
    test_extract_claude_jsonl_ignores_malformed_lines()
    test_extract_kimi_wire_user_and_assistant()
    test_extract_codex_jsonl_user_and_assistant()
    test_extract_unknown_format_raises()
    print("ok - transcript parser")
