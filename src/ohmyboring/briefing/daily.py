"""일일 브리프 노트 한 회차 — drudge scheduler.rs run_brief 의 이식(파일이 있으면 쓰지 않는다).

결정(오늘 날짜·파일명·frontmatter)은 순수 함수, 효과(DB 읽기·LLM·파일 쓰기·사건)는 run_once 한 곳.
파일 쓰기와 DB 읽기뿐이라 sync·compact 의 run_lock 은 쓰지 않는다 — 엔진 브리프도 sync 잠금 밖에서 돌았다.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ohmyboring.briefing import generate
from ohmyboring.result import Either, Err, Ok

COMPONENT = "door-brief"
EVENT = "brief_run"
Status = Literal["written", "exists", "empty", "error"]


@dataclass(frozen=True)
class BriefDeps:
    connect: Callable[[], AbstractContextManager[Any]]
    generate: generate.Generate
    lang: str
    wiki_dir: str
    append_event: Callable[..., bool]
    clock: Callable[[], datetime]


@dataclass(frozen=True)
class BriefReport:
    status: Status
    path: str
    window_hours: int | None
    docs: int
    sources: int
    bytes: int
    ms: int

    def event_fields(self) -> dict[str, Any]:
        return {
            "window_hours": self.window_hours,
            "docs": self.docs,
            "sources": self.sources,
            "bytes": self.bytes,
            "ms": self.ms,
            "path": self.path,
        }


def note_path(wiki_dir: str, date: str) -> str:
    return os.path.join(wiki_dir, f"daily-brief-{date}.md")


def render_note(date: str, out: generate.BriefOut) -> str:
    """scheduler.rs:246-260 — brief_sources 는 `sources:` 축(거버넌스 대상)과 따로 둔 칸이다."""
    block = "".join(f"  - {s.replace(chr(34), chr(39))}\n" for s in out.sources)
    sources = f"brief_sources:\n{block}" if block else ""
    frontmatter = (
        f'---\ntitle: "Daily Brief — {date}"\norigin: personal\ndate: {date}\nkind: note\n'
        f"tags: [daily-brief]\n{sources}---\n\n"
    )
    return f"{frontmatter}{out.answer}\n"


def _report(
    status: Status, path: str, started: float, out: generate.BriefOut | None, size: int
) -> BriefReport:
    return BriefReport(
        status=status,
        path=path,
        window_hours=out.window_hours if out else None,
        docs=out.docs if out else 0,
        sources=len(out.sources) if out else 0,
        bytes=size,
        ms=int((time.monotonic() - started) * 1000),
    )


def run_brief(
    deps: BriefDeps, date: str | None = None, as_of: datetime | None = None
) -> Either[BriefReport, str]:
    """고장은 값(Err)으로 돌려준다. 예외는 run_once 경계에서만 잡는다."""
    started = time.monotonic()
    now = deps.clock()
    day = date or now.strftime("%Y-%m-%d")
    path = note_path(deps.wiki_dir, day)
    if os.path.exists(path):
        return Ok(_report("exists", path, started, None, os.path.getsize(path)))
    with deps.connect() as conn:
        match generate.brief(conn, deps.generate, deps.lang, as_of or now):
            case Err(reason):
                return Err(reason)
            case Ok(out):
                pass
    os.makedirs(deps.wiki_dir, exist_ok=True)
    content = render_note(day, out)
    with open(path, "x", encoding="utf-8") as f:
        f.write(content)
    status: Status = "empty" if out.docs == 0 else "written"
    return Ok(_report(status, path, started, out, len(content.encode("utf-8"))))


def run_once(
    deps: BriefDeps, date: str | None = None, as_of: datetime | None = None
) -> Either[BriefReport, str]:
    """회차 경계 — 어떤 예외든 error 사건과 stderr 한 줄로 남기고 값으로 돌려준다."""
    try:
        result = run_brief(deps, date, as_of)
    except Exception as e:
        result = Err(f"{type(e).__name__}: {e}")
    match result:
        case Ok(report):
            deps.append_event(COMPONENT, EVENT, report.status, **report.event_fields())
        case Err(reason):
            print(f"[door-brief] run failed: {reason}", file=sys.stderr, flush=True)
            deps.append_event(COMPONENT, EVENT, "error", reason=reason)
    return result
