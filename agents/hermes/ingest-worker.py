#!/usr/bin/env python3
"""Ingest queue worker (the --script half of the self-augment cron).

Every distillation rides the one LangGraph distill engine — nothing prints a prompt for an
agent to remember by hand. The implicit offer population = session transcripts under the
configured source dirs MINUS the per-session markers (done/pending/retry/dead) and the shared
distill_queue.

Flow per cron tick:
  1. OFFER — scan the source-dir window for eligible sessions, extract + clamp each transcript
     to a size the engine can digest without derailing (~4k chars), and enqueue it into the
     shared distill_queue (the same queue the host SessionEnd hooks write, FIFO by mtime).
     The scan only tops the queue up to one tick's worth — at most
     max(0, QUEUE_PER_TICK − current queue length) — so no offered session waits past
     PENDING_TTL for a later tick's drain.
  2. DRAIN — pop up to QUEUE_PER_TICK queued items, oldest first, and run each through
     distill_run.distill_and_remember (verify → resolve → polish → remember, in the graph).
     Success → done marker + queue file removed; failure → retry marker (dead after repeated
     failures) and the file stays; engine/door/LLM unreachable → deferred without spending a try.
     The scan runs BEFORE the drain so a session offered this tick is processed this tick.

stdout stays empty on every path: the hermes job runs with no_agent, so stdout is never handed
to an agent. Progress is observable only in the event log (ingest_offer / ingest_queue) and
stderr.

This script shares the SessionEnd hook's marker directory (~/.cache/boring-distill) so hermes cron
and the engine-direct path do not duplicate sessions. The directory is bind-mounted into the
hermes-agent container at /host/.cache/boring-distill.
"""

import glob
import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "src"))
import distill_core
import distill_queue
import markers
import transcript

from ohmyboring import config as boring_config
from ohmyboring import config as omb_env
from ohmyboring.adapters import events as event_log
from ohmyboring.adapters import llm, workflow_contract
from ohmyboring.adapters.engine import DrudgeClient, check_drudge_writable
from ohmyboring.distill import run as distill_run
from ohmyboring.result import Err, Ok

# Runs in TWO contexts: inside the hermes-agent container (via `hermes cron --script`) or on the host
# (manual/launchd). Auto-detect by the container's bind mount so paths + the engine URL resolve in both.
_IN_CONTAINER = omb_env._in_container()


def _source_dirs():
    """Configured session source dirs, translated for the container filesystem when needed."""
    dirs = boring_config.source_dirs(adapter="session-end")
    if not dirs:
        # Graceful fallback to the Claude Code default so a fresh clone without config still works.
        dirs = [os.path.expanduser("~/.claude/projects")]
    if not _IN_CONTAINER:
        return dirs
    # Inside the hermes-agent container, host home is bind-mounted under /host, but config paths
    # expand to the container's own home (e.g. /root). Rewrite them to the /host mirror.
    home = os.path.expanduser("~")
    mapped = []
    for d in dirs:
        if d.startswith(home + "/"):
            mapped.append("/host" + d[len(home) :])
        elif d == home:
            mapped.append("/host")
        else:
            mapped.append(d)
    return mapped


def _agent_for_dir(source_dir):
    """Agent id for a session source dir, read from its path (claude projects → claude-code, …).

    A session file arrives with no agent label of its own; the dir it was found under is the
    label. Well-known tool roots map to their agent id; anything unrecognized falls back to
    claude-code, the historical source of this queue.
    """
    d = source_dir.replace("\\", "/").lower()
    for marker, agent in (
        (".claude/", "claude-code"),
        (".codex/", "codex"),
        (".kimi", "kimi"),
    ):
        if marker in d:
            return agent
    return "claude-code"


# Shared marker directory: host ~/.cache/boring-distill is mounted at /host/.cache/boring-distill
# inside the hermes-agent container so host SessionEnd hook markers are visible here too.
DISTILL_MARK_DIR = (
    "/host/.cache/boring-distill" if _IN_CONTAINER else os.path.expanduser("~/.cache/boring-distill")
)
if _IN_CONTAINER:
    markers.set_mark_dir(DISTILL_MARK_DIR)
MARK_DIR = DISTILL_MARK_DIR
BORING_URL = (
    omb_env.drudge_url()
)  # BORING_URL canonical, BORING_URL deprecated alias; container-aware default
BORING_HOME = os.environ.get("BORING_HOME") or omb_env.omb_home()
TRANSCRIPT_FORMAT = boring_config.agent_config("claude-code").get("format") or "claude-json"
WINDOW_H = float(os.environ.get("COLLECT_WINDOW_HOURS") or "720")
MIN_KB = float(os.environ.get("COLLECT_MIN_KB") or "20")
STABLE_AGE_S = float(os.environ.get("COLLECT_STABLE_AGE_SECONDS") or "1800")
CLAMP = int(os.environ.get("INGEST_CLAMP") or "4000")  # 12B digest ceiling — above this the agent derails
MIN_TEXT = 500  # below this = no real content → skip
# A pending-marker prevents the same session being re-offered every tick while the agent is still
# working on it (or just failed). It expires so a crashed tick doesn't pin a session forever.
PENDING_TTL = float(os.environ.get("INGEST_PENDING_TTL") or "1800")
# A retry-marker is a backoff signal, not a terminal state. Once it is stale, Hermes may re-offer it.
RETRY_TTL = float(os.environ.get("INGEST_RETRY_TTL") or str(PENDING_TTL))
QUEUE_PER_TICK = int(os.environ.get("BORING_QUEUE_PER_TICK") or "3")


def _repo_slug(cwd):
    """Category axis: canonical repo slug from git remote or cwd basename."""
    return distill_core.repo_slug(cwd)


def _log_worker_event(event, status, agent="claude-code", **fields):
    event_log.try_append_event(
        "hermes-ingest-worker",
        event,
        status,
        agent=agent,
        **workflow_contract.worker_fields(event, status),
        **fields,
    )


def _eligible(p):
    """A session is queue-eligible if: within window, big enough, finished writing, not yet
    done, not dead, not pending, not in fresh retry state, and not already handled by the
    engine-direct SessionEnd hook."""
    sid = os.path.splitext(os.path.basename(p))[0]
    if markers.is_done(sid) or markers.is_dead(sid) or distill_queue.is_queued(sid):
        return False
    if markers.is_pending(sid, ttl=PENDING_TTL):
        return False
    if markers.is_retry(sid, ttl=RETRY_TTL):
        return False
    if STABLE_AGE_S > 0 and os.path.getmtime(p) > time.time() - STABLE_AGE_S:
        return False
    return True


def extract(path):
    """Extract user/assistant text using the configured transcript format."""
    return transcript.extract(path, TRANSCRIPT_FORMAT)


def transcript_cwd(path):
    try:
        with open(path, encoding="utf-8") as f:
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


@dataclass(frozen=True)
class Drain:
    status: str  # ok | failed | dead | skipped | deferred
    reason: str = ""


#: Last-resort guard against *unbounded* input, not a quality knob — every agent path clamps with
#: its own `transcript.*_distill_clamp()` first, so reaching this is a caller bug. The number and
#: its rationale live in transcript.py with every other clamp; see `distill_backstop_clamp`.
BACKSTOP_CLAMP = transcript.distill_backstop_clamp()


def _within_backstop(text):
    if len(text) <= BACKSTOP_CLAMP:
        return text
    text, _ = transcript.clamp_text(text, BACKSTOP_CLAMP)
    print(
        f"[distill-session] caller passed unclamped text; cut to {len(text)} chars by the "
        f"{BACKSTOP_CLAMP}-char backstop. Raise the caller's own clamp, not this one.",
        file=sys.stderr,
    )
    return text


def _distill(item):
    return distill_run.distill_and_remember(
        _within_backstop(item.text), item.origin, item.repo, item.session_id
    )


def _attempt(item):
    try:
        ok = _distill(item)
    except Exception as e:  # noqa: BLE001 — model output can break the graph in any way; count it as a failed try
        return Drain("failed", f"{type(e).__name__}: {e}")
    return Drain("ok") if ok else Drain("failed", "queue distill failed")


def _drain_one(item):
    if markers.is_done(item.session_id):
        distill_queue.remove(item.session_id)
        return Drain("skipped", "already_done")
    attempt = _attempt(item)
    if attempt.status == "ok":
        markers.mark_done(item.session_id)
        distill_queue.remove(item.session_id)
        return attempt
    markers.mark_retry(item.session_id, reason=attempt.reason)
    if markers.is_dead(item.session_id):
        distill_queue.remove(item.session_id)
        return Drain("dead", attempt.reason)
    return attempt


def _llm_reachable():
    url = urlparse(llm.LLM_BASE_URL)
    port = url.port or (443 if url.scheme == "https" else 80)
    try:
        with socket.create_connection((url.hostname, port), timeout=5):
            return Ok(None)
    except OSError as e:
        return Err(f"llm {url.hostname}:{port} unreachable: {e}")


def _door_reachable():
    """문(:7710)이 떠 있는지 — remember 의 새 출구(E3a-1). 엔진·LLM 검사와 같은 Err 모양."""
    url = urlparse(boring_config.door_url())
    port = url.port or (443 if url.scheme == "https" else 80)
    try:
        with socket.create_connection((url.hostname, port), timeout=5):
            return Ok(None)
    except OSError as e:
        return Err(f"door {url.hostname}:{port} unreachable: {e}")


def _reachable():
    match check_drudge_writable(DrudgeClient(base_url=BORING_URL, timeout=15.0, retries=0)):
        case Err(failure):
            return Err(str(failure))
        case Ok(_):
            pass
    # 문이 낮은 동안 시도를 태우지 않는다 — remember 는 문을 지나니, 문이 답이 없으면 그 틱의
    # 증류는 미루고 큐 항목은 그대로 둔다(시도 소모 없음).
    match _door_reachable():
        case Err(reason):
            return Err(reason)
        case Ok(_):
            return _llm_reachable()


def _log_queue(status, reason="", **fields):
    event_log.try_append_event("hermes-ingest-worker", "ingest_queue", status, reason=reason, **fields)


def _drain_queue():
    """Distill up to QUEUE_PER_TICK hook-queued sessions, oldest first."""
    items = distill_queue.drain(QUEUE_PER_TICK)
    if not items:
        return
    match _reachable():
        case Err(reason):
            print(f"[ingest-worker] queue deferred: {reason}", file=sys.stderr)
            agents = ",".join(sorted({item.agent for item in items}))
            _log_queue("deferred", f"unreachable: {reason}", agent=agents, queued=len(items))
            return
        case Ok(_):
            pass
    for item in items:
        result = _drain_one(item)
        _log_queue(result.status, result.reason, agent=item.agent, session_id=item.session_id)


def main(argv=None):
    os.makedirs(MARK_DIR, exist_ok=True)
    if "--drain-only" in (sys.argv[1:] if argv is None else argv):
        _drain_queue()
        return

    # OFFER — eligible sessions in the window → the shared engine queue (FIFO by mtime).
    cutoff = time.time() - WINDOW_H * 3600
    candidates = []
    for d in _source_dirs():
        for p in glob.glob(os.path.join(d, "*", "*.jsonl")):
            if os.path.getmtime(p) >= cutoff and os.path.getsize(p) >= MIN_KB * 1024 and _eligible(p):
                candidates.append((os.path.getmtime(p), p, d))
    candidates.sort(key=lambda c: c[0])  # oldest first (FIFO)

    # Top the queue up to one tick's worth, never flood it: the drain below pulls at most
    # QUEUE_PER_TICK, so a bigger drop would leave items waiting past PENDING_TTL (which
    # doctor's marker-health check flags). Items beyond the room wait for a later tick.
    room = max(0, QUEUE_PER_TICK - len(distill_queue.drain()))
    offered = 0
    for _mtime, p, source_dir in candidates:
        if offered >= room:
            break
        sid = os.path.splitext(os.path.basename(p))[0]
        agent = _agent_for_dir(source_dir)
        text = extract(p)
        original_text_chars = len(text)
        if len(text) < MIN_TEXT:
            markers.mark_done(sid)  # no content → done (don't re-offer)
            _log_worker_event(
                "ingest_offer",
                "skipped",
                agent=agent,
                session_id=sid,
                reason="too_short",
                source_chars=original_text_chars,
            )
            continue
        text, was_clamped = transcript.clamp_text(text, CLAMP)
        cwd = transcript_cwd(p)
        remote_url = distill_core.git_remote_url(cwd)
        origin, _name = boring_config.classify(cwd, remote_url)
        repo = _repo_slug(cwd)
        # The shared queue replaces the old printed prompt: enqueue() marks the session pending,
        # and the drain below runs it through the engine in the same tick.
        distill_queue.enqueue(distill_queue.QueueItem(sid, agent, origin, repo, text))
        offered += 1
        _log_worker_event(
            "ingest_offer",
            "queued",
            agent=agent,
            session_id=sid,
            origin=origin,
            repo=repo,
            source_chars=original_text_chars,
            emitted_chars=len(text),
            clamped=was_clamped,
        )
    # DRAIN — after the offer scan, so a session offered this tick is processed this tick.
    _drain_queue()
    if offered == 0:
        # nothing offered: either nothing eligible, or the queue had no room this tick
        _log_worker_event("ingest_offer", "ok", offered=0, eligible=len(candidates), room=room)


if __name__ == "__main__":
    main()
