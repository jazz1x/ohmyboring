#!/usr/bin/env python3
"""Record local workflow events into the engine DB, with a file spool fallback."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_EVENT_LOG = "~/.cache/oh-my-boring/events.ndjson"
DEFAULT_RECENT_HOURS = 24
DEFAULT_SERVICE_NAMESPACE = "oh-my-boring"


def _door_url() -> str:
    """문(:7710) 주소 — ohmyboring.config.door_url 의 규칙을 그대로 옮긴 것.

    이 모듈은 doctor 가 소스 트리에서 벌거벗은 파이썬으로 직접 실행한다(third-party ·
    ohmyboring 패키지 임포트 불가). 규칙은 config.door_url 가 바뀌면 여기도 같이 —
    adapters/test_events.py 의 door_url 대치 시험이 둘을 못 박는다(E4-α: 사건 싱크도
    문 하나를 지난다 — 명시적 BORING_EVENT_SINK_URL 은 그대로 윗사람이다).
    """
    if os.environ.get("BORING_IN_CONTAINER", "").lower() in ("1", "true", "yes"):
        default = "http://boring-door:7710"
    else:
        default = "http://localhost:7710"
    return os.environ.get("BORING_DOOR_URL") or default


def event_log_path() -> Path:
    raw = os.environ.get("BORING_EVENT_LOG") or DEFAULT_EVENT_LOG
    return Path(os.path.expanduser(raw))


def foreign_dsn_refusal(dsn: str, env: Mapping[str, str]) -> str | None:
    """사본 DB(--dsn != DOOR_PG_DSN)로 사건을 낼 때 라이브·기본 spool 로 새지 않게 하는 한 곳의 판단.

    거부 이유 한 줄, 통과면 None. 문 안 정상 실행(--dsn 미지정 = DOOR_PG_DSN)은 항상 통과한다.
    """
    if dsn == env.get("DOOR_PG_DSN", ""):
        return None
    if env.get("BORING_EVENT_SINK_URL"):
        return "BORING_EVENT_SINK_URL 이 명시돼 있다 — 사본 DB 실행은 라이브로 사건을 못 낸다."
    if env.get("BORING_EVENT_SINK", "").strip().lower() != "spool":
        return "사본 DB(--dsn)는 BORING_EVENT_SINK=spool 과 임시 BORING_EVENT_LOG 가 있어야 돈다."
    log = env.get("BORING_EVENT_LOG", "")
    if not log:
        return "사본 DB(--dsn)는 BORING_EVENT_LOG 를 임시 파일로 줘야 돈다."
    if Path(os.path.expanduser(log)).resolve() == Path(os.path.expanduser(DEFAULT_EVENT_LOG)).resolve():
        return "BORING_EVENT_LOG 가 기본 spool 경로다 — 임시 파일을 줘야 한다."
    return None


def append_event(component: str, event: str, status: str, **fields: Any) -> None:
    payload = _event_payload(component, event, status, **fields)
    db_enabled = _event_sink_url() is not None
    stored_in_db = _try_store_in_engine(payload)
    spool_mode = _event_spool_mode(db_enabled)
    if spool_mode == "always" or (spool_mode == "on_failure" and not stored_in_db):
        _append_to_spool(payload)


def _event_payload(component: str, event: str, status: str, **fields: Any) -> dict[str, Any]:
    normalized = {k: v for k, v in fields.items() if v is not None}
    if "run_id" not in normalized and normalized.get("session_id"):
        normalized["run_id"] = normalized["session_id"]
    now = datetime.now(UTC)
    payload = {
        "ts": now.isoformat(),
        "component": component,
        "event": event,
        "status": status,
    }
    payload.update(normalized)
    payload["otel"] = _otel_envelope(payload, now)
    return payload


def _append_to_spool(payload: dict[str, Any]) -> None:
    path = event_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        f.write("\n")


def replay_spool(path: Path | None = None) -> dict[str, int]:
    """Hand the spooled rows to the engine, and keep only the ones it still refuses.

    The spool is where a row lands when the engine is down. Nothing brought those rows back:
    `doctor` had an alarm for them — "EVENTS TRAPPED IN THE SPOOL" — and no remedy, so the
    sessions they describe stayed missing from the verdict population for as long as nobody
    replayed them by hand. Measured 2026-09-21 after a Docker restart: 2 verdict-kind rows from 1
    session inside the open window, invisible to coverage because its numerator and denominator
    vanished together.

    Each row is POSTed exactly as it was spooled — the engine keys on the row's own timestamp, so a
    replay is not a second event. Rows the engine still refuses stay in the spool for the next
    attempt; a row that cannot be parsed stays too, and is counted, rather than being dropped on
    the way through. Returns counts so the caller can say what happened instead of "done".
    """
    spool = path or event_log_path()
    if not spool.exists():
        return {"replayed": 0, "kept": 0, "unparseable": 0}
    replayed = kept = unparseable = 0
    remaining: list[str] = []
    for line in spool.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            unparseable += 1
            remaining.append(line)
            continue
        if _try_store_in_engine(payload):
            replayed += 1
        else:
            kept += 1
            remaining.append(line)
    tmp = spool.with_suffix(spool.suffix + ".replaying")
    tmp.write_text("".join(f"{ln}\n" for ln in remaining), encoding="utf-8")
    tmp.replace(spool)
    return {"replayed": replayed, "kept": kept, "unparseable": unparseable}


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


def _trace_span_ids(run_key: str, payload: dict[str, Any]) -> tuple[str | None, str | None]:
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


def _try_store_in_engine(payload: dict[str, Any]) -> bool:
    url = _event_sink_url()
    if not url:
        return False
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
            return True
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[event-log] DB sink failed: {e}", file=sys.stderr)
        return False


def _event_sink_url() -> str | None:
    if _event_sink_mode() == "spool":
        return None
    explicit = os.environ.get("BORING_EVENT_SINK_URL")
    if explicit:
        return explicit
    return f"{_door_url().rstrip('/')}/events"


def _event_sink_mode() -> str:
    raw = os.environ.get("BORING_EVENT_SINK", "").strip().lower()
    if raw in {"db", "spool", "both"}:
        return raw
    legacy = os.environ.get("BORING_EVENT_DB_MIRROR", "").strip().lower()
    if legacy in {"0", "false", "no", "off"}:
        return "spool"
    if legacy in {"1", "true", "yes", "on"}:
        return "both"
    return "db"


def _event_spool_mode(db_enabled: bool) -> str:
    raw = os.environ.get("BORING_EVENT_SPOOL", "").strip().lower()
    if raw in {"always", "on_failure", "off"}:
        return raw
    if _event_sink_mode() == "both":
        return "always"
    if not db_enabled:
        return "always"
    return "on_failure"


def _fetch_engine_events(
    limit: int,
    component: str | None = None,
    event_name: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]] | None:
    url = _event_sink_url()
    if not url:
        return None
    params = {
        "limit": str(limit),
        "component": component,
        "event": event_name,
        "status": status,
    }
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    timeout = float(os.environ.get("BORING_EVENT_SINK_TIMEOUT") or "0.5")
    try:
        with urllib.request.urlopen(f"{url}?{query}", timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        print(f"[event-log] DB read failed: {e}", file=sys.stderr)
        return None
    entries = body.get("entries") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        return None
    return [_normalize_engine_event(entry) for entry in reversed(entries) if isinstance(entry, dict)]


def _normalize_engine_event(entry: dict[str, Any]) -> dict[str, Any]:
    event = {}
    attributes = entry.get("attributes")
    if isinstance(attributes, dict):
        event.update(attributes)
    event.update(entry)
    if "event_name" in event and "event" not in event:
        event["event"] = event["event_name"]
    if "observed_at" in event and "ts" not in event:
        event["ts"] = event["observed_at"]
    return event


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
    component: str | None = None,
    event_name: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    engine_events = _fetch_engine_events(limit, component, event_name, status)
    if engine_events is not None:
        return engine_events
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


#: The eval gate runs in CI on every push; a gate that stops is indistinguishable from a gate
#: that passes (`eval_graphrag_gate` emitted twenty `ok` rows, vanished five weeks, and its stale
#: rows were later read as proof the graph was covered). The local event log only sees a
#: hand-run `make eval-gate`, so freshness is read from the newest finished ci.yml run on main
#: (page of 20) whose job named `eval-gate` succeeded or failed — not the workflow conclusion,
#: which other jobs fail, and not a run cancelled by a newer push.
GATE_STALE_DAYS = int(os.environ.get("BORING_GATE_STALE_DAYS") or "7")
GATE_WORKFLOW = "ci.yml"
GATE_JOB = "eval-gate"
_GH_RUNS = f"repos/{{owner}}/{{repo}}/actions/workflows/{GATE_WORKFLOW}/runs?branch=main&status=completed&per_page=20"


@dataclass(frozen=True)
class GateFresh:
    days: int


@dataclass(frozen=True)
class GateStale:
    days: int


@dataclass(frozen=True)
class GateFailed:
    conclusion: str
    days: int


@dataclass(frozen=True)
class GateUnmeasurable:
    reason: str


GateState = GateFresh | GateStale | GateFailed | GateUnmeasurable

#: `gh` argv tail -> (returncode, output). The seam tests replace; production shells out.
GhCall = Callable[[list[str]], tuple[int, str]]


def _run_gh(args: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stdout if done.returncode == 0 else done.stderr.strip()


def _gh_json(gh: GhCall, args: list[str]) -> tuple[Any, str | None]:
    code, out = gh(args)
    if code != 0:
        return None, f"gh failed ({code}): {out[:120]}"
    try:
        return json.loads(out), None
    except json.JSONDecodeError:
        return None, "gh output is not JSON"


#: A job that ended any other way (cancelled by a newer push's concurrency group, skipped) is a
#: superseded run, not a gate result — the walk passes over it to an older run.
_DECISIVE = ("success", "failure")


def _parse_gate_job(jobs: Any, now: datetime, limit: int) -> GateState | None:
    """None = this run says nothing about the gate; keep walking."""
    listed = jobs.get("jobs") if isinstance(jobs, dict) else None
    rows = [j for j in listed or [] if isinstance(j, dict) and j.get("name") == GATE_JOB]
    if not rows or str(rows[0].get("conclusion") or "") not in _DECISIVE:
        return None
    done = _parse_ts(str(rows[0].get("completed_at") or ""))
    if done is None:
        return GateUnmeasurable(f"job {GATE_JOB} has no completed_at")
    days = (now - done).days
    if rows[0]["conclusion"] != "success":
        return GateFailed(rows[0]["conclusion"], days)
    return GateStale(days) if days > limit else GateFresh(days)


def eval_gate_state(
    gh: GhCall = _run_gh, now: datetime | None = None, max_days: int | None = None
) -> GateState:
    """Newest finished ci.yml runs on main -> first whose `eval-gate` job succeeded or failed."""
    limit = GATE_STALE_DAYS if max_days is None else max_days
    runs, err = _gh_json(gh, ["api", _GH_RUNS])
    if err is not None:
        return GateUnmeasurable(err)
    found = runs.get("workflow_runs") if isinstance(runs, dict) else None
    ids = [r["id"] for r in found or [] if isinstance(r, dict) and r.get("id")]
    for run_id in ids:
        jobs, err = _gh_json(gh, ["api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs"])
        if err is not None:
            return GateUnmeasurable(err)
        state = _parse_gate_job(jobs, now or datetime.now(UTC), limit)
        if state is not None:
            return state
    return GateUnmeasurable(f"no run of the last {len(ids)} has a finished {GATE_JOB} job")


def gate_line(state: GateState) -> tuple[bool, str]:
    """Fold a GateState into (ok, line)."""
    match state:
        case GateFresh(days):
            return True, f"gate={GATE_JOB} fresh ({days}d ago, <= {GATE_STALE_DAYS}d)"
        case GateStale(days):
            return False, f"gate={GATE_JOB} stale: last success {days}d ago (> {GATE_STALE_DAYS}d)"
        case GateFailed(conclusion, days):
            return False, f"gate={GATE_JOB} {conclusion} in latest main run ({days}d ago)"
        case GateUnmeasurable(reason):
            return False, f"gate={GATE_JOB} 못 잼 (unmeasurable): {reason}"


def recent_resolution_failures(limit: int = 3, hours: int | None = None) -> list[dict[str, Any]]:
    if hours is None:
        raw_hours = os.environ.get("BORING_EVENT_RECENT_HOURS") or str(DEFAULT_RECENT_HOURS)
        hours = int(raw_hours)
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    latest_by_key: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    anonymous_failures: list[tuple[int, dict[str, Any]]] = []
    events = _fetch_engine_events(1000, event_name="distill_resolution")
    if events is None:
        events = iter_events()
    for idx, event in enumerate(events):
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
        (idx, event) for idx, event in latest_by_key.values() if _is_resolution_failure(event)
    ]
    failures.sort(key=lambda item: item[0])
    return [event for _, event in failures[-limit:]]


def _resolution_event_key(event: dict[str, Any]) -> tuple[str, str] | None:
    session = event.get("session_id") or event.get("run_id")
    if not session:
        return None
    return (str(session), str(event.get("resolution") or ""))


def _is_resolution_failure(event: dict[str, Any]) -> bool:
    return event.get("verifier_status") == "failed" or event.get("status") == "failed"


def verdict_spool_loss() -> dict[str, Any]:
    """Spool rows of verdict-read kinds, split by the verdict's open window.

    The event names are loaded from scripts/uptake-verdict.py and the window bounds from
    verdict_core, the same places the verdict reads them, so this check cannot drift from
    what it guards. Timestamps are compared as instants on the window's calendar, not as
    strings: the spool writes ts with an offset, and a lexical compare silently reorders
    a row written with a different one. A row whose timestamp will not parse cannot be
    placed inside the window and is counted outside it, the side the open verdict cannot
    read; guessing would place it wherever the guess is convenient.
    """
    repo_root = Path(__file__).resolve().parents[3]
    shared_dir = str(repo_root / "agents" / "shared")
    if shared_dir not in sys.path:
        sys.path.insert(0, shared_dir)
    import verdict_core

    verdict_script = str(repo_root / "scripts" / "uptake-verdict.py")
    spec = importlib.util.spec_from_file_location("uptake_verdict", verdict_script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    since = datetime.strptime(verdict_core.WINDOW_SINCE, "%Y-%m-%d").replace(tzinfo=verdict_core.WINDOW_TZ)
    until = datetime.strptime(verdict_core.WINDOW_UNTIL, "%Y-%m-%d").replace(tzinfo=verdict_core.WINDOW_TZ)
    in_rows = []
    out_rows = 0
    oldest = None
    for row in iter_events():
        if row.get("event") not in module.VERDICT_EVENT_NAMES:
            continue
        at = verdict_core._instant(row.get("ts"))
        if at is not None and since <= at < until:
            in_rows.append(row)
            continue
        out_rows += 1
        if at is not None and (oldest is None or at < oldest):
            oldest = at
    sessions = {row.get("session_id") or (row.get("attributes") or {}).get("session_id") for row in in_rows}
    sessions.discard(None)
    sessions.discard("")
    return {
        "in_rows": len(in_rows),
        "in_sessions": len(sessions),
        "out_rows": out_rows,
        "oldest": oldest.astimezone(verdict_core.WINDOW_TZ).date().isoformat() if oldest else "undated",
        "since": verdict_core.WINDOW_SINCE,
        "until": verdict_core.WINDOW_UNTIL,
    }


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect oh-my-boring workflow events")
    parser.add_argument("--sink-mode", action="store_true")
    parser.add_argument("--verdict-spool-loss", action="store_true")
    parser.add_argument("--recent-resolution-failures", action="store_true")
    parser.add_argument("--stale-gates", action="store_true")
    parser.add_argument(
        "--replay-spool",
        action="store_true",
        help="hand spooled rows to the engine; rows it still refuses stay in the spool",
    )
    parser.add_argument("--record", nargs=3, metavar=("COMPONENT", "EVENT", "STATUS"))
    parser.add_argument("--field", action="append", default=[], type=_parse_field)
    parser.add_argument("--tail", action="store_true")
    parser.add_argument("--component")
    parser.add_argument("--event")
    parser.add_argument("--status")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max", type=int, default=3)
    parser.add_argument("--hours", type=int, default=None)
    return parser


def _print_sink_mode(_args: argparse.Namespace) -> int:
    print(_event_sink_mode())
    return 0


def _print_verdict_spool_loss(_args: argparse.Namespace) -> int:
    loss = verdict_spool_loss()
    print(
        "verdict_spool "
        f"in_window_rows={loss['in_rows']} "
        f"in_window_sessions={loss['in_sessions']} "
        f"out_of_window_rows={loss['out_rows']} "
        f"oldest={loss['oldest']} "
        f"since={loss['since']} "
        f"until={loss['until']}"
    )
    return 0


def _record(args: argparse.Namespace) -> int:
    fields = dict(args.field)
    append_event(args.record[0], args.record[1], args.record[2], **fields)
    return 0


def _print_tail(args: argparse.Namespace) -> int:
    events = recent_events(args.max, args.component, args.event, args.status)
    for event in events:
        if args.json:
            print(json.dumps(event, ensure_ascii=False, sort_keys=True))
        else:
            print(_format_event(event))
    return 0


def _print_stale_gates(_args: argparse.Namespace) -> int:
    ok, line = gate_line(eval_gate_state())
    print(line)
    return 0 if ok else 1


def _print_replay_spool(_args: argparse.Namespace) -> int:
    counts = replay_spool()
    print(
        f"spool replay: replayed={counts['replayed']} kept={counts['kept']} "
        f"unparseable={counts['unparseable']} spool={event_log_path()}"
    )
    return 0 if counts["kept"] == 0 and counts["unparseable"] == 0 else 1


def _print_recent_resolution_failures(args: argparse.Namespace) -> int:
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


_COMMANDS = (
    ("sink_mode", _print_sink_mode),
    ("verdict_spool_loss", _print_verdict_spool_loss),
    ("record", _record),
    ("tail", _print_tail),
    ("stale_gates", _print_stale_gates),
    ("replay_spool", _print_replay_spool),
    ("recent_resolution_failures", _print_recent_resolution_failures),
)


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    for flag, command in _COMMANDS:
        if getattr(args, flag):
            return command(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
