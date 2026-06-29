#!/usr/bin/env python3
"""Lazy backfill collector for GitHub Codex sessions.

Codex has no SessionEnd hook we can install, so this collector scans the local
Codex session directory (`~/.codex/sessions`) and distills a small batch of
un-ingested sessions per run. It shares marker state with the rest of the
oh-my-boring ingestion pipeline via `~/.cache/boring-distill`.

- Marker: ~/.cache/boring-distill/codex-<sid>.ts (done) / .pending / .retry
- LIMIT (default 1, COLLECT_LIMIT): number processed per invocation.
- WINDOW (default 720h=30d, COLLECT_WINDOW_HOURS): ignore anything too old.
- Subagent/rollout sessions (guardian, etc.) are skipped by default; set
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
PENDING_TTL = float(os.environ.get("COLLECT_PENDING_TTL") or os.environ.get("INGEST_PENDING_TTL") or "1800")
BORING_HOME = os.environ.get("BORING_HOME") or omb_env.omb_home()
HOOK = os.path.join(BORING_HOME, "agents/codex/distill-session.py")
INCLUDE_SUBAGENTS = os.environ.get("CODEX_INCLUDE_SUBAGENTS", "").lower() in ("1", "true", "yes")

if omb_env._in_container():
    markers.set_mark_dir("/host/.cache/boring-distill")


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
    return markers.is_done(prefixed) or markers.is_pending(prefixed, ttl=PENDING_TTL)


def _scan_sessions(source_dir: str, cutoff: float) -> dict:
    paths = glob.glob(os.path.join(source_dir, "**", "*.jsonl"), recursive=True)
    scan = {
        "total": len(paths),
        "too_old": 0,
        "too_small": 0,
        "rollout": 0,
        "already_marked": 0,
        "subagent": 0,
        "todo": [],
    }
    for p in paths:
        if os.path.getmtime(p) < cutoff:
            scan["too_old"] += 1
            continue
        if os.path.getsize(p) < MIN_KB * 1024:
            scan["too_small"] += 1
            continue
        sid = _codex_session_id(p)
        if not INCLUDE_SUBAGENTS and _is_rollout_session(sid):
            scan["rollout"] += 1
            continue
        if not INCLUDE_SUBAGENTS and _is_subagent(p):
            scan["subagent"] += 1
            continue
        if _marked(sid):
            scan["already_marked"] += 1
            continue
        scan["todo"].append(p)
    scan["todo"].sort(key=os.path.getmtime, reverse=True)
    return scan


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


def _is_rollout_session(session_id: str) -> bool:
    return session_id.startswith("rollout-")


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


def _format_mtime(path: str) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(os.path.getmtime(path)))


def _newest(paths: list[str]) -> str:
    if not paths:
        return ""
    return max(paths, key=os.path.getmtime)


def _is_rollout_marker(path: str) -> bool:
    return os.path.basename(path).startswith("codex-rollout-")


def _marker_status() -> dict:
    status = {}
    for label, suffix in (("done", "ts"), ("pending", "pending"), ("retry", "retry")):
        paths = [
            p
            for p in glob.glob(os.path.join(markers.MARK_DIR, f"codex-*.{suffix}"))
            if not _is_rollout_marker(p)
        ]
        newest = _newest(paths)
        status[label] = {
            "count": len(paths),
            "newest": newest,
            "newest_mtime": _format_mtime(newest) if newest else "",
        }
    return status


def _vault_wiki_dir() -> str:
    vault = os.environ.get("BORING_VAULT_DIR") or os.path.join(BORING_HOME, "vault")
    return os.path.join(vault, "wiki")


def _frontmatter_session_id(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---\n")
    if end < 0:
        return ""
    for line in text[4:end].splitlines():
        if line.startswith("omb_session_id:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return ""


def _newest_codex_note() -> dict:
    wiki_dir = _vault_wiki_dir()
    if not os.path.isdir(wiki_dir):
        return {}
    latest_path = ""
    latest_sid = ""
    for p in glob.glob(os.path.join(wiki_dir, "wiki-*.md")):
        sid = _frontmatter_session_id(p)
        if not sid.startswith("codex-") or sid.startswith("codex-rollout-"):
            continue
        if not latest_path or os.path.getmtime(p) > os.path.getmtime(latest_path):
            latest_path = p
            latest_sid = sid
    if not latest_path:
        return {}
    return {
        "path": latest_path,
        "session_id": latest_sid,
        "mtime": _format_mtime(latest_path),
    }


def _hermes_worker_status(path: str | None = None) -> dict:
    jobs_path = path or _hermes_jobs_path()
    if not os.path.exists(jobs_path):
        return {"path": jobs_path, "found": False}
    with open(jobs_path, encoding="utf-8") as f:
        data = json.load(f)
    for job in data.get("jobs", []):
        if job.get("name") == "codex-memory-ingest-worker":
            return {
                "path": jobs_path,
                "found": True,
                "enabled": bool(job.get("enabled", True)),
                "state": job.get("state") or "",
                "last_status": job.get("last_status") or "",
                "last_error": job.get("last_error") or "",
                "last_run_at": job.get("last_run_at") or "",
                "next_run_at": job.get("next_run_at") or "",
                "script": job.get("script") or "",
            }
    return {"path": jobs_path, "found": False}


def _hermes_jobs_path() -> str:
    if omb_env._in_container():
        return "/opt/data/cron/jobs.json"
    return os.path.expanduser("~/.hermes/cron/jobs.json")


def _print_status(source_dir: str, scan: dict) -> None:
    todo = scan["todo"]
    marker = _marker_status()
    worker = _hermes_worker_status()
    latest_note = _newest_codex_note()

    print(f"[codex-status] source_dir={source_dir}")
    print(f"[codex-status] marker_dir={markers.MARK_DIR}")
    print(
        "[codex-status] config "
        f"window_h={WINDOW_H:g} min_kb={MIN_KB:g} limit={LIMIT} "
        f"include_subagents={str(INCLUDE_SUBAGENTS).lower()}"
    )
    print(
        "[codex-status] sessions "
        f"total={scan['total']} queue_pending={len(todo)} "
        f"skipped_old={scan['too_old']} skipped_small={scan['too_small']} "
        f"skipped_rollout={scan['rollout']} "
        f"skipped_marked={scan['already_marked']} skipped_subagent={scan['subagent']}"
    )
    if todo:
        next_path = todo[0]
        print(
            "[codex-status] next_session "
            f"id={_codex_session_id(next_path)} mtime={_format_mtime(next_path)} "
            f"size_kb={os.path.getsize(next_path) / 1024:.1f} path={next_path}"
        )
    else:
        print("[codex-status] next_session none")
    print(
        "[codex-status] markers "
        f"done={marker['done']['count']} pending={marker['pending']['count']} retry={marker['retry']['count']}"
    )
    for label in ("done", "pending", "retry"):
        if marker[label]["newest"]:
            print(
                f"[codex-status] newest_{label} "
                f"mtime={marker[label]['newest_mtime']} path={marker[label]['newest']}"
            )
    print(
        "[codex-status] worker "
        f"found={str(worker.get('found', False)).lower()} "
        f"enabled={str(worker.get('enabled', False)).lower()} "
        f"state={worker.get('state', '')} last_status={worker.get('last_status', '')} "
        f"last_error={worker.get('last_error', '')} "
        f"last_run_at={worker.get('last_run_at', '')} next_run_at={worker.get('next_run_at', '')} "
        f"script={worker.get('script', '')} path={worker.get('path', '')}"
    )
    if latest_note:
        print(
            "[codex-status] newest_note "
            f"session_id={latest_note['session_id']} mtime={latest_note['mtime']} "
            f"path={latest_note['path']}"
        )
    else:
        print("[codex-status] newest_note none")


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description="Backfill past Codex sessions into ohmyboring.")
    ap.add_argument(
        "--now",
        action="store_true",
        help="distill the MOST RECENT session immediately, ignoring done-markers and WITHOUT marking "
        "it done — so it is re-distillable on demand.",
    )
    ap.add_argument(
        "--status",
        action="store_true",
        help="show Codex session queue, marker, and worker status without distilling or syncing",
    )
    args = ap.parse_args(argv)

    cutoff = time.time() - WINDOW_H * 3600
    source_dir = _source_dir()
    if not os.path.isdir(source_dir):
        label = "codex-status" if args.status else "codex-collect"
        print(f"[{label}] source dir not found: {source_dir}", file=sys.stderr)
        if args.status:
            _print_status(
                source_dir,
                {
                    "total": 0,
                    "too_old": 0,
                    "too_small": 0,
                    "rollout": 0,
                    "already_marked": 0,
                    "subagent": 0,
                    "todo": [],
                },
            )
        return 0

    if args.now:
        todo = glob.glob(os.path.join(source_dir, "**", "*.jsonl"), recursive=True)
        todo = [
            p
            for p in todo
            if os.path.getmtime(p) >= cutoff
            and os.path.getsize(p) >= MIN_KB * 1024
            and (INCLUDE_SUBAGENTS or not _is_rollout_session(_codex_session_id(p)))
            and (INCLUDE_SUBAGENTS or not _is_subagent(p))
        ]
        todo.sort(key=os.path.getmtime, reverse=True)
    else:
        scan = _scan_sessions(source_dir, cutoff)
        if args.status:
            _print_status(source_dir, scan)
            return 0
        todo = scan["todo"]

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
