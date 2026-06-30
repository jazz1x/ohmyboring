#!/usr/bin/env python3
"""Centralized marker bookkeeping for session distillation/ingestion queues.

All adapters share ``~/.cache/boring-distill`` so engine-direct SessionEnd hooks,
hermes-agent cron, and host-side backfill schedulers see the same queue state.

Markers:
- ``<sid>.ts``     — done (the session has been distilled/ingested successfully).
- ``<sid>.pending`` — currently queued/processing.
- ``<sid>.retry``   — transient failure; backfill schedulers should retry later.
"""
import os
import re
import time
from pathlib import Path
from typing import Optional

MARK_DIR = os.path.expanduser("~/.cache/boring-distill")


def set_mark_dir(path: str) -> None:
    """Override the marker directory (used by containerized workers)."""
    global MARK_DIR
    MARK_DIR = path


def safe_id(session_id: str) -> str:
    """Sanitize a session id for use in a filename."""
    return re.sub(r"[^A-Za-z0-9_-]", "", session_id) or "nosession"


def _paths(session_id: str) -> tuple[str, str, str]:
    base = os.path.join(MARK_DIR, safe_id(session_id))
    return f"{base}.ts", f"{base}.pending", f"{base}.retry"


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
    """Write a done marker and clean up any pending/retry markers."""
    ts, pending, retry = _paths(session_id)
    _ensure_dir()
    _transition_marker(ts, (pending, retry), str(time.time()))


def mark_retry(session_id: str) -> None:
    """Write a retry marker and remove the done/pending markers if present."""
    ts, pending, retry = _paths(session_id)
    _ensure_dir()
    _transition_marker(retry, (ts, pending), str(time.time()))


def mark_pending(session_id: str) -> None:
    """Write a plain pending marker and remove done/retry markers."""
    ts, pending, retry = _paths(session_id)
    _ensure_dir()
    _transition_marker(pending, (ts, retry), str(time.time()))


def is_done(session_id: str) -> bool:
    """Return True if a done marker exists."""
    return os.path.exists(_paths(session_id)[0])


def is_pending(session_id: str, ttl: Optional[float] = None) -> bool:
    """Return True if a pending marker exists and (when ttl is given) is not expired."""
    _, path, _ = _paths(session_id)
    if not os.path.exists(path):
        return False
    if ttl is None:
        return True
    try:
        return (time.time() - os.path.getmtime(path)) < ttl
    except OSError:
        return False


def is_retry(session_id: str, ttl: Optional[float] = None) -> bool:
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


def done_time(session_id: str) -> Optional[float]:
    """Return the mtime of the done marker, or None if absent."""
    ts, _, _ = _paths(session_id)
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
    ts, path, retry = _paths(session_id)
    _ensure_dir()
    _transition_marker(path, (ts, retry), f"{session_id}\n{before}\n{attempts}")


def read_ingest_pending(session_id: str) -> Optional[tuple[str, int, int]]:
    """Parse the ingest-worker's pending marker. Return None if absent/corrupt."""
    _, path, _ = _paths(session_id)
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
    _, path, _ = _paths(session_id)
    _remove_marker(path)
