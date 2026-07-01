#!/usr/bin/env python3
"""아침 브리핑 — ohmyboring RAG 회수·합성을 stdout 으로.

hermes-agent cron --no-agent --script 로 호출 → stdout 이 그대로 Slack DM 등으로 배달.
지능은 ohmyboring 엔진이 SSOT. 이 스크립트는 호출+포맷만 담당.
의존성 0 (stdlib urllib). 실패는 침묵하지 않는다(ROP: 실패는 보인다).
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

from slack_briefing import (
    maybe_print_blocks_json,
    render_body_mrkdwn,
    render_message_mrkdwn,
)

# BORING_URL is the canonical env var used throughout oh-my-boring.
# DRUDGE_URL is kept as a fallback for legacy scripts only.
HERMES_URL = os.environ.get("BORING_URL") or os.environ.get(
    "DRUDGE_URL", "http://boring-drudge:7700"
)
KST = timezone(timedelta(hours=9))
DATE = datetime.now(KST).strftime("%Y-%m-%d %a")
TITLE = "☀️ 아침 브리핑"
EMPTY_MESSAGE = "오늘은 새로 짚을 진행/막힘 항목이 회수되지 않았어요."


def header(body: str) -> str:
    return f"*{TITLE}*\n`{DATE}`\n\n{body}"


def slack_mrkdwn(answer: str) -> str:
    return render_body_mrkdwn(answer)


def main() -> None:
    req = urllib.request.Request(
        f"{HERMES_URL}/brief",
        data=b"{}",
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(header(f"⚠️ ohmyboring(RAG) 응답 없음 — 엔진 가동 확인 필요. ({e})"))
        return
    except json.JSONDecodeError:
        print(header("⚠️ 응답 파싱 실패 — ohmyboring 점검 필요."))
        return

    answer = (data.get("answer") or "").strip()
    sources = data.get("sources") or []
    if not answer:
        print(header(EMPTY_MESSAGE))
        return
    if maybe_print_blocks_json(TITLE, DATE, answer, sources, EMPTY_MESSAGE):
        return
    print(render_message_mrkdwn(f"*{TITLE}*", DATE, answer, sources, EMPTY_MESSAGE))


if __name__ == "__main__":
    main()
    sys.exit(0)
