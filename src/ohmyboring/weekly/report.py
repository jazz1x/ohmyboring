"""그래프가 남긴 결과를 종료 코드와 한 줄 알림으로 접는다 — 갈래를 한 자리에서 소진한다."""

from __future__ import annotations

from dataclasses import dataclass

from ohmyboring.weekly.state import (
    AlreadyPosted,
    GenerationFailed,
    NothingToSay,
    Outcome,
    Posted,
    PostFailed,
    TimedOut,
)


@dataclass(frozen=True)
class Report:
    code: int
    out: tuple[str, ...]
    err: tuple[str, ...]


def one_line(text: str) -> str:
    return " ".join(text.split())


def report_of(outcome: Outcome) -> Report:
    match outcome:
        case AlreadyPosted(ts):
            return Report(0, (f"[weekly] already posted this week (ts={ts})",), ())
        case NothingToSay():
            msg = "[weekly] 올릴 브리핑 없음 — 이번 주는 새로 짚을 진행/막힘 항목이 회수되지 않았어요"
            return Report(0, (msg,), ())
        case GenerationFailed(reason):
            return Report(3, (), (f"[weekly] 주간 브리핑 생성 실패: {reason}",))
        case TimedOut(seconds):
            return Report(3, (), (f"[weekly] 주간 브리핑 생성 시간 초과 ({seconds}초)",))
        case PostFailed(reason):
            return Report(1, (), (f"[weekly] 슬랙 전송 실패: {reason}",))
        case Posted(ts, record_error):
            return Report(0, (f"[weekly] posted ts={ts}",), () if record_error is None else (record_error,))
