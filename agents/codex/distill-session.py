#!/usr/bin/env python3
"""GitHub Codex session distillation hook.

Parses a Codex JSONL session transcript, extracts user/assistant text, and
stores a curated note via ohmyboring's remember tool. Designed to be called by
both the host-side backfill collector and a hermes-agent cron worker.
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
    _throttled,
    distill_and_remember,
    git_remote_url,
    repo_slug,
)

TRANSCRIPT_FORMAT = "codex-jsonl"


def extract(path):
    """Extract user/assistant text from a Codex JSONL session transcript."""
    return transcript.extract(path, TRANSCRIPT_FORMAT)


def should_retry_short_extract(data: dict, transcript_path: str) -> bool:
    """True when a parse-short result came from a raw transcript large enough to require review."""
    min_raw = data.get("min_raw_bytes_for_retry")
    if min_raw is None:
        return False
    raw_bytes = data.get("raw_bytes")
    if raw_bytes is None:
        raw_bytes = os.path.getsize(transcript_path)
    return int(raw_bytes) >= int(min_raw)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"[omb-distill-codex] invalid stdin JSON: {e}", file=sys.stderr)
        return 2

    transcript_path = data.get("transcript_path") or ""
    if not transcript_path or not os.path.exists(transcript_path):
        print(f"[omb-distill-codex] transcript not found: {transcript_path!r}", file=sys.stderr)
        return 2

    raw_session_id = data.get("session_id") or ""
    # Prefix the marker id so Codex session ids never collide with Claude/Kimi ids.
    session_id = f"codex-{raw_session_id}" if raw_session_id else ""
    is_final = (data.get("hook_event_name") or "") == "SessionEnd"
    if not is_final and _throttled(session_id):
        return 0

    cwd = data.get("cwd") or ""
    remote_url = git_remote_url(cwd)
    origin, _rule = boring_config.classify(cwd, remote_url or None)
    text = extract(transcript_path)
    if len(text) < 500:
        if should_retry_short_extract(data, transcript_path):
            print(
                "[omb-distill-codex] extracted text too short for large transcript; marked for retry",
                file=sys.stderr,
            )
            if session_id:
                _mark(session_id, retry=True)
            return 1
        print("[omb-distill-codex] transcript too short; skipping", file=sys.stderr)
        if session_id:
            _mark(session_id)
        return 0

    repo = repo_slug(cwd)
    if distill_and_remember(text, origin, repo, session_id):
        _mark(session_id)
        print("[omb-distill-codex] remembered", file=sys.stderr)
        return 0
    else:
        _mark(session_id, retry=True)
        print("[omb-distill-codex] remember failed; marked for retry", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
