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
    _strip_trailing_metadata,
    _build_prompt,
    _call_llm,
    _call_remember,
    _distill_resolution,
    _throttled,
    distill_and_remember,
    git_remote_url,
    log_skip_event,
    is_automated_run,
    log_uptake_event,
    repo_slug,
)

# Re-export generic helpers at module top level so existing tests can keep using them.
# fmt: off
__all__ = [
    "_extract_json", "_mark", "_strip_trailing_metadata",
    "_build_prompt", "_call_llm", "_call_remember", "_throttled",
    "distill_and_remember", "git_remote_url", "is_automated_run", "log_skip_event", "log_uptake_event", "repo_slug", "extract", "main", "run",
]
# fmt: on

TRANSCRIPT_FORMAT = boring_config.agent_config("claude-code").get("format") or "claude-json"
# Direct SessionEnd distill calls the local LLM synchronously. The ceiling lives in transcript.py
# beside the extractor that produces the text, because the two are one decision: widening what is
# extracted is worthless if this cuts it back down. That is not hypothetical — this line read
# `or "2000"` while extraction was producing a median of 12,420 chars, so the model saw 0.16% of a
# median session and every extraction improvement upstream was silently discarded here.
# `claude_distill_clamp()` reads the same DISTILL_CLAMP knob this line already honoured, so an
# operator override behaves exactly as before. Mirrors agents/codex/distill-session.py.
CLAMP = transcript.claude_distill_clamp()


def extract(path):
    """Extract user/assistant text from a session transcript using the configured format."""
    return transcript.extract(path, TRANSCRIPT_FORMAT)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(f"[omb-distill] invalid stdin JSON: {e}", file=sys.stderr)
        return 2

    # Two log lines in the same second are either two sessions or one payload fired
    # twice, and only the id tells those apart.
    print(f"[omb-distill] session={data.get('session_id') or '?'} event={data.get('hook_event_name') or '?'}", file=sys.stderr)

    transcript_path = data.get("transcript_path") or ""
    if not transcript_path or not os.path.exists(transcript_path):
        print(f"[omb-distill] transcript not found: {transcript_path!r}", file=sys.stderr)
        return 2

    session_id = data.get("session_id") or ""
    is_final = (data.get("hook_event_name") or "") == "SessionEnd"
    if not is_final and _throttled(session_id):
        return 0

    cwd = data.get("cwd") or ""
    remote_url = git_remote_url(cwd)
    origin, _rule = boring_config.classify(cwd, remote_url or None)
    repo = repo_slug(cwd)
    text = extract(transcript_path)
    if is_final:
        # Before the length gate on purpose: a session too short to be worth distilling still
        # received injections, and whether the agent used them is the same measurement.
        log_uptake_event(session_id, repo, text, "claude-code")
    if is_automated_run(text):
        # It ended like a session and it distils like one, which is how a scripted review became a
        # quarter of the corpus. Named in the ledger rather than dropped in silence: a skip nobody
        # can count is indistinguishable from a session that never happened.
        print("[omb-distill] automated run; not memory", file=sys.stderr)
        log_skip_event(session_id, origin, repo, _distill_resolution(), "automated_run")
        if session_id:
            _mark(session_id)
        return 0

    if len(text) < 500:
        print("[omb-distill] transcript too short; skipping", file=sys.stderr)
        log_skip_event(session_id, origin, repo, _distill_resolution(), "too_short")
        if session_id:
            _mark(session_id)
        return 0
    text, was_clamped = transcript.clamp_text(text, CLAMP)
    if was_clamped:
        print(f"[omb-distill] transcript clamped to {len(text)} chars", file=sys.stderr)

    if distill_and_remember(text, origin, repo, session_id):
        _mark(session_id)
        print("[omb-distill] remembered", file=sys.stderr)
        return 0
    else:
        _mark(session_id, retry=True, reason="remember failed")
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
