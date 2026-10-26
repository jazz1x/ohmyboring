"""문 배경 스레드 — DOOR_BRIEF_OWNER=python 일 때 BORING_BRIEF_HOUR(로컬 시각)마다 일일 브리프 1회.

scheduler.rs:267 sleep_until_next_local_hour 와 같다: 기동 직후에는 돌지 않고 다음 그 시각을 기다린다.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from ohmyboring.briefing.daily import BriefDeps, run_once
from ohmyboring.result import Either, Err, Ok

DEFAULT_HOUR = 8


def parse_hour(raw: str | None) -> Either[int, str]:
    """BORING_BRIEF_HOUR — 비면 8(엔진과 같은 기본), 0..=23 만."""
    if raw is None or not raw.strip():
        return Ok(DEFAULT_HOUR)
    try:
        hour = int(raw)
    except ValueError:
        return Err(f"BORING_BRIEF_HOUR must be an integer 0..=23, got {raw!r}")
    if not 0 <= hour <= 23:
        return Err(f"BORING_BRIEF_HOUR must be 0..=23, got {raw!r}")
    return Ok(hour)


def seconds_until_next(now: datetime, hour: int) -> float:
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def loop(deps: BriefDeps, hour: int, stop: threading.Event) -> None:
    while not stop.wait(seconds_until_next(deps.clock(), hour)):
        run_once(deps)


def start(deps: BriefDeps, hour: int, stop: threading.Event | None = None) -> threading.Thread:
    thread = threading.Thread(
        target=loop, args=(deps, hour, stop or threading.Event()), name="door-brief", daemon=True
    )
    thread.start()
    return thread
