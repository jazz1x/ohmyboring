#!/usr/bin/env python3
"""Lazy backfill collector for Kimi Code CLI sessions.

Scans ~/.kimi-code/session_index.jsonl for sessions that have not yet been
distilled (no .ts marker) and processes a small batch per run. Designed to be
invoked from cron/launchd so long or past Kimi sessions slowly drain into the
vault without blocking the active session.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import omb_env

DRUDGE_URL = omb_env.drudge_url()  # OMB_URL canonical, DRUDGE_URL deprecated alias
KIMI_HOME = os.environ.get("KIMI_CODE_HOME") or os.path.expanduser("~/.kimi-code")
OMB_HOME = os.environ.get("OMB_HOME") or omb_env.omb_home()
HOOK = os.path.join(OMB_HOME, "agents", "kimi", "distill-session.py")
MARK_DIR = os.path.expanduser("~/.cache/boring-distill")
LIMIT = int(os.environ.get("COLLECT_LIMIT") or "1")
WINDOW_H = float(os.environ.get("COLLECT_WINDOW_HOURS") or "720")


def _marked(session_id: str) -> bool:
    safe = "".join(c for c in session_id if c.isalnum() or c in "_-") or "nosession"
    return os.path.exists(os.path.join(MARK_DIR, f"{safe}.ts")) or os.path.exists(
        os.path.join(MARK_DIR, f"{safe}.pending")
    )


def _session_age_hours(session_dir: str) -> float:
    try:
        return (time.time() - os.path.getmtime(session_dir)) / 3600.0
    except OSError:
        return float("inf")


def _load_index():
    path = os.path.join(KIMI_HOME, "session_index.jsonl")
    if not os.path.exists(path):
        return []
    sessions = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("sessionId") and rec.get("sessionDir"):
                sessions.append(rec)
    return sessions


def _distill(session_id: str, cwd: str) -> bool:
    payload = json.dumps(
        {"session_id": session_id, "cwd": cwd, "hook_event_name": "SessionEnd"}
    ).encode("utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, HOOK],
            input=payload,
            capture_output=True,
            timeout=180,
        )
        return proc.returncode == 0
    except Exception as e:
        print(f"[collect-kimi] failed to distill {session_id}: {e}", file=sys.stderr)
        return False


def _sync():
    try:
        req = urllib.request.Request(
            f"{DRUDGE_URL.rstrip('/')}/sync",
            data=b"",
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=600) as r:
            r.read()
    except Exception as e:
        print(f"[collect-kimi] sync call failed: {e}", file=sys.stderr)


def main():
    if not os.path.exists(HOOK):
        print(f"[collect-kimi] hook not found: {HOOK}", file=sys.stderr)
        sys.exit(1)

    sessions = _load_index()
    processed = 0
    for rec in sessions:
        if processed >= LIMIT:
            break
        sid = rec["sessionId"]
        if _marked(sid):
            continue
        sdir = rec["sessionDir"]
        if not os.path.isdir(sdir):
            continue
        if _session_age_hours(sdir) > WINDOW_H:
            continue
        cwd = rec.get("workDir") or ""
        if _distill(sid, cwd):
            processed += 1
            print(f"[collect-kimi] distilled {sid}")
        else:
            print(f"[collect-kimi] retry marker left for {sid}", file=sys.stderr)

    if processed:
        _sync()
    print(f"[collect-kimi] processed {processed} session(s)")


if __name__ == "__main__":
    main()
