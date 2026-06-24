#!/usr/bin/env python3
"""Raw session retention — archive old transcripts, delete ancient archives/markers.

Claude Code writes one .jsonl per session under ~/.claude/projects. Once a session
has been distilled into the vault (marked by ~/.cache/boring-distill/<sid>.ts), the
raw transcript is no longer needed for day-to-day recall. This script archives it
to save disk and reduce secret/exposure surface, then deletes archives that are too
old to be useful.

Policy knobs (env):
  BORING_RETENTION_PROCESSED_DAYS   default 30   → archive processed sessions older than this
  BORING_RETENTION_UNPROCESSED_DAYS default 90   → archive unprocessed sessions older than this
  BORING_RETENTION_ARCHIVE_DAYS     default 180  → delete archived .gz files older than this
  BORING_RETENTION_DELETE_ONLY      default 0    → if 1, delete sessions instead of archiving

Dry-run by default; pass --apply to execute.
"""
import argparse
import gzip
import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "agents", "shared")
)
import boring_config  # noqa: E402

DEFAULT_PROCESSED_DAYS = 30
DEFAULT_UNPROCESSED_DAYS = 90
DEFAULT_ARCHIVE_DAYS = 180
DEFAULT_PENDING_STALE_DAYS = 2
DEFAULT_RETRY_STALE_DAYS = 90


def _env_days(name: str, default: int) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


def _safe(sid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", sid) or "nosession"


def _source_dirs() -> list[Path]:
    dirs = boring_config.source_dirs(adapter="session-end")
    if not dirs:
        dirs = [os.path.expanduser("~/.claude/projects")]
    return [Path(d).expanduser() for d in dirs]


def _mark_dir() -> Path:
    return Path(os.path.expanduser("~/.cache/boring-distill"))


def _archive_dir() -> Path:
    return _mark_dir() / "archive"


def _collect_sessions(source_dirs: list[Path]):
    sessions = []
    for d in source_dirs:
        if not d.exists():
            continue
        for p in d.rglob("*.jsonl"):
            if p.name.endswith(".jsonl"):
                sessions.append(p)
    return sessions


def _marker_exists(sid: str, suffix: str) -> bool:
    return (_mark_dir() / f"{_safe(sid)}{suffix}").exists()


def _marker_mtime(sid: str, suffix: str) -> float | None:
    path = _mark_dir() / f"{_safe(sid)}{suffix}"
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _classify(sid: str) -> str:
    if _marker_exists(sid, ".pending"):
        return "pending"
    if _marker_exists(sid, ".ts"):
        return "processed"
    return "unprocessed"


def _format_size(bytes_: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if bytes_ < 1024:
            return f"{bytes_:.1f}{unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f}TB"


def _archive(src: Path, archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    tmp = archive.with_suffix(archive.suffix + ".tmp")
    with open(src, "rb") as f_in, gzip.open(tmp, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    # preserve original mtime on archive for age-based cleanup
    mtime = src.stat().st_mtime
    os.utime(tmp, (mtime, mtime))
    os.replace(tmp, archive)


def plan(now, processed_days, unprocessed_days, archive_days, delete_only) -> dict:
    """Pure planning pass — scans the filesystem (read-only) and decides what WOULD be archived /
    deleted, without mutating anything. Extracted from main() so the data-loss guardrails (an
    unprocessed session is never hard-deleted; thresholds; processed-only archive deletion) are unit-
    testable. main() prints + applies the returned plan; --apply alone touches the disk."""
    source_dirs = _source_dirs()
    sessions = _collect_sessions(source_dirs)
    mark_dir = _mark_dir()
    archive_dir = _archive_dir()

    to_archive: list[tuple[Path, Path, str]] = []
    to_delete: list[Path] = []
    bytes_total = 0

    for p in sessions:
        sid = p.stem
        age_days = (now - p.stat().st_mtime) / 86400
        state = _classify(sid)

        if state == "pending":
            continue

        threshold = processed_days if state == "processed" else unprocessed_days
        if age_days < threshold:
            continue

        # An UNPROCESSED session (no .ts) was never distilled into the vault, so its raw transcript
        # is the ONLY source it can ever be distilled from. Never hard-delete it — always archive
        # (recoverable gzip), even under delete_only. Only PROCESSED sessions honor delete_only.
        if delete_only and state == "processed":
            to_delete.append(p)
            bytes_total += p.stat().st_size
        else:
            archive = archive_dir / f"{p.stem}.jsonl.gz"
            to_archive.append((p, archive, state))
            bytes_total += p.stat().st_size

    # ancient archives — but only delete archives of PROCESSED sessions (already in the vault).
    # An undistilled session's archive is its sole re-distill source; never auto-delete it.
    ancient_archives: list[Path] = []
    if archive_dir.exists():
        for p in archive_dir.glob("*.jsonl.gz"):
            age_days = (now - p.stat().st_mtime) / 86400
            sid = p.name[: -len(".jsonl.gz")]
            if age_days >= archive_days and _classify(sid) == "processed":
                ancient_archives.append(p)

    # stale markers
    stale_pending: list[Path] = []
    stale_retry: list[Path] = []
    if mark_dir.exists():
        for p in mark_dir.glob("*.pending"):
            age_days = (now - p.stat().st_mtime) / 86400
            if age_days >= DEFAULT_PENDING_STALE_DAYS:
                stale_pending.append(p)
        for p in mark_dir.glob("*.retry"):
            age_days = (now - p.stat().st_mtime) / 86400
            if age_days >= DEFAULT_RETRY_STALE_DAYS:
                stale_retry.append(p)

    return {
        "source_dirs": source_dirs,
        "mark_dir": mark_dir,
        "archive_dir": archive_dir,
        "sessions": sessions,
        "to_archive": to_archive,
        "to_delete": to_delete,
        "ancient_archives": ancient_archives,
        "stale_pending": stale_pending,
        "stale_retry": stale_retry,
        "bytes": bytes_total,
    }


def main():
    parser = argparse.ArgumentParser(description="Manage raw session transcript retention")
    parser.add_argument("--apply", action="store_true", help="actually archive/delete instead of dry-run")
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    args = parser.parse_args()

    processed_days = _env_days("BORING_RETENTION_PROCESSED_DAYS", DEFAULT_PROCESSED_DAYS)
    unprocessed_days = _env_days("BORING_RETENTION_UNPROCESSED_DAYS", DEFAULT_UNPROCESSED_DAYS)
    archive_days = _env_days("BORING_RETENTION_ARCHIVE_DAYS", DEFAULT_ARCHIVE_DAYS)
    delete_only = os.environ.get("BORING_RETENTION_DELETE_ONLY", "0").strip() in ("1", "true", "yes")
    now = time.time()

    p = plan(now, processed_days, unprocessed_days, archive_days, delete_only)
    source_dirs = p["source_dirs"]
    mark_dir = p["mark_dir"]
    archive_dir = p["archive_dir"]
    sessions = p["sessions"]
    to_archive = p["to_archive"]
    to_delete = p["to_delete"]
    ancient_archives = p["ancient_archives"]
    stale_pending = p["stale_pending"]
    stale_retry = p["stale_retry"]
    bytes_to_archive = p["bytes"]

    total_actions = len(to_archive) + len(to_delete) + len(ancient_archives) + len(stale_pending) + len(stale_retry)

    print(f"📂 source dirs: {[str(d) for d in source_dirs]}")
    print(f"🗑  mark dir:   {mark_dir}")
    print(f"📦 archive dir: {archive_dir}\n")
    print(f"scanned sessions: {len(sessions)}")
    print(
        f"policy: processed≥{processed_days}d, unprocessed≥{unprocessed_days}d, "
        f"archive≥{archive_days}d, delete_only={delete_only}\n"
    )

    if not total_actions:
        print("✅ Nothing to clean up.")
        return

    if to_archive:
        print(f"📦 Archive {len(to_archive)} session(s) ({_format_size(bytes_to_archive)}):")
        for src, dst, state in to_archive:
            print(f"   [{state}] {src} → {dst}")
        print()

    if to_delete:
        print(f"🗑  Delete {len(to_delete)} session(s) ({_format_size(bytes_to_archive)}):")
        for p in to_delete:
            print(f"   {p}")
        print()

    if ancient_archives:
        print(f"🗑  Delete {len(ancient_archives)} ancient archive(s):")
        for p in ancient_archives:
            print(f"   {p}")
        print()

    if stale_pending:
        print(f"🧹 Remove {len(stale_pending)} stale pending marker(s)")
        print()
    if stale_retry:
        print(f"🧹 Remove {len(stale_retry)} stale retry marker(s)")
        print()

    if not args.apply:
        print("💡 This is a dry-run. Pass --apply to execute.")
        return

    if not args.yes:
        ans = input("Apply retention? [y/N] ")
        if ans.lower() not in ("y", "yes"):
            print("aborted.")
            return

    done_archive = done_delete = 0
    for src, dst, _state in to_archive:
        try:
            _archive(src, dst)
            src.unlink()
            done_archive += 1
        except OSError as e:
            print(f"[error] failed to archive {src}: {e}", file=sys.stderr)

    for p in to_delete:
        try:
            p.unlink()
            done_delete += 1
        except OSError as e:
            print(f"[error] failed to delete {p}: {e}", file=sys.stderr)

    for p in ancient_archives:
        try:
            p.unlink()
        except OSError as e:
            print(f"[error] failed to delete archive {p}: {e}", file=sys.stderr)

    for p in stale_pending + stale_retry:
        try:
            p.unlink()
        except OSError as e:
            print(f"[error] failed to delete marker {p}: {e}", file=sys.stderr)

    print(
        f"\n✅ Done: archived {done_archive}, deleted {done_delete} sessions, "
        f"removed {len(ancient_archives)} ancient archives, "
        f"{len(stale_pending)} pending / {len(stale_retry)} retry markers."
    )


if __name__ == "__main__":
    main()
