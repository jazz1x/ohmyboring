#!/usr/bin/env python3
"""Lazy backfill collector — slowly ingests a small batch of past sessions per run.

The SessionEnd hook fires only on session *termination* → long/open/past sessions aren't
captured. This collector scans the top-level session .jsonl files under ~/.claude/projects
(excluding subagents/workflows) and distills LIMIT of **only the not-yet-done ones**
(no marker) per run. It doesn't run them all at once, so it doesn't burn CPU.

- Marker: ~/.cache/boring-distill/<sid>.ts, same as distill-session.py. If present, skip (already done).
- LIMIT (default 1, COLLECT_LIMIT): number processed per invocation. Called periodically via launchd/cron → drains slowly.
- WINDOW (default 720h=30d, COLLECT_WINDOW_HOURS): ignore anything too old.
- Each session → distill-session.py (wakes the agent → remember). No trailing /sync: remember
  ingests each note live, and the engine's own 4h scheduler re-scans the vault regardless.
- cwd = the real working dir from the transcript → distill-session determines origin via boring.json.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "src"))
import markers
import transcript
import waste_kind

from ohmyboring import config as boring_config
from ohmyboring.adapters import events as event_log
from ohmyboring.adapters import workflow_contract
from ohmyboring.adapters.engine import DrudgeClient, check_drudge_writable
from ohmyboring.result import Err, Ok

match boring_config.collect():
    case Err(_reason):
        sys.exit(f"[collect] {_reason}")
    case Ok(_collect):
        pass
WINDOW_H = _collect.window_hours
LIMIT = int(os.environ.get("COLLECT_LIMIT") or "1")
MIN_KB = _collect.min_bytes / 1024  # skip small sessions (distill would SKIP anyway)
PENDING_TTL = _collect.pending_ttl_seconds
# BORING_HOME: repo clone location (default ~/oh-my-boring). Lets a forker clone elsewhere
# without editing this file.
BORING_HOME = os.environ.get("BORING_HOME") or os.path.expanduser("~/oh-my-boring")
HOOK = os.path.join(BORING_HOME, "agents/claude-code/distill-session.py")


def _marked(session_id):
    # Done marker means fully handled; hermes pending markers mean "already queued" — don't backfill.
    # A fresh retry marker means hermes still holds the session and will retry it; re-queuing would
    # overwrite the item for nothing. A dead-lettered session has exhausted its retries.
    return (
        markers.is_done(session_id)
        or markers.is_pending(session_id, ttl=PENDING_TTL)
        or markers.is_retry(session_id, ttl=PENDING_TTL)
        or markers.is_dead(session_id)
    )


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


def _line(tag: str, seat: str, sid: str, text: str) -> None:
    print(f"{tag}\t{seat or '이름 없음'}\t{sid}\t{','.join(waste_kind.waste_kinds(text))}\t{text}")


def print_owner_corrections(since_iso: str, until_iso: str) -> int:
    """C-WASTEVIEW1 S1·S2: one line per owner sentence with its waste kinds
    (tag · seat · session · kinds · text), then the counts as `#` lines. Register rows are not
    kinded: sessions wrote them, and the view must stand on the owner's words (38918)."""
    since, until = datetime.fromisoformat(since_iso), datetime.fromisoformat(until_iso)
    paths = []
    for d in boring_config.source_dirs(adapter="session-end") or [os.path.expanduser("~/.claude/projects")]:
        paths.extend(glob.glob(os.path.join(d, "*", "*.jsonl")))  # top-level only, as the collector
    sessions, total, hits, first_total, first_hits = 0, 0, 0, 0, 0
    kinds: dict[str, int] = {name: 0 for name, _ in waste_kind.WASTE_KINDS}
    seats: dict[str, list[int]] = {}
    for p in sorted(p for p in paths if os.path.getmtime(p) >= since.timestamp()):
        found = transcript.owner_sentences(p, since, until)
        sid, seat = os.path.basename(p)[:8], found.seat or "이름 없음"
        sessions += bool(found.owner)
        for s in found.owner:
            hit = bool(transcript.OWNER_CORRECTION.search(s))
            total, hits = total + 1, hits + hit
            seats.setdefault(seat, [0, 0])
            seats[seat][0] += hit
            seats[seat][1] += 1
            for name in waste_kind.waste_kinds(s) if hit else []:
                kinds[name] += 1
            _line("correction" if hit else "other", seat, sid, s)
        for s in found.first_prompts:
            hit = bool(transcript.OWNER_CORRECTION.search(s))
            first_total, first_hits = first_total + 1, first_hits + hit
            _line("first-correction" if hit else "first-other", seat, sid, s)
    print(
        f"# owner_corrections {hits} / owner_sentences {total} sessions {sessions} since {since_iso} until {until_iso}"
    )
    for name, n in kinds.items():
        print(f"# kind {name} {n} / {hits}")
    for seat, (c, n) in sorted(seats.items(), key=lambda kv: -kv[1][0]):
        print(f"# seat {seat} corrections {c} / sentences {n}")
    print(f"# first_prompt_corrections {first_hits} / first_prompt_sentences {first_total}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Backfill past Claude Code sessions into ohmyboring.")
    ap.add_argument(
        "--now",
        action="store_true",
        help="distill the MOST RECENT session immediately, ignoring done-markers and WITHOUT marking "
        "it done — so it is re-distillable on demand and the normal SessionEnd capture still runs.",
    )
    ap.add_argument(
        "--owner-corrections",
        nargs=2,
        metavar=("SINCE", "UNTIL"),
        help="read-only: print the owner's sentences between two ISO instants and which match the "
        "correction word rule; no distill, no event, no marker.",
    )
    args = ap.parse_args()
    if args.owner_corrections:
        return print_owner_corrections(*args.owner_corrections)
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
    print(
        f"[{label}] pending={len(todo)} this_batch={len(batch)} (LIMIT={1 if args.now else LIMIT})",
        flush=True,
    )
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

    # Distilling costs a full LLM pass; remembering is what needs the DB. Check the write
    # door first so a degraded engine leaves the session pending instead of burning the model
    # on input that cannot be stored — that loop re-ran the same session every cycle.
    match check_drudge_writable(DrudgeClient()):
        case Err(failure):
            print(f"[claude-collect] write door closed: {failure}", file=sys.stderr, flush=True)
            event_log.try_append_event(
                "claude-collector",
                "collector_run",
                "failed",
                run_id=run_id,
                agent="claude-code",
                pending=len(todo),
                batch=len(batch),
                processed=0,
                failed=0,
                remaining=len(todo),
                mode=label,
                reason=str(failure),
                **workflow_contract.collector_run_fields("failed", len(batch)),
            )
            return 1
        case Ok(_):
            pass

    env = dict(os.environ)
    if args.now:
        env["BORING_DISTILL_NO_MARK"] = "1"  # the hook only queues; `make distill-now` drains it in hermes
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
            {"transcript_path": tp, "cwd": cwd, "session_id": sid, "hook_event_name": "Backfill"}
        )
        try:
            r = subprocess.run([sys.executable, HOOK], input=payload, text=True, env=env, timeout=180)
            done += 1 if r.returncode == 0 else 0
            failed += 1 if r.returncode != 0 else 0
            print(f"[{label}] {'ok' if r.returncode == 0 else 'fail'}  {proj}", flush=True)
        except subprocess.TimeoutExpired:
            failed += 1
            timed_out += 1
            print(f"[{label}] timeout  {proj}", flush=True)

    print(f"[{label}] done={done}/{len(batch)}  remaining={len(todo) - done}", flush=True)
    status = "ok" if done == len(batch) else "failed"
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
        mode=label,
        **workflow_contract.collector_run_fields(status, len(batch)),
    )
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
