#!/usr/bin/env python3
"""Claude Code SessionEnd hook script — distills a session into a personal memory note.

This hook does *host-only* work: reading the transcript, extracting text, throttling,
and correcting the session mtime. LLM distillation, the KEEP/SKIP gate, secret scrubbing,
and raw-note formatting are handled by the drudge engine (/distill, SSOT) — this removes
the past duplication where this script reimplemented ollama.generate/redact (engine
ollama.rs is the SSOT for LLM calls). Only the extracted text is POSTed to the engine →
the engine writes to ~/oh-my-boring/vault/raw. On failure, short sessions, or engine
downtime it silently skips. It never blocks session termination (always exits 0).

Installation (persistence) is up to the user: add to hooks.SessionEnd in
~/.claude/settings.json:
  {"type":"command","command":"python3 ~/oh-my-boring/hooks/distill-session.py",
   "timeout":130,"async":true}
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

RAW_DIR = os.path.expanduser("~/oh-my-boring/vault/raw")
DRUDGE_URL = os.environ.get("DRUDGE_URL", "http://localhost:7700")
# Minimum interval (minutes) before re-distilling an in-progress session (Stop hook).
# SessionEnd (final) ignores the throttle.
THROTTLE_MIN = int(os.environ.get("DISTILL_THROTTLE_MIN") or "25")
MARK_DIR = os.path.expanduser("~/.cache/boring-distill")  # last distill time per session


def _trigger_sync():
    """Reflect the distilled note into the RAG immediately — call drudge /sync
    (compile→ingest→extract) detached. Does not block the hook (synchronously chaining
    distill+sync risks exceeding 130s). Engine downtime/failure is ignored — the 4h
    scheduler catches it (never blocks session termination).
    Skipped when DISTILL_NO_SYNC is set (so the backfill collector syncs only once at the end)."""
    if os.environ.get("DISTILL_NO_SYNC"):
        return
    try:
        subprocess.Popen(
            ["curl", "-sS", "-m", "600", "-X", "POST", f"{DRUDGE_URL}/sync"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # survives parent (hook) exit to finish ingestion
        )
    except Exception:
        pass


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


def _session_mtime(path):
    """Latest message timestamp in the transcript → epoch (float). The session's actual time.
    None if absent (caller keeps the file mtime as-is = distill time)."""
    latest = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    ts = json.loads(line).get("timestamp")
                except Exception:
                    continue
                if not ts:
                    continue
                try:
                    e = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    continue
                if latest is None or e > latest:
                    latest = e
    except OSError:
        return None
    return latest


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


def repo_slug(cwd):
    """Category axis: the repo slug (`org/name`) from the git remote of cwd. Falls back to
    the folder name if there's no git/remote. Only the host sees git/cwd, so compute it once
    here and pass it to the engine (the engine is in a container and can't see the original cwd's git)."""
    if not cwd:
        return ""
    try:
        url = subprocess.run(
            ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        url = ""
    if url:
        # git@host:org/name.git | https://host/org/name(.git) → org/name
        slug = re.sub(r"^.*[:/]([^/]+/[^/]+?)(?:\.git)?$", r"\1", url)
        if slug and slug != url:
            return slug
    return os.path.basename(cwd.rstrip("/")) or ""  # fallback: folder name


def post_distill(text, session_id, origin, phase, repo, cwd):
    """POST the extracted text to drudge /distill → the engine distills, scrubs, and writes
    the raw note (SSOT). Returns {"written": bool, "filename": str|None}, or None (engine
    down/error → no-op). The engine performs length clamping, the KEEP/SKIP gate, and secret scrubbing."""
    body = json.dumps(
        {"text": text, "session_id": session_id, "origin": origin,
         "phase": phase, "repo": repo, "cwd": cwd}
    ).encode()
    req = urllib.request.Request(
        f"{DRUDGE_URL}/distill", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except Exception:
        return None  # engine down/error — the 4h scheduler catches it (never blocks the session)


def via_agent(transcript_path):
    """Write front door (opt-in, DISTILL_VIA_AGENT): delegate 'judge and ingest' to the
    already-running hermes-agent. The agent performs the gate (KEEP/SKIP, curation) with its
    own reasoning and saves via the drudge MCP (remember/sync). The transcript is read through
    the /host/.claude path mounted into the agent container (compose volume).
    Returns True on success. If docker/agent is down or mapping fails, returns False → the caller falls back to the engine."""
    home = os.path.expanduser("~")
    claude_root = os.path.join(home, ".claude")
    if not transcript_path.startswith(claude_root):
        return False  # outside ~/.claude → can't map to container path → fall back
    container_path = "/host/.claude" + transcript_path[len(claude_root):]
    container = os.environ.get("DISTILL_AGENT_CONTAINER", "boring-agent")
    prompt = (
        f"세션 트랜스크립트 {container_path} 를 읽어라. 기억할 가치가 있으면(실제 문제해결·결정·사실) "
        "핵심을 '문제해결 서사'로 추려 drudge 의 remember 툴로 적재한 뒤 sync 툴을 호출해라. "
        "단순 잡담·설정덤프뿐이면 아무것도 하지 마라."
    )
    try:
        r = subprocess.run(
            ["docker", "exec", container, "hermes", "-z", prompt],
            capture_output=True, timeout=120,
        )
        return r.returncode == 0
    except Exception:
        return False  # no docker / timeout / agent down → fall back


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
    # If in-progress but already distilled recently, bail cheaply without even reading the transcript.
    is_final = (data.get("hook_event_name") or "") == "SessionEnd"
    if not is_final and _throttled(session_id):
        return
    # Session 'experiences' are not isolated — they all accumulate in the same raw/. origin is just a tag for recall toggling.
    #   Putting a cwd token in DISTILL_COMPANY_CWD (':'-separated) tags that session as origin=company.
    #   Default empty = the company concept is unused (everything is personal). (Not exclusion — just a tag.)
    cwd = data.get("cwd") or ""
    company_tokens = (os.environ.get("DISTILL_COMPANY_CWD") or "").split(":")
    is_company = any(tok and tok in cwd for tok in company_tokens)
    text = extract(tp)
    if len(text) < 500:  # skip too-short sessions (cheap host-side pre-filter — avoids wasted POSTs)
        return
    # Write front door (opt-in): if DISTILL_VIA_AGENT, the agent gates + ingests. Done on success, fall back to engine on failure.
    # (Two-door design: delegate the judgment of auto-capture to the agent's reasoning. The engine distill is always alive as a fallback.)
    if os.environ.get("DISTILL_VIA_AGENT") and via_agent(tp):
        _mark(session_id)
        return
    # No isolation — both personal and company session experiences go to the same raw/. Distinction is via the origin tag only.
    origin = "company" if is_company else "personal"
    phase = "종료" if is_final else "진행중"
    repo = repo_slug(cwd)  # category axis — git remote slug (fallback folder name)
    # The engine (SSOT) performs length clamping, LLM distillation, the KEEP/SKIP gate, secret scrubbing, and raw-note writing.
    resp = post_distill(text, session_id, origin, phase, repo, cwd)
    if resp is None:
        return  # engine unreachable → leave no marker so it's retried later
    if not resp.get("written"):
        # Engine was reached but its KEEP/SKIP gate rejected this session → terminal.
        # Mark it so the backfill collector doesn't re-pick the same SKIP'd session forever.
        _mark(session_id)
        return
    filename = resp.get("filename")
    if not filename:
        return
    fp = os.path.join(RAW_DIR, filename)  # the engine returns only the filename → join with host RAW_DIR
    # Recency sort-key correction: note mtime = the session's actual time (latest transcript timestamp).
    # Even if a backfill distills an old session now, mtime=session-time so the brief won't surface it as fake-recent.
    # (compile preserves this mtime into the wiki → ingest uses it as updated_at.)
    st = _session_mtime(tp)
    if st:
        try:
            os.utime(fp, (st, st))
        except OSError:
            pass
    _mark(session_id)  # refresh the throttle marker
    _trigger_sync()  # ingest the note immediately (detached) — don't wait for the 4h scheduler


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never block the session
    sys.exit(0)
