#!/usr/bin/env python3
"""주간 브리핑 — ohmyboring RAG 회수·합성을 stdout 으로.

hermes-agent cron --no-agent --script 로 호출 → stdout 이 그대로 Slack DM 등으로 배달.
지능은 ohmyboring 엔진이 SSOT. 이 스크립트는 호출+포맷만 담당.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from slack_briefing import (
    maybe_print_blocks_json,
    parse_brief,
    render_body_mrkdwn,
    render_message_mrkdwn,
    render_weekly_blocks,
    render_weekly_mrkdwn,
)
from vault_note import split_frontmatter
from weekly_trend import collect_week, label_trend, needs_intervention, scoreboard

HERMES_URL = os.environ.get("BORING_URL") or os.environ.get("DRUDGE_URL", "http://boring-drudge:7700")
KST = timezone(timedelta(hours=9))
# ISO week: YYYY-WNN
TODAY = datetime.now(KST)
WEEK = TODAY.strftime("%G-W%V")
DATE = TODAY.strftime("%Y-%m-%d %a")
TITLE = "📅 주간 브리핑"
STAMP = f"{WEEK} · {DATE}"
EMPTY_MESSAGE = "이번 주는 새로 짚을 진행/막힘 항목이 회수되지 않았어요."


def header(body: str) -> str:
    return f"*{TITLE}*\n`{STAMP}`\n\n{body}"


def slack_mrkdwn(answer: str) -> str:
    return render_body_mrkdwn(answer)


#: Where the daily briefings land. The weekly reads them instead of asking the engine to
#: re-summarise a week: a fresh synthesis produces new wording every time, so nothing can be
#: tracked across days (measured — 252 items over a week, adjacent-day overlap 0,0,0,2,0,0).
VAULT_WIKI = os.environ.get("BORING_VAULT_DIR") or os.path.join(
    os.environ.get("BORING_HOME") or os.path.expanduser("~/oh-my-boring"), "vault"
)
WINDOW_DAYS = 7


def read_week(today, span=WINDOW_DAYS, wiki_dir=None):
    """Parsed daily briefs for the window, oldest first. Missing days are simply absent.

    A missing day is not an error — the machine may have been off — but the count of days found
    is reported, because "5/7일" means something different when only five briefs exist.
    """
    root = os.path.join(wiki_dir or VAULT_WIKI, "wiki")
    out = []
    for back in range(span - 1, -1, -1):
        date = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        path = os.path.join(root, f"daily-brief-{date}.md")
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read()
        except OSError:
            continue
        split = split_frontmatter(raw)
        out.append((date, parse_brief(split[1] if split else raw)))
    return out


def print_trend_blocks(days) -> bool:
    """Emit the persistence/trend weekly. False when there is nothing to stand on."""
    if len(days) < 2:
        return False
    projects = collect_week(days)
    if not projects:
        return False
    span = len(days)
    intervention = [(w, label, count, span) for w, label, count in needs_intervention(projects)]
    board = [(w, span) for w in scoreboard(projects)]
    blocks = render_weekly_blocks(
        TITLE,
        f"{STAMP} · 스냅샷 {span}/{WINDOW_DAYS}일",
        projects,
        intervention,
        board,
        label_trend(days),
        [],
    )
    text = render_weekly_mrkdwn(
        TITLE,
        f"{STAMP} · 스냅샷 {span}/{WINDOW_DAYS}일",
        intervention,
        board,
        label_trend(days),
        [],
    )
    # Cron delivery is text-only, so text is the default and the JSON payload is the opt-in.
    # It was the other way round, which put this whole weekly behind an env var nothing sets.
    if os.environ.get("BORING_BRIEFING_FORMAT", "").strip().lower() == "blocks":
        print(
            json.dumps(
                {
                    "text": text,
                    "blocks": blocks,
                    "unfurl_links": False,
                    "unfurl_media": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(text)
    return True


def main() -> None:
    # The weekly's own signal comes from the daily artifacts, not from a fresh synthesis. Fall
    # through to the engine only when there are too few of them to say anything.
    if print_trend_blocks(read_week(TODAY)):
        return

    req = urllib.request.Request(
        f"{HERMES_URL}/weekly",
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
    if maybe_print_blocks_json(TITLE, STAMP, answer, sources, EMPTY_MESSAGE):
        return
    print(render_message_mrkdwn(f"*{TITLE}*", STAMP, answer, sources, EMPTY_MESSAGE))


if __name__ == "__main__":
    main()
    sys.exit(0)
