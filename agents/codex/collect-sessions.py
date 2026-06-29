#!/usr/bin/env python3
"""Lazy backfill collector for GitHub Codex sessions.

Codex has no SessionEnd hook we can install, so this collector scans the local
Codex session directory (`~/.codex/sessions`) and distills a small batch of
un-ingested sessions per run. It shares marker state with the rest of the
oh-my-boring ingestion pipeline via `~/.cache/boring-distill`.

- Marker: ~/.cache/boring-distill/codex-<sid>.ts (done) / .pending / .retry
- LIMIT (default 1, COLLECT_LIMIT): number processed per invocation.
- WINDOW (default 720h=30d, COLLECT_WINDOW_HOURS): ignore anything too old.
- Subagent sessions (guardian, etc.) are skipped by default; set
  CODEX_INCLUDE_SUBAGENTS=1 to ingest them too.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import boring_config
import markers
import omb_env
from drudge_client import DrudgeClient

BORING_URL = omb_env.drudge_url()
WINDOW_H = float(os.environ.get("COLLECT_WINDOW_HOURS") or "720")
LIMIT = int(os.environ.get("COLLECT_LIMIT") or "1")
MIN_KB = float(os.environ.get("COLLECT_MIN_KB") or "20")
BORING_HOME = os.environ.get("BORING_HOME") or omb_env.omb_home()
HOOK = os.path.join(BORING_HOME, "agents/codex/distill-session.py")
INCLUDE_SUBAGENTS = os.environ.get("CODEX_INCLUDE_SUBAGENTS", "").lower() in ("1", "true", "yes")


def _source_dir():
    """Resolve the Codex sessions directory, including inside the hermes container."""
    if omb_env._in_container():
        return "/host/.codex/sessions"
    return os.path.expanduser("~/.codex/sessions")


def _codex_session_id(path: str) -> str:
    """Stable session id from the transcript filename (UUID suffix)."""
    return os.path.splitext(os.path.basename(path))[0]


def _marked(session_id: str) -> bool:
    prefixed = f"codex-{session_id}"
    return markers.is_done(prefixed) or markers.is_pending(prefixed)


def _is_subagent(path: str) -> bool:
    """True if the first line says this is a subagent/guardian roll-out."""
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline()
    except OSError as e:
        print(f"[codex-collect] cannot read transcript header {path}: {e}", file=sys.stderr)
        return False
    if not first:
        return False
    try:
        meta = json.loads(first).get("payload", {})
    except json.JSONDecodeError as e:
        print(f"[codex-collect] malformed transcript header {path}: {e}", file=sys.stderr)
        return False
    if meta.get("thread_source") == "subagent":
        return True
    source = meta.get("source") or {}
    if isinstance(source, dict) and source.get("subagent"):
        return True
    return False


def _transcript_cwd(path: str) -> str:
    """Best-effort cwd from the session_meta payload."""
    try:
        with open(path, encoding="utf-8") as f:
            for _ in range(10):
                line = f.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "session_meta":
                    return obj.get("payload", {}).get("cwd", "")
    except OSError as e:
        print(f"[codex-collect] cannot read transcript cwd {path}: {e}", file=sys.stderr)
    return ""


def main():
    ap = argparse.ArgumentParser(description="Backfill past Codex sessions into ohmyboring.")
    ap.add_argument(
        "--now",
        action="store_true",
        help="distill the MOST RECENT session immediately, ignoring done-markers and WITHOUT marking "
        "it done — so it is re-distillable on demand.",
    )
    args = ap.parse_args()

    cutoff = time.time() - WINDOW_H * 3600
    source_dir = _source_dir()
    if not os.path.isdir(source_dir):
        print(f"[codex-collect] source dir not found: {source_dir}", file=sys.stderr)
        return 0

    paths = glob.glob(os.path.join(source_dir, "**", "*.jsonl"), recursive=True)
    todo = []
    for p in paths:
        if os.path.getmtime(p) < cutoff:
            continue
        if os.path.getsize(p) < MIN_KB * 1024:
            continue
        sid = _codex_session_id(p)
        if not args.now and _marked(sid):
            continue
        if not INCLUDE_SUBAGENTS and _is_subagent(p):
            continue
        todo.append(p)

    todo.sort(key=os.path.getmtime, reverse=True)
    batch = todo[:1] if args.now else todo[:LIMIT]
    label = "distill-now" if args.now else "collect"
    print(f"[{label}] pending={len(todo)} this_batch={len(batch)} (LIMIT={1 if args.now else LIMIT})", flush=True)
    if not batch:
        print(f"[{label}] nothing to do", flush=True)
        return 0

    env = dict(os.environ)
    if args.now:
        env["BORING_DISTILL_NO_MARK"] = "1"
    done = 0
    for tp in batch:
        sid = _codex_session_id(tp)
        cwd = _transcript_cwd(tp)
        payload = json.dumps(
            {
                "transcript_path": tp,
                "cwd": cwd,
                "session_id": sid,
                "hook_event_name": "SessionEnd",
                "raw_bytes": os.path.getsize(tp),
                "min_raw_bytes_for_retry": int(MIN_KB * 1024),
            }
        )
        r = subprocess.run([sys.executable, HOOK], input=payload, text=True, env=env)
        done += 1 if r.returncode == 0 else 0
        print(f"[{label}] {'ok' if r.returncode == 0 else 'fail'}  {sid}", flush=True)

    try:
        DrudgeClient().sync()
        print(f"[{label}] sync ok", flush=True)
    except Exception as e:
        print(f"[{label}] sync failed: {e}", file=sys.stderr, flush=True)
        return 1
    print(f"[{label}] done={done}/{len(batch)}  remaining={len(todo) - done}", flush=True)
    return 0 if done == len(batch) else 1


if __name__ == "__main__":
    sys.exit(main())
