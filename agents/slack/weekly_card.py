#!/usr/bin/env python3
"""The weekly card — the weekly briefing, posted like the morning card.

hermes cron used to deliver the weekly: it wrapped the output in a "Cronjob Response" banner
and re-read our Slack mrkdwn as markdown, so `*bold*` arrived as italics. This poster is the
weekly's own process with the bot token — it runs `weekly-briefing.py` in blocks mode and
posts the Block Kit payload to SLACK_CARD_CHANNEL verbatim. No socket, no hermes wrapper;
launchd fires it Monday 09:00 (`scripts/schedule-card.sh install weekly`) and, since the
handover, hermes cron asks the door to fire it (`POST /run/weekly-card`) — one tool, two
schedulers, and the second one of the ISO week sees the first card's own `weekly_card`
event and exits 0 with one line instead of posting twice.

Exit codes: 0 posted — or nothing to say, one line and no post — or already posted this
week, one line and no post; 1 Slack refused the post; 2 SLACK_BOT_TOKEN/SLACK_CARD_CHANNEL
missing; 3 the weekly itself could not be produced.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "hermes"))
sys.path.insert(0, str(_HERE.parent / "shared"))
sys.path.insert(0, str(_HERE.parents[1] / "src"))

import slack_briefing  # noqa: E402
import slack_post  # noqa: E402

from ohmyboring.adapters import events as event_log  # noqa: E402

_KST = timezone(timedelta(hours=9))

WEEKLY_SCRIPT = Path(slack_briefing.__file__).resolve().parent / "weekly-briefing.py"
# The weekly's engine fallback sits on a 180s timeout of its own; give the child room and no
# more, so a hung engine becomes a failure notice instead of a silent Monday.
GENEROUS_TIMEOUT_S = 300
#: The poster's own ledger — one per posted weekly, the guard's signal (no new state file).
WEEKLY_EVENT = "weekly_card"


def one_line(text: str) -> str:
    return " ".join(text.split())


def this_week(now: datetime | None = None) -> str:
    """The ISO week label the briefing itself stamps (weekly-briefing.py, KST)."""
    return (now or datetime.now(_KST)).strftime("%G-W%V")


def _posted_this_week_ts(week: str) -> str | None:
    """The ts of the weekly already posted this ISO week, from the weekly_card events this
    poster records — or None when the week is still unposted. An unreadable event log is
    not a reason to skip a Monday: it just leaves the guard blind and the normal run will
    surface a real outage on its own."""
    newest: str | None = None
    for event in event_log.recent_events(20, event_name=WEEKLY_EVENT):
        if str(event.get("week") or "") == week and event.get("ts"):
            newest = str(event["ts"])  # oldest-first order: the last match is the newest
    return newest


def build_payload(stdout: str) -> dict[str, Any] | None:
    """The weekly's blocks-mode stdout as a postable payload.

    None means there is nothing to say this week — the briefing prints its empty message as
    plain text even in blocks mode. Any other non-JSON output is the briefing's own failure
    line (a dead engine, an unparseable response), raised as ValueError so the caller can
    quote it on stderr and let the scheduler DM the owner.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        if slack_briefing.EMPTY_MESSAGE in stdout:
            return None
        raise ValueError(one_line(stdout)) from None
    if not isinstance(payload, dict) or not isinstance(payload.get("blocks"), list):
        raise ValueError(f"blocks 페이로드 아님: {one_line(stdout)[:120]}")
    return payload


def main() -> int:
    if not os.environ.get("SLACK_BOT_TOKEN"):
        print("[weekly] SLACK_BOT_TOKEN must be set (see .env.example)", file=sys.stderr)
        return 2
    if not os.environ.get("SLACK_CARD_CHANNEL"):
        print(
            "[weekly] SLACK_CARD_CHANNEL must be set — the channel id the weekly card posts to "
            "(see .env.example)",
            file=sys.stderr,
        )
        return 2

    posted = _posted_this_week_ts(this_week())
    if posted is not None:
        # launchd and the hermes cron share the handover window; the second runner of the
        # ISO week sees the first one's card in the event log and stops — one weekly a week.
        print(f"[weekly] already posted this week (ts={posted})", flush=True)
        return 0

    try:
        completed = subprocess.run(
            [sys.executable, str(WEEKLY_SCRIPT)],
            env={**os.environ, "BORING_BRIEFING_FORMAT": "blocks"},
            capture_output=True,
            text=True,
            timeout=GENEROUS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(f"[weekly] 주간 브리핑 생성 시간 초과 ({GENEROUS_TIMEOUT_S}초)", file=sys.stderr)
        return 3
    if completed.returncode != 0:
        reason = one_line(completed.stderr) or f"exit {completed.returncode}"
        print(f"[weekly] 주간 브리핑 생성 실패: {reason}", file=sys.stderr)
        return 3
    try:
        payload = build_payload(completed.stdout)
    except ValueError as e:
        print(f"[weekly] 주간 브리핑 생성 실패: {e}", file=sys.stderr)
        return 3
    if payload is None:
        print("[weekly] 올릴 브리핑 없음 — 이번 주는 새로 짚을 진행/막힘 항목이 회수되지 않았어요")
        return 0
    try:
        ts = slack_post.post_payload(payload)
    except Exception as e:  # noqa: BLE001 — say why in one line, not with a stack trace
        print(f"[weekly] 슬랙 전송 실패: {one_line(str(e))}", file=sys.stderr)
        return 1
    try:
        # The guard's ledger: next runner of the same ISO week (the other scheduler during
        # the handover) reads this instead of posting a second weekly. The card is already
        # out — a failed write must not turn a success into a failure exit.
        event_log.append_event("slack-card", WEEKLY_EVENT, "ok", week=this_week(), ts=ts)
    except OSError as e:
        print(f"[weekly] {WEEKLY_EVENT} event not recorded: {one_line(str(e))}", file=sys.stderr)
    print(f"[weekly] posted ts={ts}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
