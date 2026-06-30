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
import event_log
import markers
import omb_env
import workflow_contract
from drudge_client import DrudgeClient

BORING_URL = omb_env.drudge_url()  # BORING_URL canonical, BORING_URL deprecated alias
KIMI_HOME = os.environ.get("KIMI_CODE_HOME") or os.path.expanduser("~/.kimi-code")
BORING_HOME = os.environ.get("BORING_HOME") or omb_env.omb_home()
HOOK = os.path.join(BORING_HOME, "agents", "kimi", "distill-session.py")
LIMIT = int(os.environ.get("COLLECT_LIMIT") or "1")
WINDOW_H = float(os.environ.get("COLLECT_WINDOW_HOURS") or "720")
PENDING_TTL = float(os.environ.get("COLLECT_PENDING_TTL") or os.environ.get("INGEST_PENDING_TTL") or "1800")


def _marked(session_id: str) -> bool:
    return markers.is_done(session_id) or markers.is_pending(session_id, ttl=PENDING_TTL)


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


def _sync() -> bool:
    try:
        DrudgeClient().sync()
    except Exception as e:
        print(f"[collect-kimi] sync call failed: {e}", file=sys.stderr)
        return False
    return True


def main():
    run_id = event_log.new_run_id("kimi-collector")
    if not os.path.exists(HOOK):
        print(f"[collect-kimi] hook not found: {HOOK}", file=sys.stderr)
        event_log.try_append_event(
            "kimi-collector",
            "collector_run",
            "failed",
            run_id=run_id,
            agent="kimi",
            failed=1,
            reason="hook_missing",
            **workflow_contract.collector_run_fields("failed", 1),
        )
        return 1

    sessions = _load_index()
    eligible = 0
    attempted = 0
    processed = 0
    failed = 0
    for rec in sessions:
        if attempted >= LIMIT:
            break
        sid = rec["sessionId"]
        if _marked(sid):
            continue
        sdir = rec["sessionDir"]
        if not os.path.isdir(sdir):
            continue
        if _session_age_hours(sdir) > WINDOW_H:
            continue
        eligible += 1
        cwd = rec.get("workDir") or ""
        attempted += 1
        if _distill(sid, cwd):
            processed += 1
            print(f"[collect-kimi] distilled {sid}")
        else:
            failed += 1
            print(f"[collect-kimi] retry marker left for {sid}", file=sys.stderr)

    sync_status = "skipped"
    if processed:
        sync_status = "ok" if _sync() else "failed"
    print(f"[collect-kimi] processed {processed} session(s)")
    status = "ok" if failed == 0 and sync_status != "failed" else "failed"
    event_log.try_append_event(
        "kimi-collector",
        "collector_run",
        status,
        run_id=run_id,
        agent="kimi",
        eligible=eligible,
        attempted=attempted,
        processed=processed,
        failed=failed,
        sync_status=sync_status,
        **workflow_contract.collector_run_fields(status, attempted),
    )
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
