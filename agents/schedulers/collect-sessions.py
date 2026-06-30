#!/usr/bin/env python3
"""Lazy backfill collector — slowly ingests a small batch of past sessions per run.

The SessionEnd hook fires only on session *termination* → long/open/past sessions aren't
captured. This collector scans the top-level session .jsonl files under ~/.claude/projects
(excluding subagents/workflows) and distills LIMIT of **only the not-yet-done ones**
(no marker) per run. It doesn't run them all at once, so it doesn't burn CPU.

- Marker: ~/.cache/boring-distill/<sid>.ts, same as distill-session.py. If present, skip (already done).
- LIMIT (default 1, COLLECT_LIMIT): number processed per invocation. Called periodically via launchd/cron → drains slowly.
- WINDOW (default 720h=30d, COLLECT_WINDOW_HOURS): ignore anything too old.
- Each session → distill-session.py (wakes the agent → remember). One /sync at the end re-scans the
  vault (idempotent safety net; remember already ingests each note live).
- cwd = the real working dir from the transcript → distill-session determines origin via boring.json.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import boring_config
import event_log
import markers
import omb_env
import workflow_contract
from drudge_client import DrudgeClient

BORING_URL = omb_env.drudge_url()  # BORING_URL canonical, BORING_URL deprecated alias
WINDOW_H = float(os.environ.get("COLLECT_WINDOW_HOURS") or "720")
LIMIT = int(os.environ.get("COLLECT_LIMIT") or "1")
MIN_KB = float(os.environ.get("COLLECT_MIN_KB") or "20")  # skip small sessions (distill would SKIP anyway)
PENDING_TTL = float(os.environ.get("COLLECT_PENDING_TTL") or os.environ.get("INGEST_PENDING_TTL") or "1800")
# BORING_HOME: repo clone location (default ~/oh-my-boring). Lets a forker clone elsewhere
# without editing this file.
BORING_HOME = os.environ.get("BORING_HOME") or os.path.expanduser("~/oh-my-boring")
HOOK = os.path.join(BORING_HOME, "agents/claude-code/distill-session.py")


def _marked(session_id):
    # Done marker means fully handled; hermes pending markers mean "already queued" — don't backfill.
    # Retry markers are intentionally eligible for backfill (that's what backfill is for).
    return markers.is_done(session_id) or markers.is_pending(session_id, ttl=PENDING_TTL)


def transcript_cwd(tp):
    """The real working dir from the transcript (Claude Code records `cwd` per line).
    Returns '' if none found — better empty than the mangled project-dir name."""
    try:
        with open(tp, encoding="utf-8") as f:
            for _ in range(50):
                line = f.readline()
                if not line:
                    break
                try:
                    c = json.loads(line).get("cwd")
                except Exception:
                    continue
                if c:
                    return c
    except OSError:
        pass
    return ""


def _warm_llm():
    """Pre-load + pin the chat model so a per-run cold start (~70s after idle unload) doesn't make the
    first agent call exceed its timeout → silent SKIP. Best-effort: any failure is ignored (the agent's
    own call still works, just slower). Uses Ollama's native /api/generate keep_alive (no-op elsewhere)."""
    base = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    model = omb_env.llm_model()
    body = json.dumps({"model": model, "prompt": "ok", "stream": False, "keep_alive": 1800}).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"{base}/api/generate", data=body,
                                   headers={"Content-Type": "application/json"}),
            timeout=120,
        ).read()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="Backfill past Claude Code sessions into ohmyboring.")
    ap.add_argument(
        "--now",
        action="store_true",
        help="distill the MOST RECENT session immediately, ignoring done-markers and WITHOUT marking "
        "it done — so it is re-distillable on demand and the normal SessionEnd capture still runs.",
    )
    args = ap.parse_args()
    run_id = event_log.new_run_id("claude-collector")

    cutoff = time.time() - WINDOW_H * 3600
    paths = []
    for d in boring_config.source_dirs(adapter="session-end") or [os.path.expanduser("~/.claude/projects")]:
        paths.extend(glob.glob(os.path.join(d, "*", "*.jsonl")))  # top-level only
    # within window + big enough; backfill also skips already-done (marker), --now ignores the marker.
    todo = [
        p
        for p in paths
        if os.path.getmtime(p) >= cutoff
        and os.path.getsize(p) >= MIN_KB * 1024
        and (args.now or not _marked(os.path.splitext(os.path.basename(p))[0]))
    ]
    todo.sort(key=os.path.getmtime, reverse=True)
    # --now is an on-demand single-shot on the current (newest) session, not a batch drain.
    batch = todo[:1] if args.now else todo[:LIMIT]
    label = "distill-now" if args.now else "collect"
    print(f"[{label}] pending={len(todo)} this_batch={len(batch)} (LIMIT={1 if args.now else LIMIT})", flush=True)
    if not batch:
        print(f"[{label}] nothing to do", flush=True)
        event_log.try_append_event(
            "claude-collector",
            "collector_run",
            "ok",
            run_id=run_id,
            agent="claude-code",
            pending=len(todo),
            batch=0,
            processed=0,
            failed=0,
            remaining=len(todo),
            mode=label,
            **workflow_contract.collector_run_fields("ok", 0),
        )
        return 0

    _warm_llm()  # pre-warm gemma so the first session isn't a ~70s cold start (→ agent timeout → SKIP)

    env = dict(os.environ)
    if args.now:
        env["BORING_DISTILL_NO_MARK"] = "1"  # leave the session un-marked → re-distillable + SessionEnd still fires
    done = 0
    failed = 0
    timed_out = 0
    for tp in batch:
        proj = os.path.basename(os.path.dirname(tp))  # encoded dir name — for the log label only
        sid = os.path.splitext(os.path.basename(tp))[0]
        # Real cwd from the transcript, not the mangled project-dir name — so backfilled notes
        # get a correct repo/<slug> + company-origin tag (matching live-hook notes).
        cwd = transcript_cwd(tp)
        payload = json.dumps(
            {"transcript_path": tp, "cwd": cwd, "session_id": sid, "hook_event_name": "SessionEnd"}
        )
        try:
            r = subprocess.run(
                [sys.executable, HOOK], input=payload, text=True, env=env, timeout=180
            )
            done += 1 if r.returncode == 0 else 0
            failed += 1 if r.returncode != 0 else 0
            print(f"[{label}] {'ok' if r.returncode == 0 else 'fail'}  {proj}", flush=True)
        except subprocess.TimeoutExpired:
            failed += 1
            timed_out += 1
            print(f"[{label}] timeout  {proj}", flush=True)

    sync_status = "ok"
    try:
        DrudgeClient().sync()
        print(f"[{label}] sync ok", flush=True)
    except Exception as e:
        sync_status = "failed"
        print(f"[{label}] sync failed: {e}", file=sys.stderr, flush=True)
    print(f"[{label}] done={done}/{len(batch)}  remaining={len(todo) - done}", flush=True)
    status = "ok" if done == len(batch) and sync_status == "ok" else "failed"
    event_log.try_append_event(
        "claude-collector",
        "collector_run",
        status,
        run_id=run_id,
        agent="claude-code",
        pending=len(todo),
        batch=len(batch),
        processed=done,
        failed=failed,
        timed_out=timed_out,
        remaining=len(todo) - done,
        sync_status=sync_status,
        mode=label,
        **workflow_contract.collector_run_fields(status, len(batch)),
    )
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
