from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict


@dataclass(frozen=True)
class AlreadyPosted:
    ts: str


@dataclass(frozen=True)
class NothingToSay:
    pass


@dataclass(frozen=True)
class GenerationFailed:
    reason: str


@dataclass(frozen=True)
class TimedOut:
    seconds: float


@dataclass(frozen=True)
class PostFailed:
    reason: str


@dataclass(frozen=True)
class Posted:
    ts: str
    record_error: str | None


Outcome = AlreadyPosted | NothingToSay | GenerationFailed | TimedOut | PostFailed | Posted


class WeeklyState(TypedDict, total=False):
    deliver: bool
    fmt: str
    now: datetime
    engine_url: str
    vault_dir: str
    split_frontmatter: Callable[[str], tuple[str, str] | None]
    days: list[tuple[str, Any]]
    projects: dict[str, Any]
    answer: str
    sources: list[Any]
    stdout: str
    payload: dict[str, Any]
    ts: str
    outcome: Outcome
