#!/usr/bin/env python3
"""Claude Code SessionEnd/Stop hook — distill a session into memory via the local LLM.

Install (persistence) — ~/.claude/settings.json:
  {"type":"command","command":"python3 ~/oh-my-boring/hooks/distill-session.py",
   "timeout":130,"async":true}
"""
import json
import os
import sys

# Allow import of shared agent policy library regardless of how this script is invoked.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import boring_config
import transcript
from distill_core import (  # noqa: F401
    _extract_json,
    _mark,
    _mark_path,
    _strip_trailing_metadata,
    _build_prompt,
    _call_llm,
    _call_remember,
    _throttled,
    distill_and_remember,
    git_remote_url,
    repo_slug,
)

# Re-export generic helpers at module top level so existing tests can keep using them.
# fmt: off
__all__ = [
    "_extract_json", "_mark", "_mark_path", "_strip_trailing_metadata",
    "_build_prompt", "_call_llm", "_call_remember", "_throttled",
    "distill_and_remember", "git_remote_url", "repo_slug", "extract", "main",
]
# fmt: on

TRANSCRIPT_FORMAT = boring_config.agent_config("claude-code").get("format") or "claude-json"


def extract(path):
    """Extract user/assistant text from a session transcript using the configured format."""
    return transcript.extract(path, TRANSCRIPT_FORMAT)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(f"[omb-distill] invalid stdin JSON: {e}", file=sys.stderr)
        return

    transcript_path = data.get("transcript_path") or ""
    if not transcript_path or not os.path.exists(transcript_path):
        print(f"[omb-distill] transcript not found: {transcript_path!r}", file=sys.stderr)
        return

    session_id = data.get("session_id") or ""
    is_final = (data.get("hook_event_name") or "") == "SessionEnd"
    if not is_final and _throttled(session_id):
        return

    cwd = data.get("cwd") or ""
    remote_url = git_remote_url(cwd)
    origin, _rule = boring_config.classify(cwd, remote_url or None)
    text = extract(transcript_path)
    if len(text) < 500:
        print("[omb-distill] transcript too short; skipping", file=sys.stderr)
        return

    repo = repo_slug(cwd)
    if distill_and_remember(text, origin, repo, session_id):
        _mark(session_id)
        print("[omb-distill] remembered", file=sys.stderr)
    else:
        _mark(session_id, retry=True)
        print("[omb-distill] remember failed; marked for retry", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[omb-distill] crashed: {e}", file=sys.stderr)
    sys.exit(0)
