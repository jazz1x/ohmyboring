#!/usr/bin/env python3
"""cwd -> (origin, repo) table on the shared marker dir (stdlib only; hooks import it).

The hermes ingest worker runs in a container with no /Users/... mounted, so it cannot ask git
about a transcript's cwd. The host hooks can, so they write the answer here and the worker reads
it. One file per cwd, keyed by the cwd's hash, under ``markers.MARK_DIR`` (host
~/.cache/boring-distill = container /host/.cache/boring-distill).
"""

import hashlib
import json
import os
import tempfile

import markers


def _path(cwd: str) -> str:
    key = hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:24]
    return os.path.join(markers.MARK_DIR, f"repo-{key}.json")


def has(cwd: str) -> bool:
    """One stat — the recall hook calls this on every prompt."""
    return bool(cwd) and os.path.exists(_path(cwd))


def write(cwd: str, origin: str, repo: str) -> None:
    """Atomic overwrite (temp file + rename): a reader sees the old entry or the new, never half."""
    os.makedirs(markers.MARK_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=markers.MARK_DIR, prefix=".repo-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"cwd": cwd, "origin": origin, "repo": repo}, f)
    os.replace(tmp, _path(cwd))


def read(cwd: str) -> tuple[str, str] | None:
    """(origin, repo) for cwd, or None when the table has no entry (or an unreadable one)."""
    if not cwd:
        return None
    try:
        with open(_path(cwd), encoding="utf-8") as f:
            entry = json.load(f)
        return str(entry["origin"]), str(entry["repo"])
    except (OSError, ValueError, KeyError):
        return None
