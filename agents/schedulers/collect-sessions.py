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
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

DRUDGE_URL = os.environ.get("DRUDGE_URL", "http://localhost:7700")
WINDOW_H = float(os.environ.get("COLLECT_WINDOW_HOURS") or "720")
LIMIT = int(os.environ.get("COLLECT_LIMIT") or "1")
MIN_KB = float(os.environ.get("COLLECT_MIN_KB") or "20")  # skip small sessions (distill would SKIP anyway)
# OMB_HOME: repo clone location (default ~/oh-my-boring). Lets a forker clone elsewhere
# without editing this file.
OMB_HOME = os.environ.get("OMB_HOME") or os.path.expanduser("~/oh-my-boring")
HOOK = os.path.join(OMB_HOME, "agents/claude-code/distill-session.py")
PROJECTS = os.path.expanduser("~/.claude/projects")
MARK_DIR = os.path.expanduser("~/.cache/boring-distill")


def _marked(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id) or "nosession"
    return os.path.exists(os.path.join(MARK_DIR, f"{safe}.ts"))


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
    model = os.environ.get("DRUDGE_LLM_MODEL", "gemma4:12b")
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
    cutoff = time.time() - WINDOW_H * 3600
    paths = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))  # top-level only
    # not-yet-done (no marker) + within window → newest first
    todo = [
        p
        for p in paths
        if os.path.getmtime(p) >= cutoff
        and os.path.getsize(p) >= MIN_KB * 1024
        and not _marked(os.path.splitext(os.path.basename(p))[0])
    ]
    todo.sort(key=os.path.getmtime, reverse=True)
    batch = todo[:LIMIT]
    print(f"[collect] pending={len(todo)} this_batch={len(batch)} (LIMIT={LIMIT})", flush=True)
    if not batch:
        print("[collect] all done — nothing to do", flush=True)
        return

    _warm_llm()  # pre-warm gemma so the first session isn't a ~70s cold start (→ agent timeout → SKIP)

    env = dict(os.environ)
    done = 0
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
            print(f"[collect] {'ok' if r.returncode == 0 else 'fail'}  {proj}", flush=True)
        except subprocess.TimeoutExpired:
            print(f"[collect] timeout  {proj}", flush=True)

    try:
        req = urllib.request.Request(f"{DRUDGE_URL}/sync", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=900) as resp:
            print("[collect] sync ok", flush=True)
    except Exception as e:
        print(f"[collect] sync failed (ignored): {e}", flush=True)
    print(f"[collect] done={done}/{len(batch)}  remaining={len(todo) - done}", flush=True)


if __name__ == "__main__":
    main()
    sys.exit(0)
