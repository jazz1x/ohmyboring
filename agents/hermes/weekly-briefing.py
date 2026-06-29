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
from datetime import datetime, timezone, timedelta

HERMES_URL = os.environ.get("BORING_URL") or os.environ.get(
    "DRUDGE_URL", "http://boring-drudge:7700"
)
KST = timezone(timedelta(hours=9))
# ISO week: YYYY-WNN
TODAY = datetime.now(KST)
WEEK = TODAY.strftime("%G-W%V")
DATE = TODAY.strftime("%Y-%m-%d %a")
SECTION_LABELS = {
    "Done",
    "Next",
    "Blocked",
    "Decisions",
    "Risks",
    "Stalled",
    "완료",
    "다음",
    "막힘",
    "결정",
    "리스크",
    "정체",
}


def header(body: str) -> str:
    return f"📅 *주간 브리핑*\n`{WEEK}` · `{DATE}`\n\n{body}"


def slack_mrkdwn(answer: str) -> str:
    """Keep output as Slack mrkdwn text; Hermes sends stdout via chat.postMessage text."""
    out = []
    previous_blank = False
    for raw in answer.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if not previous_blank:
                out.append("")
            previous_blank = True
            continue
        previous_blank = False
        if stripped.startswith(("- ", "* ")):
            out.append(f"• {stripped[2:].strip()}")
        elif stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                out.append(f"*{heading}*")
        elif stripped in SECTION_LABELS:
            out.append(f"*{stripped}*")
        elif len(stripped) <= 80 and not stripped.endswith((".", "요", "다", ":")):
            out.append(f"*{stripped}*")
        else:
            out.append(stripped)
    return "\n".join(out).strip()


def source_label(source: object) -> str:
    return os.path.basename(str(source)) or str(source)


def main() -> None:
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
        print(header("이번 주는 새로 짚을 진행/막힘 항목이 회수되지 않았어요."))
        return
    out = header(slack_mrkdwn(answer))
    if sources:
        out += "\n\n_근거: " + " · ".join(source_label(s) for s in sources[:5]) + "_"
    print(out)


if __name__ == "__main__":
    main()
    sys.exit(0)
