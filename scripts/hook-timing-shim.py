#!/usr/bin/env python3
"""Run a prompt hook unchanged and log where its time goes — C-HOOKTIME1 measurement.

settings.json calls this instead of the hook, with BORING_HOOK_T0_NS (epoch ns the shell took
right before exec, zsh $(( epochtime[1]*1000000000 + epochtime[2] ))) and BORING_HOOK_NAME. It writes a start line, then an end line, to
BORING_HOOK_TIMING_LOG (default ~/.local/state/ohmyboring/hook-timing.ndjson). A start line
with no end line is a run the 10s timeout killed. stdout/stdin/exit code are the hook's own.

Delete this file once the measurement window closes (10 timeouts or 3 days).
"""

from __future__ import annotations

import http.client
import json
import os
import runpy
import sys
import time
import uuid
from pathlib import Path

_START_NS = time.time_ns()
_LOG = Path(
    os.environ.get("BORING_HOOK_TIMING_LOG", "~/.local/state/ohmyboring/hook-timing.ndjson")
).expanduser()
_RUN = uuid.uuid4().hex[:12]
_spent = {"connect": 0, "response": 0, "calls": 0}


def _ms(ns: int) -> float:
    return round(ns / 1e6, 1)


def _write(row: dict) -> None:
    # The hook must run even when the log cannot be written; a lost row is the only cost.
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with _LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"hook-timing-shim: log not written: {exc}", file=sys.stderr)


def _timed(name: str, original):
    def wrapper(self, *args, **kwargs):
        began = time.perf_counter_ns()
        try:
            return original(self, *args, **kwargs)
        finally:
            _spent[name] += time.perf_counter_ns() - began
            _spent["calls"] += name == "response"

    return wrapper


http.client.HTTPConnection.connect = _timed("connect", http.client.HTTPConnection.connect)
http.client.HTTPConnection.getresponse = _timed("response", http.client.HTTPConnection.getresponse)


def _end_row(import_ns: int, exit_code: object) -> dict:
    load1, load5, load15 = os.getloadavg()
    total_ns = time.time_ns() - _START_NS
    return {
        "phase": "end",
        "run": _RUN,
        "hook": os.environ.get("BORING_HOOK_NAME", ""),
        "import_ms": _ms(import_ns),
        "connect_ms": _ms(_spent["connect"]),
        "search_ms": _ms(_spent["response"]),
        "http_calls": _spent["calls"],
        "after_start_ms": _ms(total_ns),
        "exit": exit_code if isinstance(exit_code, int) else str(exit_code),
        "load1": load1,
        "load5": load5,
        "load15": load15,
    }


def main() -> None:
    hook = sys.argv[1]
    t0_ns = os.environ.get("BORING_HOOK_T0_NS", "")
    start_wait = _ms(_START_NS - int(t0_ns)) if t0_ns.isdigit() else None
    _write(
        {
            "phase": "start",
            "run": _RUN,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(_START_NS / 1e9)) + "Z",
            "hook": os.environ.get("BORING_HOOK_NAME", ""),
            "start_wait_ms": start_wait,
            "load1": os.getloadavg()[0],
        }
    )
    sys.argv = sys.argv[1:]
    began = time.perf_counter_ns()
    module = runpy.run_path(hook, run_name="__hook__")
    import_ns = time.perf_counter_ns() - began
    exit_code: object = 0
    try:
        module["main"]()
    except SystemExit as exc:
        exit_code = exc.code
        raise
    finally:
        row = _end_row(import_ns, exit_code)
        if start_wait is not None:
            row["total_ms"] = round(start_wait + row["after_start_ms"], 1)
        _write(row)


if __name__ == "__main__":
    main()
