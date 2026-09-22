#!/usr/bin/env python3
"""Centralized marker bookkeeping for session distillation/ingestion queues.

All adapters share ``~/.cache/boring-distill`` so engine-direct SessionEnd hooks,
hermes-agent cron, and host-side backfill schedulers see the same queue state.

Markers:
- ``<sid>.ts``      — done (the session has been distilled/ingested successfully).
- ``<sid>.pending`` — currently queued/processing.
- ``<sid>.retry``   — transient failure; backfill schedulers should retry later.
                       Content is ``"<mtime>\\n<attempts>"``; markers written before
                       attempt-counting existed are timestamp-only and read as attempt 1.
- ``<sid>.dead``    — permanent failure; ``mark_retry`` transitions here once attempts
                       reach ``MARKER_RETRY_MAX_ATTEMPTS``. Collectors must exclude
                       ``.dead`` from their queues so a repeatedly-failing session stops
                       occupying the head of the line (head-of-line blocking).
"""

import os
import re
import sys
import time
from pathlib import Path

MARK_DIR = os.path.expanduser("~/.cache/boring-distill")

# SSOT for how many times `mark_retry` will re-queue a session before giving up and
# writing a `.dead` marker instead. Override via env for ops tuning without a code change.
RETRY_MAX_ATTEMPTS_ENV = "MARKER_RETRY_MAX_ATTEMPTS"
DEFAULT_RETRY_MAX_ATTEMPTS = 5


def _retry_max_attempts() -> int:
    raw = os.environ.get(RETRY_MAX_ATTEMPTS_ENV)
    if not raw:
        return DEFAULT_RETRY_MAX_ATTEMPTS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_RETRY_MAX_ATTEMPTS


def set_mark_dir(path: str) -> None:
    """Override the marker directory (used by containerized workers)."""
    global MARK_DIR
    MARK_DIR = path


def safe_id(session_id: str) -> str:
    """Sanitize a session id for use in a filename."""
    return re.sub(r"[^A-Za-z0-9_-]", "", session_id) or "nosession"


def _paths(session_id: str) -> tuple[str, str, str, str]:
    base = os.path.join(MARK_DIR, safe_id(session_id))
    return f"{base}.ts", f"{base}.pending", f"{base}.retry", f"{base}.dead"


def _ensure_dir() -> None:
    os.makedirs(MARK_DIR, exist_ok=True)


def _remove_marker(path: str) -> None:
    Path(path).unlink(missing_ok=True)


def _write_marker(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _transition_marker(target: str, cleanup: tuple[str, ...], text: str) -> None:
    _write_marker(target, text)
    for p in cleanup:
        _remove_marker(p)


def mark_done(session_id: str) -> None:
    """Write a done marker and clean up any pending/retry/dead markers."""
    ts, pending, retry, dead = _paths(session_id)
    _ensure_dir()
    _transition_marker(ts, (pending, retry, dead), str(time.time()))


def _read_retry_count(retry_path: str) -> int:
    """Parse the attempt count out of a `.retry` marker; 0 if absent.

    Current format is ``"<mtime>\\n<attempts>"``. Markers written before attempt
    counting existed are timestamp-only (no newline) — their mere existence already
    means one attempt failed, so they read as attempt 1, not 0. Reading them as 0
    would silently reset the failure history for every session already on disk.
    """
    try:
        with open(retry_path, encoding="utf-8") as f:
            content = f.read().strip()
    except OSError:
        return 0
    if not content:
        return 0
    parts = content.split("\n")
    if len(parts) >= 2:
        try:
            return int(parts[1].strip())
        except ValueError:
            return 1
    return 1


def retry_count(session_id: str) -> int:
    """Return the recorded attempt count for `session_id`'s `.retry` marker (0 if none)."""
    return _read_retry_count(_paths(session_id)[2])


def mark_retry(session_id: str, reason: str = "") -> int:
    """Write/increment a retry marker; return the attempt count just recorded.

    Once attempts reach ``MARKER_RETRY_MAX_ATTEMPTS`` (env override, default
    ``DEFAULT_RETRY_MAX_ATTEMPTS``), transitions to a `.dead` marker instead so the
    session stops occupying the head of the retry queue. `.retry` stays eligible for
    retry below that threshold — transient failures (engine down, etc.) still recover.
    """
    ts, pending, retry, dead = _paths(session_id)
    _ensure_dir()
    attempts = _read_retry_count(retry) + 1
    if attempts >= _retry_max_attempts():
        _transition_marker(dead, (ts, pending, retry), f"{time.time()}\n{attempts}\n{reason}")
        print(
            f"[markers] {session_id} dead-lettered after {attempts} attempts"
            f"{f' ({reason})' if reason else ''}",
            file=sys.stderr,
        )
        return attempts
    _transition_marker(retry, (ts, pending), f"{time.time()}\n{attempts}")
    return attempts


def mark_pending(session_id: str) -> None:
    """Write a plain pending marker and remove done/retry/dead markers."""
    ts, pending, retry, dead = _paths(session_id)
    _ensure_dir()
    _transition_marker(pending, (ts, retry, dead), str(time.time()))


def is_done(session_id: str) -> bool:
    """Return True if a done marker exists."""
    return os.path.exists(_paths(session_id)[0])


def is_dead(session_id: str) -> bool:
    """Return True if a dead-letter marker exists (permanently failed; do not re-queue)."""
    return os.path.exists(_paths(session_id)[3])


def is_pending(session_id: str, ttl: float | None = None) -> bool:
    """Return True if a pending marker exists and (when ttl is given) is not expired."""
    _, path, _, _ = _paths(session_id)
    if not os.path.exists(path):
        return False
    if ttl is None:
        return True
    try:
        return (time.time() - os.path.getmtime(path)) < ttl
    except OSError:
        return False


def is_retry(session_id: str, ttl: float | None = None) -> bool:
    """Return True if a retry marker exists and (when ttl is given) is not expired."""
    path = _paths(session_id)[2]
    if not os.path.exists(path):
        return False
    if ttl is None:
        return True
    try:
        return (time.time() - os.path.getmtime(path)) < ttl
    except OSError:
        return False


def done_time(session_id: str) -> float | None:
    """Return the mtime of the done marker, or None if absent."""
    ts, _, _, _ = _paths(session_id)
    try:
        return os.path.getmtime(ts)
    except OSError:
        return None


# ─────────────────────────────────────────────────────────────
# hermes ingest-worker pending marker (carries extra metadata)
# ─────────────────────────────────────────────────────────────


def ingest_pending_path(session_id: str) -> str:
    """Path to the ingest-worker's pending marker for ``session_id``."""
    return _paths(session_id)[1]


def write_ingest_pending(session_id: str, before: int, attempts: int) -> None:
    """Write the ingest-worker's pending marker with ``(sid, before, attempts)``."""
    ts, path, retry, _dead = _paths(session_id)
    _ensure_dir()
    _transition_marker(path, (ts, retry), f"{session_id}\n{before}\n{attempts}")


def read_ingest_pending(session_id: str) -> tuple[str, int, int] | None:
    """Parse the ingest-worker's pending marker. Return None if absent/corrupt."""
    _, path, _, _ = _paths(session_id)
    try:
        with open(path, encoding="utf-8") as f:
            parts = f.read().strip().split("\n")
        sid = parts[0]
        before = int(parts[1].strip())
        attempts = int(parts[2].strip()) if len(parts) > 2 else 0
        return sid, before, attempts
    except Exception:
        return None


def remove_pending(session_id: str) -> None:
    """Remove any pending marker for ``session_id``."""
    _, path, _, _ = _paths(session_id)
    _remove_marker(path)
