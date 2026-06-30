#!/usr/bin/env python3
"""Kimi Code CLI SessionEnd hook — distill a session into memory via the local LLM.

Install (persistence) — ~/.kimi-code/config.toml:
  [[hooks]]
  event = "SessionEnd"
  command = "python3 ~/oh-my-boring/hooks/kimi-distill-session.py"
  timeout = 130

The hook receives a JSON payload on stdin. Unlike Claude Code, Kimi does not pass
`transcript_path`; we resolve the session directory from `session_id` + `cwd` using
`~/.kimi-code/session_index.jsonl` (or the documented `wd_<slug>_<hash>` bucket layout).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import boring_config
from distill_core import (  # noqa: F401
    _mark,
    _throttled,
    distill_and_remember,
    git_remote_url,
    repo_slug,
)

KIMI_HOME = os.environ.get("KIMI_CODE_HOME") or os.path.expanduser("~/.kimi-code")


def _work_dir_key(cwd: str) -> str:
    """Return the Kimi workDirKey bucket for a cwd: wd_<slug>_<first-12-sha256>."""
    abspath = os.path.abspath(cwd)
    slug = os.path.basename(abspath.rstrip("/")) or "unknown"
    slug = re.sub(r"[^A-Za-z0-9_-]", "", slug)[:20]
    h = hashlib.sha256(abspath.encode("utf-8")).hexdigest()[:12]
    return f"wd_{slug}_{h}"


def _find_session_dir(session_id: str, cwd: str) -> str | None:
    """Resolve the on-disk session directory from session_id (+ cwd fallback)."""
    if not session_id:
        return None

    # 1. Prefer the official index (authoritative mapping).
    index_path = os.path.join(KIMI_HOME, "session_index.jsonl")
    if os.path.exists(index_path):
        try:
            with open(index_path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("sessionId") == session_id:
                        d = rec.get("sessionDir")
                        if d and os.path.isdir(d):
                            return d
        except OSError:
            pass

    # 2. Fallback: derive the bucket from cwd.
    if cwd:
        d = os.path.join(KIMI_HOME, "sessions", _work_dir_key(cwd), session_id)
        if os.path.isdir(d):
            return d

    return None


def _text_from_content(content) -> str:
    """Extract plain text from a Kimi message content (string, part dict, or part list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        t = content.get("type")
        if t == "text":
            return content.get("text", "")
        if t == "tool_use" and content.get("name"):
            return f"<tool:{content['name']}>"
        return ""
    if isinstance(content, list):
        parts = [_text_from_content(part) for part in content]
        return " ".join(p for p in parts if p)
    return ""


def extract_session(session_dir: str) -> str:
    """Extract user/assistant text from a Kimi Code session directory."""
    wire_path = os.path.join(session_dir, "agents", "main", "wire.jsonl")
    if not os.path.exists(wire_path):
        return ""

    out = []
    with open(wire_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = obj.get("type")
            if t == "turn.prompt":
                inp = obj.get("input") or []
                text = " ".join(_text_from_content(part) for part in inp).strip()
                if text:
                    out.append(f"[user] {text}")

            elif t == "context.append_message":
                msg = obj.get("message") or {}
                role = msg.get("role")
                if role == "user":
                    # Ignore system/injection messages; keep only real user input.
                    origin = msg.get("origin") or {}
                    if isinstance(origin, dict) and origin.get("kind") != "user":
                        continue
                    text = _text_from_content(msg.get("content"))
                    if text:
                        out.append(f"[user] {text}")
                elif role == "assistant":
                    text = _text_from_content(msg.get("content"))
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
                elif ev.get("type") == "tool.call":
                    name = ev.get("name") or ""
                    args = ev.get("args") or {}
                    if name:
                        out.append(f"[tool] {name}: {json.dumps(args, ensure_ascii=False)}")
                elif ev.get("type") == "tool.result":
                    result = ev.get("result") or {}
                    output = result.get("output") or ""
                    if output:
                        out.append(f"[tool-result] {str(output)[:400]}")

    return "\n".join(out)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(f"[omb-distill] invalid stdin JSON: {e}", file=sys.stderr)
        return 2

    session_id = data.get("session_id") or data.get("sessionId") or ""
    cwd = data.get("cwd") or ""
    is_final = (data.get("hook_event_name") or "") == "SessionEnd"
    if not is_final and _throttled(session_id):
        return 0

    session_dir = _find_session_dir(session_id, cwd)
    if not session_dir:
        print(f"[omb-distill] session dir not found for {session_id}", file=sys.stderr)
        return 2

    text = extract_session(session_dir)
    if len(text) < 500:
        print("[omb-distill] transcript too short; skipping", file=sys.stderr)
        return 0

    remote_url = git_remote_url(cwd)
    origin, _rule = boring_config.classify(cwd, remote_url or None)
    repo = repo_slug(cwd)

    if distill_and_remember(text, origin, repo, session_id):
        _mark(session_id)
        print("[omb-distill] remembered", file=sys.stderr)
        return 0
    else:
        _mark(session_id, retry=True)
        print("[omb-distill] remember failed; marked for retry", file=sys.stderr)
        return 1


def run() -> int:
    try:
        return main()
    except Exception as e:
        print(f"[omb-distill] crashed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
