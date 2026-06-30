#!/usr/bin/env python3
"""Append-only local NDJSON events for adapter/workflow observability."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_EVENT_LOG = "~/.cache/oh-my-boring/events.ndjson"
DEFAULT_RECENT_HOURS = 24


def event_log_path() -> Path:
    raw = os.environ.get("BORING_EVENT_LOG") or DEFAULT_EVENT_LOG
    return Path(os.path.expanduser(raw))


def append_event(component: str, event: str, status: str, **fields: Any) -> None:
    path = event_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "component": component,
        "event": event,
        "status": status,
    }
    payload.update({k: v for k, v in fields.items() if v is not None})
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        f.write("\n")


def recent_resolution_failures(limit: int = 3, hours: Optional[int] = None) -> list[dict[str, Any]]:
    path = event_log_path()
    if not path.exists():
        return []
    if hours is None:
        raw_hours = os.environ.get("BORING_EVENT_RECENT_HOURS") or str(DEFAULT_RECENT_HOURS)
        hours = int(raw_hours)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    failures: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "distill_resolution":
            continue
        ts = _parse_ts(str(event.get("ts") or ""))
        if ts is not None and ts < cutoff:
            continue
        if event.get("verifier_status") == "failed" or event.get("status") == "failed":
            failures.append(event)
    return failures[-limit:]


def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect oh-my-boring local event log")
    parser.add_argument("--recent-resolution-failures", action="store_true")
    parser.add_argument("--max", type=int, default=3)
    parser.add_argument("--hours", type=int, default=None)
    args = parser.parse_args()

    if args.recent_resolution_failures:
        failures = recent_resolution_failures(args.max, args.hours)
        if not failures:
            print(f"resolution_quality recent_failures=0 log={event_log_path()}")
            return 0
        print(f"resolution_quality recent_failures={len(failures)} log={event_log_path()}")
        for event in failures:
            missing = ",".join(event.get("missing_fields") or [])
            print(
                "  "
                f"session={event.get('session_id', '')} "
                f"resolution={event.get('resolution', '')} "
                f"remember={event.get('remember_status', '')} "
                f"missing={missing}"
            )
        return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
