"""문 배경 스레드 — DOOR_SYNC_OWNER=python 일 때 기동 1회 + BORING_SYNC_HOURS(기본 4)마다 sync 한 회차.

engine 일 때는 이 스레드를 아예 띄우지 않는다(문이 start 를 부르지 않는다).
"""

from __future__ import annotations

import sys
import threading

from ohmyboring.result import Either, Err, Ok
from ohmyboring.sync.run import COMPONENT, EVENT, SyncDeps, run_sync

DEFAULT_HOURS = 4.0


def parse_hours(raw: str | None) -> Either[float, str]:
    """BORING_SYNC_HOURS — 비면 4, 양수만(Rust 는 1 이상 정수; 스레드는 0 이하를 거절)."""
    if raw is None or not raw.strip():
        return Ok(DEFAULT_HOURS)
    try:
        hours = float(raw)
    except ValueError:
        return Err(f"BORING_SYNC_HOURS must be a number, got {raw!r}")
    if hours <= 0:
        return Err(f"BORING_SYNC_HOURS must be > 0, got {raw!r}")
    return Ok(hours)


def run_once(deps: SyncDeps) -> None:
    """한 회차 — 어떤 예외든 오류 사건(종류·메시지)과 stderr 한 줄로 남기고 스레드는 다음 회차를 기다린다.

    넓게 잡는 곳은 회차 경계 하나뿐이다(소유자 결정, Rust scheduler.rs run_sync 와 같은 계약) —
    노트별 코드 안에서는 잡지 않는다. 스레드가 말없이 죽어 sync_run 사건이 끊기던 것을 막는다.
    """
    try:
        run_sync(deps)
    except Exception as e:
        print(f"[door-sync] run failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        deps.append_event(COMPONENT, EVENT, "error", reason=f"{type(e).__name__}: {e}")


def loop(deps: SyncDeps, interval_s: float, stop: threading.Event) -> None:
    while True:
        run_once(deps)
        if stop.wait(interval_s):
            return


def start(deps: SyncDeps, hours: float, stop: threading.Event | None = None) -> threading.Thread:
    thread = threading.Thread(
        target=loop,
        args=(deps, hours * 3600, stop or threading.Event()),
        name="door-sync",
        daemon=True,
    )
    thread.start()
    return thread
