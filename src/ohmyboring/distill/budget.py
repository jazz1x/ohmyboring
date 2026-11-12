"""하루 증류 지출 판정 — 사건 목록에서 오늘 쓴 USD 를 세어 Within / Exceeded / Unmeasured 로 접는다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import Any

from ohmyboring.config import DistillBudget


@dataclass(frozen=True)
class Within:
    spent: float


@dataclass(frozen=True)
class Exceeded:
    spent: float
    limit: float


@dataclass(frozen=True)
class Unmeasured:
    reason: str


BudgetState = Within | Exceeded | Unmeasured


def _event_day(event: dict[str, Any], tz: tzinfo) -> date | None:
    try:
        return datetime.fromisoformat(str(event.get("ts"))).astimezone(tz).date()
    except ValueError:
        return None


def _cost(event: dict[str, Any], budget: DistillBudget) -> float:
    # Events written before the token fields existed carry none: 0 tokens, not an error.
    tokens_in = event.get("llm_input_tokens") or 0
    tokens_out = event.get("llm_output_tokens") or 0
    return (tokens_in * budget.price_in_per_mtok + tokens_out * budget.price_out_per_mtok) / 1_000_000


def _fold(spent: float, budget: DistillBudget) -> BudgetState:
    if spent >= budget.daily_usd:
        return Exceeded(spent, budget.daily_usd)
    return Within(spent)


def judge_day_budget(
    events: list[dict[str, Any]], day: date, tz: tzinfo, budget: DistillBudget
) -> BudgetState:
    """events: `distill_resolution` rows. A row whose `ts` cannot be placed on a day is Unmeasured."""
    days = [_event_day(event, tz) for event in events]
    if any(d is None for d in days):
        return Unmeasured("event with unparseable ts")
    spent = sum(_cost(event, budget) for event, d in zip(events, days, strict=True) if d == day)
    return _fold(spent, budget)
