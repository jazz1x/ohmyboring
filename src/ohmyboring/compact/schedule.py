"""문 배경 스레드 — DOOR_COMPACT_OWNER=python 일 때 BORING_COMPACT_HOURS(기본 24)마다 compact 한 회차.

기동 직후에는 돌지 않는다 — 엔진의 「주기가 지난 뒤 첫 틱」과 같다(첫 회차는 한 주기 뒤).
"""

from __future__ import annotations

import threading

from ohmyboring.compact.run import CompactDeps, run_once
from ohmyboring.result import Either, Err, Ok

DEFAULT_HOURS = 24.0


def parse_hours(raw: str | None) -> Either[float, str]:
    """BORING_COMPACT_HOURS — 비면 24, 양수만."""
    if raw is None or not raw.strip():
        return Ok(DEFAULT_HOURS)
    try:
        hours = float(raw)
    except ValueError:
        return Err(f"BORING_COMPACT_HOURS must be a number, got {raw!r}")
    if hours <= 0:
        return Err(f"BORING_COMPACT_HOURS must be > 0, got {raw!r}")
    return Ok(hours)


def loop(deps: CompactDeps, interval_s: float, stop: threading.Event) -> None:
    while not stop.wait(interval_s):
        run_once(deps)


def start(deps: CompactDeps, hours: float, stop: threading.Event | None = None) -> threading.Thread:
    thread = threading.Thread(
        target=loop,
        args=(deps, hours * 3600, stop or threading.Event()),
        name="door-compact",
        daemon=True,
    )
    thread.start()
    return thread
