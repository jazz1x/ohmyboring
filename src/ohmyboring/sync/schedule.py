"""문 배경 스레드 — DOOR_SYNC_OWNER=python 일 때 기동 1회 + BORING_SYNC_HOURS(기본 4)마다 sync 한 회차.

engine 일 때는 이 스레드를 아예 띄우지 않는다(문이 start 를 부르지 않는다).
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from ohmyboring.result import Either, Err, Ok
from ohmyboring.sync.run import COMPONENT, EVENT, SyncDeps, run_sync

DEFAULT_HOURS = 4.0
#: sync·compact 가 프로세스를 넘어 한 번에 하나만 돌게 하는 PostgreSQL advisory 잠금 키 — Rust scheduler 의
#: sync_lock 과 같은 자리. 문 스레드와 `docker exec` 로 뜨는 compact_once 는 별개 프로세스라 DB 가 잠가야 한다.
RUN_LOCK_KEY = 0x6F6D6272756E  # "ombrun"


@contextmanager
def run_lock(connect: Callable[[], AbstractContextManager[Any]]) -> Iterator[None]:
    """세션 수준 advisory 잠금을 전용 연결 하나에 쥔다. 기다리는 쪽이 막힌다(엔진이 한 틱 루프에서 sync 뒤에
    compact 를 돌리는 것과 같다). 정상·예외 경로 모두 명시로 풀고 연결을 닫으면 서버가 어차피 푼다 —
    DB 가 안 닿으면 connect 가 올리는 예외가 그대로 그 회차의 실패다."""
    with connect() as conn:
        conn.autocommit = True
        conn.execute("SELECT pg_advisory_lock(%s)", (RUN_LOCK_KEY,))
        try:
            yield
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (RUN_LOCK_KEY,))


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
        with run_lock(deps.connect):
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
