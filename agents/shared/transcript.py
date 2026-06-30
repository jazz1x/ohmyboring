#!/usr/bin/env python3
"""Shared transcript parser for agent session logs.

Currently supports the Claude Code JSONL format. The `format` field from
boring.json selects the parser; unknown formats are rejected loudly so a
misconfigured agent does not silently produce empty notes.
"""
import json
import sys


def clamp_text(text, limit):
    """Return a head/tail-clamped transcript and whether it was shortened."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = limit * 2 // 5
    return text[:head] + "\n…(truncated)…\n" + text[-(limit - head) :], True


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


# Codex injects large system context blocks as role=user content. We drop those
# items so they do not drown out the actual user turns and blow up the prompt.
_CODEX_USER_NOISE_MARKERS = (
    "# AGENTS.md instructions",
    "<INSTRUCTIONS>",
    "<permissions instructions>",
    "<app-context>",
    "<environment_context>",
    "Filesystem sandboxing",
    "<filesystem>",
    "<current_date>",
    "<shell>",
    "<cwd>",
)


def _codex_content_text(role: str, content) -> str:
    """Extract speakable text from a Codex response_item content payload."""
    parts = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, dict):
        if content.get("type") in ("input_text", "output_text", "text"):
            parts.append(content.get("text", ""))
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                t = item.get("type", "")
                if role == "user" and t == "input_text":
                    text = item.get("text", "")
                    if text and not any(m in text for m in _CODEX_USER_NOISE_MARKERS):
                        parts.append(text)
                elif role == "assistant" and t == "output_text":
                    parts.append(item.get("text", ""))
    return " ".join(p for p in parts if p).strip()


def _extract_codex_jsonl(path: str) -> str:
    """Extract user/assistant text from a GitHub Codex JSONL session transcript."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            payload = obj.get("payload") or {}
            if t == "response_item":
                role = payload.get("role") or ""
                if role not in ("user", "assistant"):
                    continue
                text = _codex_content_text(role, payload.get("content"))
                if text:
                    out.append(f"[{role}] {text}")
            elif t == "event_msg":
                ev_type = payload.get("type")
                if ev_type == "user_message":
                    for te in payload.get("text_elements") or []:
                        if isinstance(te, dict) and te.get("type") == "text":
                            text = te.get("text", "").strip()
                            if text:
                                out.append(f"[user] {text}")
                elif ev_type == "agent_message":
                    last = payload.get("last_agent_message")
                    if isinstance(last, str) and last.strip():
                        out.append(f"[assistant] {last.strip()}")
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
    if fmt == "codex-jsonl":
        return _extract_codex_jsonl(path)
    print(f"[transcript] unsupported format '{fmt}' for {path} — skipping", file=sys.stderr)
    raise ValueError(f"unsupported transcript format: {fmt}")
