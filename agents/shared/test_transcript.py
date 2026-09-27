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


def test_extract_claude_jsonl_includes_allowlisted_tool_calls():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        _write(
            f.name,
            [
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "checking the tests"},
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "rg -n TODO src/", "description": "find TODOs"},
                            },
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "input": {
                                    "file_path": "agents/shared/transcript.py",
                                    "old_string": "SECRET_OLD_BODY",
                                    "new_string": "SECRET_NEW_BODY",
                                },
                            },
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "input": {
                                    "file_path": "agents/shared/new_file.py",
                                    "content": "SECRET_FILE_BODY",
                                },
                            },
                            {
                                "type": "tool_use",
                                "name": "TodoWrite",
                                "input": {"todos": [{"content": "inventory files", "status": "in_progress"}]},
                            },
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {"file_path": "agents/shared/transcript.py"},
                            },
                        ],
                    }
                },
                {
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "content": "SECRET_TOOL_RESULT_BODY",
                            }
                        ],
                    }
                },
            ],
        )
        path = f.name
    try:
        out = transcript.extract(path, "claude-json")
        assert "[tool] Bash rg -n TODO src/  # find TODOs" in out
        assert "[tool] Edit agents/shared/transcript.py" in out
        assert "[tool] Write agents/shared/new_file.py" in out
        assert "[tool] TodoWrite in_progress:inventory files" in out
        assert "SECRET_OLD_BODY" not in out
        assert "SECRET_NEW_BODY" not in out
        assert "SECRET_FILE_BODY" not in out
        assert "SECRET_TOOL_RESULT_BODY" not in out
        assert "[tool] Read" not in out
    finally:
        os.unlink(path)


def test_extract_claude_jsonl_drops_unknown_tool_calls():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        _write(
            f.name,
            [
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Grep",
                                "input": {"pattern": "TODO", "path": "src/"},
                            },
                            {
                                "type": "tool_use",
                                "name": "WebFetch",
                                "input": {"url": "https://example.com"},
                            },
                        ],
                    }
                },
            ],
        )
        path = f.name
    try:
        out = transcript.extract(path, "claude-json")
        assert out == ""
    finally:
        os.unlink(path)


def test_extract_claude_jsonl_user_lines_are_only_what_the_owner_typed():
    def user(content, **flags):
        return {"type": "user", **flags, "message": {"role": "user", "content": content}}

    harness_rows = [
        user([{"type": "text", "text": "Base directory for this skill: /x"}], isMeta=True),
        user("This session is being continued from a previous conversation.", isCompactSummary=True),
        user("<task-notification>\n<task-id>a1</task-id> worker done</task-notification>"),
        user([{"type": "text", "text": "<command-message>pr-craft is running…</command-message>"}]),
        user("<command-name>/clear</command-name>"),
        user("<local-command-stdout>Compacted</local-command-stdout>"),
        user("<bash-stdout>ok</bash-stdout><bash-stderr></bash-stderr>"),
        user("<system-reminder>Plan mode is active.</system-reminder>"),
    ]
    kept_rows = [
        user("테스트 돌려줘"),
        user([{"type": "text", "text": "D4 는 간선으로 가요"}]),
        user(
            "<command-message>loop</command-message>\n<command-name>/loop</command-name>\n"
            "<command-args>D4 는 간선으로 간다\n순위는 아직</command-args>"
        ),
        user("그 <task-notification> 은 무시해"),
        user('<pasted_content id="0e6b">owner paste</pasted_content>'),
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "네"}]}},
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        _write(f.name, harness_rows + kept_rows)
        path = f.name
    try:
        out = transcript.extract(path, "claude-json")
        assert out.splitlines() == [
            "[user] 테스트 돌려줘",
            "[user] D4 는 간선으로 가요",
            "[user] D4 는 간선으로 간다",
            "순위는 아직",
            "[user] 그 <task-notification> 은 무시해",
            '[user] <pasted_content id="0e6b">owner paste</pasted_content>',
            "[assistant] 네",
        ], out
    finally:
        os.unlink(path)


def test_claude_distill_clamp_default_and_env_override():
    saved = {k: os.environ.pop(k, None) for k in ("DISTILL_CLAMP", "INGEST_CLAMP")}
    try:
        assert transcript.claude_distill_clamp() == transcript.CLAUDE_CLAMP_DEFAULT

        os.environ["DISTILL_CLAMP"] = "9999"
        assert transcript.claude_distill_clamp() == 9999
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


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


def test_extract_codex_jsonl_includes_allowlisted_tool_calls():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        _write(
            f.name,
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": "rg -n TODO src/", "max_output_tokens": 4000}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "update_plan",
                        "arguments": json.dumps(
                            {"plan": [{"step": "inventory files", "status": "in_progress"}]}
                        ),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "send_message",
                        "arguments": json.dumps({"target": "/root/x", "message": "gAAAAA_opaque_blob"}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {"type": "function_call_output", "output": "huge raw dump" * 100},
                },
            ],
        )
        path = f.name
    try:
        out = transcript.extract(path, "codex-jsonl")
        assert "[tool] exec_command rg -n TODO src/" in out
        assert "[tool] update_plan in_progress:inventory files" in out
        assert "send_message" not in out
        assert "opaque_blob" not in out
        assert "huge raw dump" not in out
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


def test_clamp_text_preserves_head_and_tail():
    text = "0123456789" * 10

    clamped, changed = transcript.clamp_text(text, 25)

    assert changed is True
    assert clamped.startswith("0123456789")
    assert clamped.endswith("567890123456789")
    assert "truncated" in clamped


def test_clamp_text_keeps_short_input():
    text = "short transcript"

    clamped, changed = transcript.clamp_text(text, 100)

    assert changed is False
    assert clamped == text


def test_clamp_text_snaps_to_newline_not_mid_line():
    lines = [f"[user] turn {i} filler text here" for i in range(20)]
    text = "\n".join(lines)

    clamped, changed = transcript.clamp_text(text, 200)

    assert changed is True
    head, _, tail = clamped.partition("\n…(truncated)…\n")
    # Every kept line on both sides must be a whole line from the source, never
    # a ragged mid-line fragment.
    for line in head.split("\n") + tail.split("\n"):
        assert line == "" or line in lines


def test_kimi_distill_clamp_default_and_env_override():
    """The kimi path had no accessor at all; its absence is what made the inner backstop
    load-bearing for one path and dead for the others."""
    for var in ("KIMI_DISTILL_CLAMP", "INGEST_CLAMP"):
        os.environ.pop(var, None)
    assert transcript.kimi_distill_clamp() == transcript.KIMI_CLAMP_DEFAULT
    os.environ["KIMI_DISTILL_CLAMP"] = "7777"
    try:
        assert transcript.kimi_distill_clamp() == 7777
    finally:
        os.environ.pop("KIMI_DISTILL_CLAMP", None)


def test_codex_distill_clamp_default_and_env_override():
    saved = {k: os.environ.pop(k, None) for k in ("CODEX_DISTILL_CLAMP", "INGEST_CLAMP")}
    try:
        assert transcript.codex_distill_clamp() == transcript.CODEX_CLAMP_DEFAULT

        os.environ["CODEX_DISTILL_CLAMP"] = "9999"
        assert transcript.codex_distill_clamp() == 9999
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


if __name__ == "__main__":
    test_extract_claude_jsonl_text_and_list_content()
    test_extract_claude_jsonl_ignores_malformed_lines()
    test_extract_claude_jsonl_includes_allowlisted_tool_calls()
    test_extract_claude_jsonl_drops_unknown_tool_calls()
    test_extract_claude_jsonl_user_lines_are_only_what_the_owner_typed()
    test_claude_distill_clamp_default_and_env_override()
    test_extract_kimi_wire_user_and_assistant()
    test_extract_codex_jsonl_user_and_assistant()
    test_extract_codex_jsonl_includes_allowlisted_tool_calls()
    test_extract_unknown_format_raises()
    test_clamp_text_preserves_head_and_tail()
    test_clamp_text_keeps_short_input()
    test_clamp_text_snaps_to_newline_not_mid_line()
    test_codex_distill_clamp_default_and_env_override()
    test_kimi_distill_clamp_default_and_env_override()
    print("ok - transcript parser")
