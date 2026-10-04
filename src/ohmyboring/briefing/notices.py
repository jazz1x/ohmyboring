"""알림 문구 — 판정 표본과 사람 라벨이 모자랄 때 브리핑 끝에 붙는 한 줄."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

# 측정 계약 상수(verdict_core·label_core)는 agents/shared 에 한 벌만 있다. 호스트에서는 저장소
# 옆 디렉터리를, hermes 와 문 이미지에서는 이미 sys.path 에 있는 것을 쓴다.
_SHARED_DIR = Path(__file__).resolve().parents[3] / "agents" / "shared"
if _SHARED_DIR.is_dir() and str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

#: The window's final stretch, after which the ask runs every morning: three days is enough to sit
#: down for the minute it takes and not so long that daily becomes the nag it replaced.
_AUDIT_LAST_CALL_DAYS = 3


def _audit_last_call():
    import verdict_core

    close = datetime.fromisoformat(verdict_core.WINDOW_UNTIL)
    return (close - timedelta(days=_AUDIT_LAST_CALL_DAYS - 1)).date().isoformat()


def _is_monday(stamp: str) -> bool:
    try:
        return datetime.strptime(stamp, "%Y-%m-%d").weekday() == 0
    except ValueError:
        # An unparseable date is not a reason to fall silent about work the verdict is waiting on.
        return True


def audit_notice(label_stats) -> str:
    """One line asking for the human labels the verdict is waiting on, or "" when it is not.

    The measurement window closes whether or not anyone audits, and the LLM judge accruing 24 a
    night makes the ledger *look* healthy while the figure that decides anything stays
    uncomputable. Naming the shortfall where the reader already looks every morning is the
    cheapest way to keep a two-week window from ending in "판단 보류".

    Silent once the floor is met: a standing nag is a line readers learn to skip, and the next
    thing they skip is a real one. Silent too when the caller could not read the counts at all --
    an unreachable endpoint is not evidence that the whole floor is outstanding, and printing the
    maximum backlog because we know nothing would be a number the reader cannot act on.
    """
    if label_stats is None:
        return ""
    import label_core
    import verdict_core

    owed = label_core.audit_backlog(label_stats)
    if not owed:
        return ""
    today = verdict_core.window_today()
    if today > verdict_core.WINDOW_UNTIL:
        # Past the window the figure this unblocks can no longer be computed, so the ask is spent.
        # Without this the line outlives the thing it was asking for.
        return ""
    if today < _audit_last_call() and not _is_monday(today):
        # The count only moves when a person sits down for a minute, so on the days nobody did it
        # says exactly what it said yesterday. It stood at 20 in all 8 briefings actually sent,
        # 9 days running -- which is how a line teaches the reader to skip that part of the
        # message, and the next thing they skip is one that mattered. Weekly until the last few
        # days, then daily, because by then the ask has a deadline behind it.
        return ""
    return f"📋 판정 대기 — 사람 라벨 {owed}건 더 필요 · `label-recall.py --audit`"


def window_notice(uptake_stats) -> str:
    """How far the injection-channel sample has come, or "" when there is nothing to say.

    The verdict runs on counts that only move when sessions *end*, and sessions here run for
    days — so the floor can sit still for a week while the ledger looks busy, and nobody would
    know until the window closed on a refusal. Reported daily, a floor that stops tracking is
    visible while there is still time to do something about it.

    Silent once both floors are met: at that point the number to look at is the verdict, not the
    sample, and `scripts/uptake-verdict.py` prints that.
    """
    if not uptake_stats:
        return ""
    import verdict_core

    sessions = int(uptake_stats.get("sessions") or 0)
    prompts = int(uptake_stats.get("total_prompts") or 0)
    if sessions >= verdict_core.MIN_SESSIONS and prompts >= verdict_core.MIN_INJECTED_PROMPTS:
        return ""
    line = (
        f"📐 주입 채널 판정 표본 — 세션 {sessions}/{verdict_core.MIN_SESSIONS} · "
        f"프롬프트 {prompts}/{verdict_core.MIN_INJECTED_PROMPTS} · `uptake-verdict.py`"
    )
    # The midpoint gate rides the one channel that reaches a person every morning. doctor carries
    # the same check, but doctor is not in any crontab — measured 2026-09-02 — so a gate that
    # fires once, on a date six days out, and only under a command nobody runs is a dead letter.
    # This does not recompute it: same constants, and the shortfall says which adapter.
    today = verdict_core.window_today()
    if verdict_core.MIDPOINT <= today <= verdict_core.WINDOW_UNTIL:
        floor = verdict_core.MIDPOINT_MIN_SCORED
        if sessions < floor:
            line += (
                f"\n⚠️ 중간점({verdict_core.MIDPOINT}) 미달 — 채점 세션 {sessions}/{floor}."
                " PRD §2 는 창 종료를 기다리지 말고 계측 조사로 전환하도록 등록했다"
            )
    return line
