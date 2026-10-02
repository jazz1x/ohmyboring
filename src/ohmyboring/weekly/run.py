"""주간 브리핑 입구 — 환경을 경계에서 한 번 읽어 그래프에 태우고, 결과를 종료 코드로 접는다."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from ohmyboring.result import Err, Ok
from ohmyboring.weekly.graph import graph
from ohmyboring.weekly.report import Report, one_line, report_of
from ohmyboring.weekly.stamp import KST
from ohmyboring.weekly.state import GenerationFailed, TimedOut, WeeklyState

#: The weekly's engine fallback sits on a 180s timeout of its own; give the run room and no
#: more, so a hung engine becomes a failure notice instead of a silent Monday.
GENEROUS_TIMEOUT_S = 300

SplitFrontmatter = Callable[[str], tuple[str, str] | None]


def initial_state(
    environ: Mapping[str, str],
    now: datetime,
    split_frontmatter: SplitFrontmatter,
    *,
    fmt: str,
    deliver: bool,
) -> WeeklyState:
    home = environ.get("BORING_HOME") or os.path.expanduser("~/oh-my-boring")
    return {
        "deliver": deliver,
        "fmt": fmt,
        "now": now,
        # E4-α — /weekly 도 문 하나를 지난다. config.door_url 의 규칙을 environ 맵으로 옮긴 것
        # (주간 그래프는 주입된 environ 만 읽는다): BORING_DOOR_URL → hermes·문 컨테이너의
        # boring-door:7710. 문은 /weekly 를 엔진에 바이트 그대로 넘긴다.
        "door_url": environ.get("BORING_DOOR_URL") or "http://boring-door:7710",
        "vault_dir": environ.get("BORING_VAULT_DIR") or os.path.join(home, "vault"),
        "split_frontmatter": split_frontmatter,
    }


def format_of(environ: Mapping[str, str]) -> str:
    return "blocks" if environ.get("BORING_BRIEFING_FORMAT", "").strip().lower() == "blocks" else "text"


def render_week(environ: Mapping[str, str], split_frontmatter: SplitFrontmatter) -> str:
    """The weekly briefing exactly as the CLI prints it — nothing is posted."""
    state = initial_state(
        environ, datetime.now(KST), split_frontmatter, fmt=format_of(environ), deliver=False
    )
    return graph.invoke(state)["stdout"]


def _invoke_within(
    state: WeeklyState, seconds: float
) -> Ok[dict[str, Any]] | Err[TimedOut | GenerationFailed]:
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["final"] = graph.invoke(state)
        except Exception as e:  # noqa: BLE001 — the run's failure becomes a one-line notice
            box["error"] = e

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        return Err(TimedOut(seconds))
    if "error" in box:
        return Err(GenerationFailed(one_line(f"{type(box['error']).__name__}: {box['error']}")))
    return Ok(box["final"])


def deliver_week(
    environ: Mapping[str, str], split_frontmatter: SplitFrontmatter, *, now: datetime | None = None
) -> Report:
    """Run the weekly through to Slack; the report is the exit code and the lines to print."""
    state = initial_state(environ, now or datetime.now(KST), split_frontmatter, fmt="blocks", deliver=True)
    match _invoke_within(state, GENEROUS_TIMEOUT_S):
        case Ok(final):
            return report_of(final["outcome"])
        case Err(failure):
            return report_of(failure)
