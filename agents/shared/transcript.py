#!/usr/bin/env python3
"""Shared transcript parser for agent session logs.

Currently supports the Claude Code JSONL format. The `format` field from
boring.json selects the parser; unknown formats are rejected loudly so a
misconfigured agent does not silently produce empty notes.
"""
import json
import sys


def _extract_claude_jsonl(path: str) -> str:
    """Extract user/assistant text from a Claude Code JSONL transcript."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") or {}
            role = msg.get("role") or obj.get("type") or ""
            if role not in ("user", "assistant"):
                continue
            c = msg.get("content")
            if isinstance(c, str):
                t = c
            elif isinstance(c, list):
                t = " ".join(
                    b.get("text", "")
                    for b in c
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                t = ""
            t = t.strip()
            if t:
                out.append(f"[{role}] {t}")
    return "\n".join(out)


def _extract_kimi_wire(path: str) -> str:
    """Extract user/assistant text from a Kimi Code CLI wire.jsonl transcript.

    Mirrors the parser in agents/kimi/distill-session.py so the scheduler and
    hook can share the same transcript representation.
    """
    out = []

    def _text(content):
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            if content.get("type") == "text":
                return content.get("text", "")
            if content.get("type") == "tool_use" and content.get("name"):
                return f"<tool:{content['name']}>"
            return ""
        if isinstance(content, list):
            return " ".join(_text(part) for part in content if _text(part))
        return ""

    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t == "turn.prompt":
                inp = obj.get("input") or []
                text = " ".join(_text(part) for part in inp).strip()
                if text:
                    out.append(f"[user] {text}")
            elif t == "context.append_message":
                msg = obj.get("message") or {}
                role = msg.get("role")
                if role == "user":
                    origin = msg.get("origin") or {}
                    if isinstance(origin, dict) and origin.get("kind") != "user":
                        continue
                    text = _text(msg.get("content"))
                    if text:
                        out.append(f"[user] {text}")
                elif role == "assistant":
                    text = _text(msg.get("content"))
                    if text:
                        out.append(f"[assistant] {text}")
            elif t == "context.append_loop_event":
                ev = obj.get("event") or {}
                if ev.get("type") == "content.part":
                    part = ev.get("part") or {}
                    if part.get("type") == "text":
                        text = part.get("text", "").strip()
                        if text:
                            out.append(f"[assistant] {text}")
    return "\n".join(out)


def extract(path: str, fmt: str = "claude-json") -> str:
    """Parse a session transcript at `path` using format `fmt`.

    Args:
        path: filesystem path to the transcript.
        fmt: format identifier from boring.json (default "claude-json").

    Returns:
        Extracted user/assistant text joined by newlines.

    Raises:
        ValueError: if `fmt` is not supported.
        OSError: if `path` cannot be read.
    """
    if fmt == "claude-json":
        return _extract_claude_jsonl(path)
    if fmt == "kimi-wire":
        return _extract_kimi_wire(path)
    print(f"[transcript] unsupported format '{fmt}' for {path} — skipping", file=sys.stderr)
    raise ValueError(f"unsupported transcript format: {fmt}")
