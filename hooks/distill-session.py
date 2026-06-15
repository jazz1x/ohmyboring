#!/usr/bin/env python3
"""Claude Code SessionEnd/Stop hook — wake the agent to distill a session into memory.

Kernel A: distillation is *reasoning*, so it belongs to the agent, not the engine. This hook does
host-only glue: read the transcript, a cheap length pre-filter, throttle, and compute the
deterministic host facts (origin via boring.json, repo slug via git). It then wakes the
already-running hermes-agent, which distills the essence and stores a COMPLETE note via drudge's
`remember` MCP tool (drudge embeds + builds the graph deterministically — no LLM in the engine).

The engine no longer distills (the old /distill endpoint is gone). If the agent is down, the session
is left un-marked so the backfill collector retries it later. Never blocks session termination (exits 0).

Installation (persistence) is up to the user: add to hooks.SessionEnd in ~/.claude/settings.json:
  {"type":"command","command":"python3 ~/oh-my-boring/hooks/distill-session.py",
   "timeout":130,"async":true}
"""
import json
import os
import re
import subprocess
import sys
import time

import boring_config

# OMB_HOME: repo clone location (default ~/oh-my-boring) — forkers can clone elsewhere.
OMB_HOME = os.environ.get("OMB_HOME") or os.path.expanduser("~/oh-my-boring")
# Minimum interval (minutes) before re-distilling an in-progress session (Stop hook).
# SessionEnd (final) ignores the throttle.
THROTTLE_MIN = int(os.environ.get("DISTILL_THROTTLE_MIN") or "25")
MARK_DIR = os.path.expanduser("~/.cache/boring-distill")  # last distill time per session


def _mark_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id) or "nosession"
    return os.path.join(MARK_DIR, f"{safe}.ts")


def _throttled(session_id):
    """True (skip) if this session was already distilled within the last THROTTLE_MIN minutes. Cheap check."""
    if not session_id:
        return False
    try:
        age = time.time() - os.path.getmtime(_mark_path(session_id))
        return age < THROTTLE_MIN * 60
    except OSError:
        return False  # no marker = first distillation


def _mark(session_id):
    if not session_id:
        return
    try:
        os.makedirs(MARK_DIR, exist_ok=True)
        with open(_mark_path(session_id), "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def extract(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            msg = obj.get("message") or {}
            role = msg.get("role") or obj.get("type") or ""
            if role not in ("user", "assistant"):
                continue
            c = msg.get("content")
            if isinstance(c, str):
                t = c
            elif isinstance(c, list):
                t = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            else:
                t = ""
            t = t.strip()
            if t:
                out.append(f"[{role}] {t}")
    return "\n".join(out)


def git_remote_url(cwd):
    """Return the git remote.origin.url of cwd, or ''."""
    if not cwd:
        return ""
    try:
        return subprocess.run(
            ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def repo_slug(cwd):
    """Category axis: the repo slug (`org/name`) from the git remote of cwd. Falls back to the folder
    name if there's no git/remote. Only the host sees git/cwd, so compute it here and hand it to the
    agent (which reads the transcript inside its container and can't see the original cwd's git)."""
    url = git_remote_url(cwd)
    if url:
        slug = re.sub(r"^.*[:/]([^/]+/[^/]+?)(?:\.git)?$", r"\1", url)
        if slug and slug != url:
            return slug
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or ""  # fallback: folder name
    return ""


def via_agent(transcript_path, origin, repo):
    """Wake the already-running hermes-agent to distill + store the session (the sole write door).
    The agent gates (KEEP/SKIP), curates the narrative, and extracts the semantic fields, then stores
    a COMPLETE note via drudge's `remember` MCP tool — which embeds + builds the graph deterministically.
    The transcript is read through the /host/.claude path mounted into the agent container (compose volume).
    Returns True on success; False if the transcript is unmappable or docker/agent is down (→ retry later)."""
    home = os.path.expanduser("~")
    claude_root = os.path.join(home, ".claude")
    if not transcript_path.startswith(claude_root):
        return False  # outside ~/.claude → can't map to container path → retry later
    container_path = "/host/.claude" + transcript_path[len(claude_root):]
    container = os.environ.get("DISTILL_AGENT_CONTAINER", "boring-agent")
    repo_hint = f" The repo is '{repo}'." if repo else ""
    prompt = (
        f"Read the session transcript at {container_path}. If it is worth remembering "
        "(real problem-solving, decisions, or durable facts), distill it into a problem-solving "
        "narrative and store ONE complete note via drudge's `remember` tool: set title, body "
        "(the narrative), and the semantic fields tags/tools/concepts, plus any durable facts as "
        f"claims (subject/predicate/value). Use origin='{origin}'.{repo_hint} "
        "If it is just chit-chat or config dumps, do nothing. remember ingests the note immediately — "
        "you do not need to call sync."
    )
    try:
        r = subprocess.run(
            ["docker", "exec", container, "hermes", "-z", prompt],
            capture_output=True, timeout=120,
        )
        return r.returncode == 0
    except Exception:
        return False  # no docker / timeout / agent down → retry later


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    tp = data.get("transcript_path") or ""
    if not tp or not os.path.exists(tp):
        return
    session_id = data.get("session_id") or ""
    # SessionEnd = final, once (ignores throttle). Stop = periodic in-progress ingest (THROTTLE_MIN interval).
    is_final = (data.get("hook_event_name") or "") == "SessionEnd"
    if not is_final and _throttled(session_id):
        return
    cwd = data.get("cwd") or ""
    remote_url = git_remote_url(cwd)
    origin, _rule = boring_config.classify(cwd, remote_url or None)
    text = extract(tp)
    if len(text) < 500:  # skip too-short sessions (cheap host-side pre-filter)
        return
    repo = repo_slug(cwd)  # category axis — git remote slug (fallback folder name)
    # The agent is the sole write door: it reasons (gate + curate + extract) and stores via remember.
    # On success mark the throttle; on failure leave it un-marked so the backfill collector retries.
    if via_agent(tp, origin, repo):
        _mark(session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never block the session
    sys.exit(0)
