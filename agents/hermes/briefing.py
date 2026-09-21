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
from datetime import datetime, timedelta, timezone

# After slack_briefing on purpose: in ~/.hermes/scripts every module sits flat and the order is
# irrelevant, but running from the repo it is slack_briefing that puts agents/shared on the path.
import verdict_core  # noqa: E402
from slack_briefing import (
    maybe_print_blocks_json,
    render_body_mrkdwn,
    render_message_mrkdwn,
)

# BORING_URL is the canonical env var used throughout oh-my-boring.
# DRUDGE_URL is kept as a fallback for legacy scripts only.
HERMES_URL = os.environ.get("BORING_URL") or os.environ.get("DRUDGE_URL", "http://boring-drudge:7700")
KST = timezone(timedelta(hours=9))
DATE = datetime.now(KST).strftime("%Y-%m-%d %a")
TITLE = "☀️ 아침 브리핑"
EMPTY_MESSAGE = "오늘은 새로 짚을 진행/막힘 항목이 회수되지 않았어요."


def header(body: str) -> str:
    return f"*{TITLE}*\n`{DATE}`\n\n{body}"


def label_stats() -> dict | None:
    """Label counts for the audit nudge, or None when the engine will not say.

    Secondary to the briefing: a reader whose morning summary died because a side metric was
    unreachable lost the thing they came for. None omits the line -- which is not the same as
    printing a zero backlog, and the renderer treats it as "unknown", never as "done".
    """
    try:
        with urllib.request.urlopen(f"{HERMES_URL}/recall-label-stats", timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def known_projects() -> list | None:
    """Project names the corpus has documents for, or None when the engine will not say.

    None and [] are different answers and the renderer treats them differently: None means the
    question could not be asked, so headings fall back to the shape test; [] would mean the corpus
    genuinely holds no projects. Returning [] on a failed fetch is how an unreachable engine starts
    stripping the project off every item in the briefing.
    """
    try:
        with urllib.request.urlopen(f"{HERMES_URL}/projects", timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    names = payload.get("projects")
    return names if isinstance(names, list) else None


def slack_mrkdwn(answer: str) -> str:
    return render_body_mrkdwn(answer)


def uptake_stats() -> dict | None:
    """Sample size for the injection-channel window, or None when the engine will not say.

    Folded here rather than in the renderer so the renderer stays pure. None omits the line,
    which is not the same as reporting a sample of zero — an unreachable engine says nothing
    about how far the window has come.
    """
    try:
        with urllib.request.urlopen(f"{HERMES_URL}/events?limit=5000", timeout=20) as resp:
            rows = json.loads(resp.read().decode("utf-8")).get("entries") or []
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    # The verdict's own population, not a neighbouring one. Two corrections, both measured on
    # 2026-09-02 against the live store:
    #
    # - **Post-repair only.** The owner chose to keep the window and split the sample at the
    #   ledger repair (§8 D4); rows from before it are reported separately and never judged.
    #   Pooled across the whole store this line said 14 sessions where the verdict sees 3.
    # - **Per adapter, minimum.** The floors are per-agent because the adapters run different
    #   products, so summing them clears a floor neither one clears. With the midpoint gate riding
    #   this number, a pooled 14 would have stayed silent on 09-08 while the gate said "short".
    _pre, post = verdict_core.partition_at_repair(rows)
    per_agent, _skipped = verdict_core.collect(post)
    if not per_agent:
        return {"sessions": 0, "total_prompts": 0}
    return {
        "sessions": min(c["sessions"] for c in per_agent.values()),
        "total_prompts": min(c["total_prompts"] for c in per_agent.values()),
    }


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
    stats = label_stats()
    window = uptake_stats()
    projects = known_projects()
    if maybe_print_blocks_json(TITLE, DATE, answer, sources, EMPTY_MESSAGE, stats, window, projects):
        return
    print(render_message_mrkdwn(f"*{TITLE}*", DATE, answer, sources, EMPTY_MESSAGE, stats, window, projects))


if __name__ == "__main__":
    main()
    sys.exit(0)
