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
    try:
        os.makedirs(MARK_DIR, exist_ok=True)
    except OSError:
        pass


def mark_done(session_id: str) -> None:
    """Write a done marker and clean up any pending/retry markers."""
    ts, pending, retry = _paths(session_id)
    _ensure_dir()
    for p in (pending, retry):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    try:
        with open(ts, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def mark_retry(session_id: str) -> None:
    """Write a retry marker and remove the done/pending markers if present."""
    ts, pending, retry = _paths(session_id)
    _ensure_dir()
    for p in (ts, pending):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    try:
        with open(retry, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def mark_pending(session_id: str) -> None:
    """Write a plain pending marker and remove done/retry markers."""
    ts, pending, retry = _paths(session_id)
    _ensure_dir()
    for p in (ts, retry):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    try:
        with open(pending, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass


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


def is_retry(session_id: str) -> bool:
    """Return True if a retry marker exists."""
    return os.path.exists(_paths(session_id)[2])


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
    _, path, _ = _paths(session_id)
    _ensure_dir()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{session_id}\n{before}\n{attempts}")
    except OSError:
        pass


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
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
