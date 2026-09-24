#!/usr/bin/env python3
"""Shared transcript parser for agent session logs.

Currently supports the Claude Code JSONL format. The `format` field from
boring.json selects the parser; unknown formats are rejected loudly so a
misconfigured agent does not silently produce empty notes.
"""

import json
import os
import re
import sys


def _env_int(name: str, default: int) -> int:
    """Read an int constant from env, falling back to `default` when unset/blank."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


# Head/tail split for clamp_text: keep the start of the transcript (the ask)
# and bias the remainder toward the tail (decision/result land last).
_CLAMP_HEAD_NUMERATOR = 2
_CLAMP_HEAD_DENOMINATOR = 5

# codex-jsonl clamp ceiling. Raised 4000 -> 12000 (2026-08-04): measured against
# real ~/.codex/sessions data, the median codex-jsonl extract() is ~8.4k chars —
# above the old 4000 ceiling — and a 12k-char prompt still lands well inside the
# 120s LLM call timeout (agents/shared/distill_core.py:_call_llm) against the
# local qwen3:14b endpoint (benchmarked 26-81s for 4k-24k char prompts, no
# monotonic blowup). Override with CODEX_DISTILL_CLAMP or the shared INGEST_CLAMP.
CODEX_CLAMP_DEFAULT = 12000


def codex_distill_clamp() -> int:
    """Resolve the codex-jsonl clamp limit, honouring the existing env knobs."""
    raw = os.environ.get("CODEX_DISTILL_CLAMP") or os.environ.get("INGEST_CLAMP")
    return int(raw) if raw else CODEX_CLAMP_DEFAULT


CLAMP_MARK = "…(truncated)…"


def clamp_text(text, limit):
    """Return a head/tail-clamped transcript and whether it was shortened.

    Cut points snap to the nearest newline so a kept `[role] ...` turn is never
    sliced mid-line.
    """
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = limit * _CLAMP_HEAD_NUMERATOR // _CLAMP_HEAD_DENOMINATOR
    tail = limit - head
    head_cut = text.rfind("\n", 0, head + 1)
    if head_cut <= 0:
        head_cut = head
    tail_start = len(text) - tail
    tail_nl = text.find("\n", tail_start)
    if tail_nl != -1:
        tail_start = tail_nl + 1
    return text[:head_cut] + f"\n{CLAMP_MARK}\n" + text[tail_start:], True


# Claude Code tool_use blocks dominate raw bytes (measured: dropped tool content is
# 10.5x what plain text extraction keeps) but most of that bulk is what the session
# READ (Read file bodies, tool_result output), not what it DID. Bash/Edit/Write are
# the mutating actions — the same "did, not read" split #194 drew for codex
# (exec_command/update_plan in, function_call_output/reasoning out). TodoWrite mirrors
# codex's update_plan (plan/status narrative); Read and Grep are excluded on purpose —
# on a 2936-session sample they are pure investigation (Read 18912, Grep 8 tool calls)
# and Edit/Write already capture the file path once real changes are made. Bodies
# (old_string/new_string/content) are never taken — file_path only, per the
# must_not: no tool_result bodies.
_CLAUDE_TOOL_ALLOWLIST = {
    "Bash": lambda args: (
        args.get("command", "") + (f"  # {args['description']}" if args.get("description") else "")
    ),
    "Edit": lambda args: args.get("file_path", ""),
    "Write": lambda args: args.get("file_path", ""),
    "TodoWrite": lambda args: "; ".join(
        f"{t.get('status', '?')}:{t.get('content', '')}"
        for t in (args.get("todos") or [])
        if isinstance(t, dict)
    ),
}

CLAUDE_TOOL_ARG_CHARS = _env_int("CLAUDE_TOOL_ARG_CHARS", 200)


def _claude_tool_call_text(name: str, arguments) -> str:
    """Return a short, allowlisted signature for a Claude tool_use block, or ''."""
    extractor = _CLAUDE_TOOL_ALLOWLIST.get(name)
    if extractor is None or not isinstance(arguments, dict):
        return ""
    text = (extractor(arguments) or "").strip()
    return text[:CLAUDE_TOOL_ARG_CHARS]


# Claude-jsonl clamp ceiling. Widening extraction to include tool_use signatures
# roughly doubles the median extract() size (measured on 40 real sessions, seed 11:
# median 12420 -> 24243 chars; p90 179763; max 1525152 before clamping) — the
# pre-widening default (2000, hardcoded in agents/claude-code/distill-session.py,
# set when this path only saw text blocks) is now too tight to keep a useful slice
# of the new tool-action lines. Benchmarked against the live qwen3:14b endpoint on
# the largest post-change extraction in that sample, clamped to 4000/6000/8000/12000
# chars: 45.5s/45.7s/33.7s/51.4s — no monotonic blowup, same shape codex saw when it
# set its own ceiling to 12000. Matching that ceiling here keeps ~69s of margin
# under the 120s call timeout. Override with DISTILL_CLAMP or the shared
# INGEST_CLAMP — same env knobs already read by agents/claude-code/distill-session.py.
CLAUDE_CLAMP_DEFAULT = 12000


def claude_distill_clamp() -> int:
    """Resolve the claude-jsonl clamp limit, honouring the existing env knobs."""
    raw = os.environ.get("DISTILL_CLAMP") or os.environ.get("INGEST_CLAMP")
    return int(raw) if raw else CLAUDE_CLAMP_DEFAULT


# The kimi path had no clamp accessor and no clamp call at all: `agents/kimi/distill-session.py`
# handed extract_session() straight to distill_and_remember(), whose own hardcoded truncation was
# the only bound in the chain. That made the inner bound load-bearing for one path and dead for
# the other two, which is why it could not simply be deleted. Same ceiling as the other two so
# the three paths share one number rather than three.
KIMI_CLAMP_DEFAULT = 12000


def kimi_distill_clamp() -> int:
    """Resolve the kimi-wire clamp limit, honouring the same env knobs as the other paths."""
    raw = os.environ.get("KIMI_DISTILL_CLAMP") or os.environ.get("INGEST_CLAMP")
    return int(raw) if raw else KIMI_CLAMP_DEFAULT


# Guard against *unbounded* input reaching the model when a caller forgot to clamp — deliberately
# well above the three per-path defaults so a raised clamp passes through untouched. A guard set at
# the same value as the knob it guards is a second, hidden knob: the previous one sat at 12000 and
# silently cut a clamp raised to 20000 straight back down. Genuinely unclamped input is an order of
# magnitude larger again (measured p90 raw extraction 179,763 chars, max 1,525,152), so the net
# still catches what it is for. Lives here because this module is where every clamp number lives.
BACKSTOP_CLAMP_DEFAULT = 48000


def distill_backstop_clamp() -> int:
    """Resolve the last-resort backstop used when a caller passes unclamped text."""
    return _env_int("DISTILL_BACKSTOP_CLAMP", BACKSTOP_CLAMP_DEFAULT)


def _extract_claude_jsonl(path: str) -> str:
    """Extract user/assistant text and allowlisted tool actions from a Claude Code JSONL transcript."""
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
                t = c.strip()
                t = _claude_owner_text(obj, t) if role == "user" else t
                if t:
                    out.append(f"[{role}] {t}")
                continue
            if not isinstance(c, list):
                continue
            t = " ".join(
                b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            t = _claude_owner_text(obj, t) if role == "user" else t
            if t:
                out.append(f"[{role}] {t}")
            for b in c:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name = b.get("name") or ""
                sig = _claude_tool_call_text(name, b.get("input"))
                if sig:
                    out.append(f"[tool] {name} {sig}")
    return "\n".join(out)


def claude_entrypoint(path: str) -> str:
    """The `entrypoint` of the first Claude Code JSONL row that carries one; empty when none does."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("entrypoint"):
                return str(obj["entrypoint"])
    return ""


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
    "<recommended_plugins>",
    "<shell>",
    "<cwd>",
)

# Prefix, not substring: the owner quoting a tag mid-sentence is still the owner speaking.
_CLAUDE_USER_NOISE_MARKERS = (
    "<task-notification>",
    "<command-message>",
    "<command-name>",
    "<local-command-stdout>",
    "<bash-stdout>",
    "<system-reminder>",
)


_CLAUDE_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)


def _claude_owner_text(obj: dict, text: str) -> str:
    """The part of a role=user row the owner typed; empty when the harness wrote all of it."""
    if text.startswith(("<command-message>", "<command-name>")):
        m = _CLAUDE_COMMAND_ARGS.search(text)
        return m.group(1).strip() if m else ""
    if obj.get("isMeta") or obj.get("isCompactSummary") or text.startswith(_CLAUDE_USER_NOISE_MARKERS):
        return ""
    return text


# raw codex-jsonl bytes are dominated by function_call_output (huge shell/file
# dumps) and reasoning (hidden CoT) — both stay dropped, they are why extraction
# was ~2% of raw bytes in the first place and re-including them would just move
# the ceiling from clamp back onto extraction. function_call signatures are
# small (~11% of raw bytes for ~35 calls/session median) and, for exec_command /
# update_plan specifically, carry real "what was tried" narrative that the
# [user]/[assistant] turns alone can miss (measured: sessions with 30+ tool
# calls and <5k chars of message narrative). Every other tool name is dropped
# rather than guessed at — send_message payloads observed in the wild are
# opaque/encrypted blobs, not narrative.
_CODEX_TOOL_ALLOWLIST = {
    "exec_command": lambda args: args.get("cmd"),
    "update_plan": lambda args: "; ".join(
        f"{s.get('status', '?')}:{s.get('step', '')}" for s in (args.get("plan") or []) if isinstance(s, dict)
    ),
}

CODEX_TOOL_ARG_CHARS = _env_int("CODEX_TOOL_ARG_CHARS", 200)


def _codex_tool_call_text(name: str, arguments) -> str:
    """Return a short, allowlisted signature for a Codex function_call, or ''."""
    extractor = _CODEX_TOOL_ALLOWLIST.get(name)
    if extractor is None:
        return ""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    except json.JSONDecodeError:
        return ""
    if not isinstance(args, dict):
        return ""
    text = (extractor(args) or "").strip()
    return text[:CODEX_TOOL_ARG_CHARS]


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
                if payload.get("type") == "function_call":
                    name = payload.get("name") or ""
                    sig = _codex_tool_call_text(name, payload.get("arguments"))
                    if sig:
                        out.append(f"[tool] {name} {sig}")
                    continue
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
