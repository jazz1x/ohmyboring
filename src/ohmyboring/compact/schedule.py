"""문 배경 스레드 — DOOR_COMPACT_OWNER=python 일 때 schedule.compact_hours 마다 compact 한 회차.

기동 직후에는 돌지 않는다 — 엔진의 「주기가 지난 뒤 첫 틱」과 같다(첫 회차는 한 주기 뒤).
"""

from __future__ import annotations

import threading

from ohmyboring.compact.run import CompactDeps, run_once


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
