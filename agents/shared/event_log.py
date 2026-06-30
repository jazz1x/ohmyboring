#!/usr/bin/env python3
"""Append local workflow events and mirror them into the local engine DB."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_EVENT_LOG = "~/.cache/oh-my-boring/events.ndjson"
DEFAULT_RECENT_HOURS = 24
DEFAULT_SERVICE_NAMESPACE = "oh-my-boring"


def event_log_path() -> Path:
    raw = os.environ.get("BORING_EVENT_LOG") or DEFAULT_EVENT_LOG
    return Path(os.path.expanduser(raw))


def append_event(component: str, event: str, status: str, **fields: Any) -> None:
    path = event_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = {k: v for k, v in fields.items() if v is not None}
    if "run_id" not in normalized and normalized.get("session_id"):
        normalized["run_id"] = normalized["session_id"]
    now = datetime.now(timezone.utc)
    payload = {
        "ts": now.isoformat(),
        "component": component,
        "event": event,
        "status": status,
    }
    payload.update(normalized)
    payload["otel"] = _otel_envelope(payload, now)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        f.write("\n")
    _try_mirror_to_engine(payload)


def try_append_event(component: str, event: str, status: str, **fields: Any) -> bool:
    try:
        append_event(component, event, status, **fields)
    except OSError as e:
        print(f"[event-log] write failed: {e}", file=sys.stderr)
        return False
    return True


def new_run_id(component: str) -> str:
    return f"{component}-{uuid.uuid4()}"


def _otel_envelope(payload: dict[str, Any], observed_at: datetime) -> dict[str, Any]:
    status = str(payload.get("status") or "")
    severity_text, severity_number = _severity(status)
    run_key = str(payload.get("run_id") or payload.get("session_id") or "")
    trace_id, span_id = _trace_span_ids(run_key, payload)
    return {
        "observed_timestamp": observed_at.isoformat(),
        "time_unix_nano": int(observed_at.timestamp() * 1_000_000_000),
        "severity_text": severity_text,
        "severity_number": severity_number,
        "body": {
            "event.name": payload.get("event", ""),
            "status": status,
        },
        "attributes": dict(payload),
        "resource": {
            "attributes": {
                "service.name": payload.get("component", ""),
                "service.namespace": DEFAULT_SERVICE_NAMESPACE,
            }
        },
        "trace_id": trace_id,
        "span_id": span_id,
        "event_name": payload.get("event", ""),
    }


def _severity(status: str) -> tuple[str, int]:
    normalized = status.strip().lower()
    if normalized in {"failed", "failure", "error"}:
        return ("ERROR", 17)
    if normalized in {"warn", "warning"}:
        return ("WARN", 13)
    if normalized == "debug":
        return ("DEBUG", 5)
    if normalized == "trace":
        return ("TRACE", 1)
    return ("INFO", 9)


def _trace_span_ids(run_key: str, payload: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    if not run_key:
        return (None, None)
    seed = json.dumps(
        {
            "run_id": run_key,
            "component": payload.get("component"),
            "event": payload.get("event"),
            "status": payload.get("status"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return (digest[:32], digest[32:48])


def _try_mirror_to_engine(payload: dict[str, Any]) -> None:
    url = _event_sink_url()
    if not url:
        return
    timeout = float(os.environ.get("BORING_EVENT_SINK_TIMEOUT") or "0.5")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[event-log] DB mirror failed: {e}", file=sys.stderr)


def _event_sink_url() -> Optional[str]:
    mirror_mode = os.environ.get("BORING_EVENT_DB_MIRROR", "").strip().lower()
    if mirror_mode in {"0", "false", "no", "off"}:
        return None
    explicit = os.environ.get("BORING_EVENT_SINK_URL")
    if explicit:
        return explicit
    base = os.environ.get("BORING_URL") or "http://127.0.0.1:7700"
    return f"{base.rstrip('/')}/events"


def iter_events() -> list[dict[str, Any]]:
    path = event_log_path()
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def recent_events(
    limit: int = 20,
    component: Optional[str] = None,
    event_name: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    events = []
    for item in iter_events():
        if component and item.get("component") != component:
            continue
        if event_name and item.get("event") != event_name:
            continue
        if status and item.get("status") != status:
            continue
        events.append(item)
    return events[-limit:]


def recent_resolution_failures(limit: int = 3, hours: Optional[int] = None) -> list[dict[str, Any]]:
    if hours is None:
        raw_hours = os.environ.get("BORING_EVENT_RECENT_HOURS") or str(DEFAULT_RECENT_HOURS)
        hours = int(raw_hours)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    latest_by_key: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    anonymous_failures: list[tuple[int, dict[str, Any]]] = []
    for idx, event in enumerate(iter_events()):
        if event.get("event") != "distill_resolution":
            continue
        ts = _parse_ts(str(event.get("ts") or ""))
        if ts is not None and ts < cutoff:
            continue
        key = _resolution_event_key(event)
        if key is None:
            if _is_resolution_failure(event):
                anonymous_failures.append((idx, event))
            continue
        latest_by_key[key] = (idx, event)
    failures = anonymous_failures + [
        (idx, event)
        for idx, event in latest_by_key.values()
        if _is_resolution_failure(event)
    ]
    failures.sort(key=lambda item: item[0])
    return [event for _, event in failures[-limit:]]


def _resolution_event_key(event: dict[str, Any]) -> Optional[tuple[str, str]]:
    session = event.get("session_id") or event.get("run_id")
    if not session:
        return None
    return (str(session), str(event.get("resolution") or ""))


def _is_resolution_failure(event: dict[str, Any]) -> bool:
    return event.get("verifier_status") == "failed" or event.get("status") == "failed"


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


def _parse_field(raw: str) -> tuple[str, Any]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"field must be key=value: {raw}")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key:
        raise argparse.ArgumentTypeError(f"field key is empty: {raw}")
    return key, _coerce_value(value)


def _coerce_value(value: str) -> Any:
    text = value.strip()
    if text == "":
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _format_event(event: dict[str, Any]) -> str:
    head = (
        f"{event.get('ts', '')} "
        f"{event.get('component', '')} "
        f"{event.get('event', '')} "
        f"{event.get('status', '')}"
    ).strip()
    details = []
    for key in sorted(event):
        if key in {"ts", "component", "event", "status"}:
            continue
        value = event[key]
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            rendered = str(value)
        details.append(f"{key}={rendered}")
    return f"{head} {' '.join(details)}".rstrip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect oh-my-boring local event log")
    parser.add_argument("--recent-resolution-failures", action="store_true")
    parser.add_argument("--record", nargs=3, metavar=("COMPONENT", "EVENT", "STATUS"))
    parser.add_argument("--field", action="append", default=[], type=_parse_field)
    parser.add_argument("--tail", action="store_true")
    parser.add_argument("--component")
    parser.add_argument("--event")
    parser.add_argument("--status")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max", type=int, default=3)
    parser.add_argument("--hours", type=int, default=None)
    args = parser.parse_args()

    if args.record:
        fields = dict(args.field)
        append_event(args.record[0], args.record[1], args.record[2], **fields)
        return 0

    if args.tail:
        events = recent_events(args.max, args.component, args.event, args.status)
        for event in events:
            if args.json:
                print(json.dumps(event, ensure_ascii=False, sort_keys=True))
            else:
                print(_format_event(event))
        return 0

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
