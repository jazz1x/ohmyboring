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
    print(f"[transcript] unsupported format '{fmt}' for {path} — skipping", file=sys.stderr)
    raise ValueError(f"unsupported transcript format: {fmt}")
