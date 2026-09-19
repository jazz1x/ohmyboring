#!/usr/bin/env python3
"""Lazy backfill collector for GitHub Codex sessions.

Codex has no SessionEnd hook we can install, so this collector scans the local
Codex session directory (`~/.codex/sessions`) and distills a small batch of
un-ingested sessions per run. It shares marker state with the rest of the
oh-my-boring ingestion pipeline via `~/.cache/boring-distill`.

- Marker: ~/.cache/boring-distill/codex-<sid>.ts (done) / .pending / .retry / .dead
- LIMIT (default 1, COLLECT_LIMIT): number processed per invocation.
- WINDOW (default 720h=30d, COLLECT_WINDOW_HOURS): ignore anything too old.
- Subagent/rollout sessions (guardian, etc.) are skipped by default; set
  CODEX_INCLUDE_SUBAGENTS=1 to ingest them too.

Canonical scheduler decision (2026-08-06): this script is invoked by TWO independent
schedulers — the host launchd job `com.ohmyboring.codex-ingest` (20 min interval, runs
directly against ~/.codex/sessions, no extra runtime dependency) and the hermes cron job
`codex-memory-ingest-worker` (4h interval, runs inside the boring-agent container via a
bind mount, env supplied only through the wrapper's `setdefault` — not visible in
jobs.json). launchd is canonical: it has one fewer moving part (no container needs to be
up), its config is self-documenting (env is inline in the plist), and its tighter
interval keeps the ingestion queue drained instead of backlogged for hours. hermes should
stay disabled permanently, not run as a redundant hot-standby — both point at the same
host filesystem on the same machine, so there is no independent-uptime benefit to
running both, only duplicate-run risk. `--status` reports it as an issue if both are ever
active at once, or if neither is; `acquire_collector_lock` below is the runtime backstop
for the same failure mode.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import platform
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import boring_config
import event_log
import markers
import omb_env
import transcript
import workflow_contract
from drudge_client import DrudgeClient, DrudgeNotWritableError, check_drudge_writable
from vault_note import frontmatter_text

BORING_URL = omb_env.drudge_url()
WINDOW_H = float(os.environ.get("COLLECT_WINDOW_HOURS") or "720")
LIMIT = int(os.environ.get("COLLECT_LIMIT") or "1")
MIN_KB = float(os.environ.get("COLLECT_MIN_KB") or "20")
DISTILL_CLAMP = transcript.codex_distill_clamp()
STABLE_AGE_S = float(os.environ.get("COLLECT_STABLE_AGE_SECONDS") or "1800")
PENDING_TTL = float(os.environ.get("COLLECT_PENDING_TTL") or os.environ.get("INGEST_PENDING_TTL") or "1800")
RETRY_TTL = float(os.environ.get("COLLECT_RETRY_TTL") or os.environ.get("INGEST_RETRY_TTL") or str(PENDING_TTL))
BORING_HOME = os.environ.get("BORING_HOME") or omb_env.omb_home()
HOOK = os.path.join(BORING_HOME, "agents/codex/distill-session.py")
HOST_WORKER_LABEL = "com.ohmyboring.codex-ingest"

# Two schedulers (launchd on the host, hermes cron inside boring-agent) can both point at
# this script (see module docstring decision note below). A run-lock is the runtime
# backstop for whenever ownership drifts back to "both enabled": it does not replace
# picking one owner, it just stops a second concurrent run from burning an LLM pass on a
# session the first run is already mid-distill on. TTL-based, not pid-liveness-based,
# because launchd (host pid namespace) and hermes cron (container pid namespace) do not
# share a pid space — a raw `kill(pid, 0)` across that boundary would be meaningless.
# Sized comfortably above a normal single-session run (LLM call + sync, observed well
# under 2 minutes) and comfortably below the tightest scheduler interval (launchd's 20
# minutes), so a crashed holder is reclaimed before the next legitimate run would starve.
COLLECTOR_LOCK_TTL_S = float(os.environ.get("CODEX_COLLECTOR_LOCK_TTL") or "900")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes")


INCLUDE_SUBAGENTS = _env_bool("CODEX_INCLUDE_SUBAGENTS")
INCLUDE_ROLLOUTS = _env_bool("CODEX_INCLUDE_ROLLOUTS", default=True) or INCLUDE_SUBAGENTS

if omb_env._in_container():
    markers.set_mark_dir("/host/.cache/boring-distill")


def _source_dir():
    """Resolve the Codex sessions directory, including inside the hermes container."""
    if omb_env._in_container():
        return "/host/.codex/sessions"
    return os.path.expanduser("~/.codex/sessions")


def _lock_path() -> str:
    # Computed lazily (not a module-level constant) so the container override of
    # markers.MARK_DIR above is always reflected.
    return os.path.join(markers.MARK_DIR, "codex-collector.lock")


def _lock_owner_label() -> str:
    return "container" if omb_env._in_container() else "host"


def _pid_alive(pid: int) -> bool:
    """Best-effort same-pid-namespace liveness check.

    A False here does NOT prove the remote holder is dead when the checking process and
    the lock holder are in different pid namespaces (host vs. container) — it is a
    fast-path only; COLLECTOR_LOCK_TTL_S above is the authoritative reclaim signal.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except OSError:
        return False
    return True


def _read_lock() -> dict | None:
    try:
        with open(_lock_path(), encoding="utf-8") as f:
            parts = f.read().strip().split("\n")
    except OSError:
        return None
    try:
        return {
            "pid": int(parts[0]),
            "host": parts[1] if len(parts) > 1 else "",
            "started": float(parts[2]) if len(parts) > 2 else os.path.getmtime(_lock_path()),
            "owner": parts[3] if len(parts) > 3 else "",
        }
    except (ValueError, IndexError):
        return None


def acquire_collector_lock() -> tuple[bool, str]:
    """Best-effort single-run lock for the batch-processing section of a collector run.

    Returns (acquired, reason). `reason` explains a False result (never silent — the
    whole point is that a duplicate run leaves evidence instead of just doing nothing).
    A held lock is reclaimed once it is older than COLLECTOR_LOCK_TTL_S, or immediately
    if its pid is confirmed dead in our own pid namespace.
    """
    os.makedirs(markers.MARK_DIR, exist_ok=True)
    path = _lock_path()
    now = time.time()
    existing = _read_lock()
    if existing is not None:
        age = now - existing["started"]
        stale = age > COLLECTOR_LOCK_TTL_S
        if not stale and existing["owner"] == _lock_owner_label() and not _pid_alive(existing["pid"]):
            stale = True
        if not stale:
            return False, (
                f"locked by pid={existing['pid']} host={existing['host']} "
                f"owner={existing['owner']} age={age:.0f}s (ttl={COLLECTOR_LOCK_TTL_S:g}s)"
            )
        print(
            f"[codex-collect] reclaiming stale collector lock "
            f"(age={age:.0f}s pid={existing['pid']} owner={existing['owner']})",
            file=sys.stderr,
        )
        try:
            os.unlink(path)
        except OSError:
            pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Lost a race against another instance's acquire between our staleness check and
        # our create — the other instance legitimately holds it now.
        return False, "lost race acquiring lock (another instance just took it)"
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"{os.getpid()}\n{platform.node()}\n{now}\n{_lock_owner_label()}\n")
    return True, ""


def release_collector_lock() -> None:
    """Release the lock, but only if it is still ours (it may have been reclaimed as
    stale by another instance while we were still running — don't delete their lock)."""
    existing = _read_lock()
    if existing and existing["pid"] == os.getpid() and existing["owner"] == _lock_owner_label():
        try:
            os.unlink(_lock_path())
        except OSError:
            pass


def _codex_session_id(path: str) -> str:
    """Stable session id from the transcript filename (UUID suffix)."""
    return os.path.splitext(os.path.basename(path))[0]


def _marked(session_id: str) -> bool:
    prefixed = f"codex-{session_id}"
    return (
        markers.is_done(prefixed)
        or markers.is_pending(prefixed, ttl=PENDING_TTL)
        or markers.is_dead(prefixed)
    )


def _scan_sessions(source_dir: str, cutoff: float) -> dict:
    paths = glob.glob(os.path.join(source_dir, "**", "*.jsonl"), recursive=True)
    now = time.time()
    scan = {
        "total": len(paths),
        "too_old": 0,
        "too_new": 0,
        "too_small": 0,
        "rollout": 0,
        "already_marked": 0,
        "subagent": 0,
        "todo": [],
    }
    for p in paths:
        mtime = os.path.getmtime(p)
        if mtime < cutoff:
            scan["too_old"] += 1
            continue
        if STABLE_AGE_S > 0 and mtime > now - STABLE_AGE_S:
            scan["too_new"] += 1
            continue
        if os.path.getsize(p) < MIN_KB * 1024:
            scan["too_small"] += 1
            continue
        sid = _codex_session_id(p)
        if not INCLUDE_ROLLOUTS and _is_rollout_session(sid):
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


def _ignore_marker(label: str, path: str) -> bool:
    return label == "done" and _is_rollout_marker(path)


def _marker_status() -> dict:
    status = {}
    now = time.time()
    for label, suffixes, ttl in (
        ("done", ("ts",), None),
        ("pending", ("pending",), PENDING_TTL),
        ("retry", ("retry",), RETRY_TTL),
        ("dead_letter", ("dead", "dead-letter"), 0),
    ):
        paths = [
            p
            for suffix in suffixes
            for p in glob.glob(os.path.join(markers.MARK_DIR, f"codex-*.{suffix}"))
            if not _ignore_marker(label, p)
        ]
        newest = _newest(paths)
        stale_count = 0
        oldest_age_s = 0
        for p in paths:
            age_s = max(0, int(now - os.path.getmtime(p)))
            oldest_age_s = max(oldest_age_s, age_s)
            if ttl is not None and ttl >= 0 and age_s > ttl:
                stale_count += 1
        status[label] = {
            "count": len(paths),
            "newest": newest,
            "newest_mtime": _format_mtime(newest) if newest else "",
            "oldest_age_s": oldest_age_s,
            "stale_count": stale_count,
        }
    return status


def _vault_wiki_dir() -> str:
    vault = os.environ.get("BORING_VAULT_DIR") or os.path.join(BORING_HOME, "vault")
    return os.path.join(vault, "wiki")


def _frontmatter_session_id(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for line in frontmatter_text(text).splitlines():
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
        if not sid.startswith("codex-"):
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


def _parse_worker_time(raw: str) -> dt.datetime | None:
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _worker_readiness_issues(worker: dict) -> list[str]:
    """Hermes-specific health checks (schedule staleness, last run outcome).

    Only meaningful while hermes is actually the active scheduler: see the canonical-owner
    decision above collect-sessions.py's docstring — launchd is the intended owner, so
    hermes staying disabled (or entirely absent from jobs.json) is the correct steady
    state, not an issue. `_scheduler_ownership_issues` is what flags an actual ownership
    problem (both active, or neither active).
    """
    if not worker.get("enabled"):
        return []
    issues = []
    last_status = str(worker.get("last_status") or "").lower()
    if last_status not in ("ok", "success"):
        issues.append(f"hermes codex worker last_status={last_status or 'missing'}")
    if worker.get("last_error"):
        issues.append("hermes codex worker last_error set")
    next_run = _parse_worker_time(str(worker.get("next_run_at") or ""))
    if next_run is None:
        issues.append("hermes codex worker next_run_at missing_or_invalid")
    else:
        now = dt.datetime.now(next_run.tzinfo) if next_run.tzinfo else dt.datetime.now()
        if next_run < now:
            issues.append("hermes codex worker schedule stale")
    return issues


def _scheduler_ownership_issues(host_worker: dict, hermes_worker: dict) -> list[str]:
    """Exactly one of {launchd host worker, hermes cron worker} may own codex ingestion.

    Both active is the actual incident this guards against (a scheduler got toggled
    without noticing the other was still running, so the LLM kept firing every 4h from
    hermes while someone believed it was off). Neither active means ingestion has
    silently stopped. launchd is the canonical owner (see the module-level decision
    note); hermes being disabled or even absent from jobs.json is therefore healthy,
    not a problem — `_worker_readiness_issues` no longer flags it as one.
    """
    host_active = bool(host_worker.get("found") and host_worker.get("loaded"))
    hermes_active = bool(hermes_worker.get("found") and hermes_worker.get("enabled"))
    if host_active and hermes_active:
        return [
            "dual codex scheduler active: launchd host worker AND hermes cron worker "
            "are both enabled — pick one owner (launchd is canonical; disable the hermes job)"
        ]
    if not host_active and hermes_active:
        return [
            "non-canonical codex scheduler owns ingestion: hermes cron is active but "
            "launchd (the canonical owner) is not loaded"
        ]
    if not host_active and not hermes_active:
        return ["no codex ingestion scheduler active: neither launchd nor hermes cron is enabled"]
    return []


def _marker_readiness_issues(marker: dict) -> list[str]:
    issues = []
    if marker["pending"]["stale_count"] > 0:
        issues.append(f"stale codex pending markers={marker['pending']['stale_count']}")
    if marker["retry"]["stale_count"] > 0:
        issues.append(f"stale codex retry markers={marker['retry']['stale_count']}")
    if marker["dead_letter"]["count"] > 0:
        issues.append(f"codex dead-letter markers={marker['dead_letter']['count']}")
    return issues


def _hermes_jobs_path() -> str:
    if omb_env._in_container():
        return "/opt/data/cron/jobs.json"
    return os.path.expanduser("~/.hermes/cron/jobs.json")


def _host_worker_status() -> dict:
    system = platform.system()
    if system == "Darwin":
        path = os.path.expanduser(f"~/Library/LaunchAgents/{HOST_WORKER_LABEL}.plist")
        loaded = (
            subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{HOST_WORKER_LABEL}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        return {
            "kind": "launchd",
            "found": os.path.exists(path),
            "loaded": loaded,
            "path": path,
        }
    if system == "Linux":
        crontab = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        text = crontab.stdout if crontab.returncode == 0 else ""
        return {
            "kind": "cron",
            "found": HOST_WORKER_LABEL in text,
            "loaded": HOST_WORKER_LABEL in text,
            "path": "user crontab",
        }
    return {"kind": system or "unknown", "found": False, "loaded": False, "path": ""}


def _print_status(source_dir: str, scan: dict) -> bool:
    todo = scan["todo"]
    marker = _marker_status()
    worker = _hermes_worker_status()
    host_worker = _host_worker_status()
    latest_note = _newest_codex_note()

    print(f"[codex-status] source_dir={source_dir}")
    print(f"[codex-status] marker_dir={markers.MARK_DIR}")
    print(
        "[codex-status] config "
        f"window_h={WINDOW_H:g} min_kb={MIN_KB:g} limit={LIMIT} "
        f"distill_clamp={DISTILL_CLAMP} "
        f"stable_age_s={STABLE_AGE_S:g} include_rollouts={str(INCLUDE_ROLLOUTS).lower()} "
        f"include_subagents={str(INCLUDE_SUBAGENTS).lower()}"
    )
    print(
        "[codex-status] sessions "
        f"total={scan['total']} queue_pending={len(todo)} "
        f"skipped_old={scan['too_old']} skipped_new={scan['too_new']} skipped_small={scan['too_small']} "
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
        f"done={marker['done']['count']} pending={marker['pending']['count']} "
        f"retry={marker['retry']['count']} dead_letter={marker['dead_letter']['count']} "
        f"stale_pending={marker['pending']['stale_count']} stale_retry={marker['retry']['stale_count']}"
    )
    for label in ("done", "pending", "retry", "dead_letter"):
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
    print(
        "[codex-status] host_worker "
        f"found={str(host_worker.get('found', False)).lower()} "
        f"loaded={str(host_worker.get('loaded', False)).lower()} "
        f"kind={host_worker.get('kind', '')} path={host_worker.get('path', '')}"
    )
    issues = []
    issues.extend(_scheduler_ownership_issues(host_worker, worker))
    issues.extend(_worker_readiness_issues(worker))
    issues.extend(_marker_readiness_issues(marker))
    for issue in issues:
        print(f"[codex-status] readiness_issue {issue}")
    if latest_note:
        print(
            "[codex-status] newest_note "
            f"session_id={latest_note['session_id']} mtime={latest_note['mtime']} "
            f"path={latest_note['path']}"
        )
    else:
        print("[codex-status] newest_note none")
    return not issues


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
        help="show Codex session queue, marker, and worker status without distilling",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="with --status, exit non-zero unless the host Codex ingestion worker is active",
    )
    args = ap.parse_args(argv)
    if args.strict and not args.status:
        ap.error("--strict requires --status")
    run_id = event_log.new_run_id("codex-collector")

    cutoff = time.time() - WINDOW_H * 3600
    source_dir = _source_dir()
    if not os.path.isdir(source_dir):
        label = "codex-status" if args.status else "codex-collect"
        print(f"[{label}] source dir not found: {source_dir}", file=sys.stderr)
        if args.status:
            ready = _print_status(
                source_dir,
                {
                    "total": 0,
                    "too_old": 0,
                    "too_new": 0,
                    "too_small": 0,
                    "rollout": 0,
                    "already_marked": 0,
                    "subagent": 0,
                    "todo": [],
                },
            )
            status = "ok"
            if args.strict and not ready:
                print("[codex-status] readiness failed: worker/marker state is not ready", file=sys.stderr)
                status = "failed"
            event_log.try_append_event(
                "codex-collector",
                "collector_status",
                status,
                run_id=run_id,
                agent="codex",
                source_present=False,
                ready=ready,
                strict=args.strict,
                total=0,
                queue_pending=0,
                skipped_new=0,
                marker_pending=0,
                marker_retry=0,
                marker_dead_letter=0,
                **workflow_contract.readiness_fields(ready),
            )
            if status == "failed":
                return 1
        else:
            event_log.try_append_event(
                "codex-collector",
                "collector_run",
                "ok",
                run_id=run_id,
                agent="codex",
                source_present=False,
                pending=0,
                batch=0,
                processed=0,
                failed=0,
                mode="collect",
                **workflow_contract.collector_run_fields("ok", 0),
            )
        return 0

    if args.now:
        todo = glob.glob(os.path.join(source_dir, "**", "*.jsonl"), recursive=True)
        todo = [
            p
            for p in todo
            if os.path.getmtime(p) >= cutoff
            and os.path.getsize(p) >= MIN_KB * 1024
            and (INCLUDE_ROLLOUTS or not _is_rollout_session(_codex_session_id(p)))
            and (INCLUDE_SUBAGENTS or not _is_subagent(p))
        ]
        todo.sort(key=os.path.getmtime, reverse=True)
    else:
        scan = _scan_sessions(source_dir, cutoff)
        if args.status:
            ready = _print_status(source_dir, scan)
            status = "ok" if ready or not args.strict else "failed"
            marker = _marker_status()
            event_log.try_append_event(
                "codex-collector",
                "collector_status",
                status,
                run_id=run_id,
                agent="codex",
                source_present=True,
                ready=ready,
                strict=args.strict,
                total=scan["total"],
                queue_pending=len(scan["todo"]),
                skipped_old=scan["too_old"],
                skipped_new=scan["too_new"],
                skipped_small=scan["too_small"],
                skipped_rollout=scan["rollout"],
                skipped_marked=scan["already_marked"],
                skipped_subagent=scan["subagent"],
                marker_done=marker["done"]["count"],
                marker_pending=marker["pending"]["count"],
                marker_retry=marker["retry"]["count"],
                marker_dead_letter=marker["dead_letter"]["count"],
                marker_stale_pending=marker["pending"]["stale_count"],
                marker_stale_retry=marker["retry"]["stale_count"],
                **workflow_contract.readiness_fields(ready),
            )
            if args.strict and not ready:
                print("[codex-status] readiness failed: worker/marker state is not ready", file=sys.stderr)
                return 1
            return 0
        todo = scan["todo"]

    batch = todo[:1] if args.now else todo[:LIMIT]
    label = "distill-now" if args.now else "collect"
    print(f"[{label}] pending={len(todo)} this_batch={len(batch)} (LIMIT={1 if args.now else LIMIT})", flush=True)
    if not batch:
        print(f"[{label}] nothing to do", flush=True)
        event_log.try_append_event(
            "codex-collector",
            "collector_run",
            "ok",
            run_id=run_id,
            agent="codex",
            pending=len(todo),
            batch=0,
            processed=0,
            failed=0,
            remaining=len(todo),
            mode=label,
            **workflow_contract.collector_run_fields("ok", 0),
        )
        return 0

    # Batch processing (LLM distill + DB write) is the section a second concurrent
    # scheduler instance must not duplicate. Acquire before the write-door check so a
    # locked-out instance never even touches Drudge.
    acquired, lock_reason = acquire_collector_lock()
    if not acquired:
        print(f"[{label}] skipped: {lock_reason}", file=sys.stderr, flush=True)
        event_log.try_append_event(
            "codex-collector",
            "collector_run",
            "ok",
            run_id=run_id,
            agent="codex",
            pending=len(todo),
            batch=len(batch),
            processed=0,
            failed=0,
            remaining=len(todo),
            mode=label,
            reason=lock_reason,
            locked_out=True,
            **workflow_contract.collector_run_fields("ok", 0),
        )
        return 0

    try:
        # Distilling costs a full LLM pass; remembering is what needs the DB. Check the write
        # door first so a degraded engine leaves the session pending instead of burning the model
        # on input that cannot be stored — that loop re-ran the same session every cycle.
        try:
            check_drudge_writable(DrudgeClient())
        except DrudgeNotWritableError as exc:
            print(f"[codex-collect] write door closed: {exc}", file=sys.stderr, flush=True)
            event_log.try_append_event(
                "codex-collector",
                "collector_run",
                "failed",
                run_id=run_id,
                agent="codex",
                pending=len(todo),
                batch=len(batch),
                processed=0,
                failed=0,
                remaining=len(todo),
                mode=label,
                reason=str(exc),
                **workflow_contract.collector_run_fields("failed", len(batch)),
            )
            return 1

        env = dict(os.environ)
        if args.now:
            env["BORING_DISTILL_NO_MARK"] = "1"
        done = 0
        failed = 0
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
                    "distill_clamp": DISTILL_CLAMP,
                }
            )
            r = subprocess.run([sys.executable, HOOK], input=payload, text=True, env=env)
            done += 1 if r.returncode == 0 else 0
            failed += 1 if r.returncode != 0 else 0
            print(f"[{label}] {'ok' if r.returncode == 0 else 'fail'}  {sid}", flush=True)

        print(
            f"[{label}] done={done}/{len(batch)}  remaining={len(todo) - done}",
            flush=True,
        )
        status = "ok" if done == len(batch) else "failed"
        event_log.try_append_event(
            "codex-collector",
            "collector_run",
            status,
            run_id=run_id,
            agent="codex",
            pending=len(todo),
            batch=len(batch),
            processed=done,
            failed=failed,
            remaining=len(todo) - done,
            mode=label,
            **workflow_contract.collector_run_fields(status, len(batch)),
        )
        return 0 if status == "ok" else 1
    finally:
        release_collector_lock()


if __name__ == "__main__":
    sys.exit(main())
