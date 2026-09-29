#!/usr/bin/env python3
"""Network-free tests for the weekly card entry (agents/slack/weekly_card.py).

The entry checks the Slack env, runs the weekly graph and prints what the graph's report says.
The graph's edges are in src/ohmyboring/weekly/test_graph.py and the bytes it posts are pinned in
test_pins.py — here only the entry's own promises: refuse before running, print the report as
given, no child process.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "shared"))
sys.path.insert(0, str(ROOT.parents[1] / "src"))

from ohmyboring.briefing.parse import parse_brief  # noqa: E402
from ohmyboring.briefing.weekly_render import render_weekly_blocks, render_weekly_mrkdwn  # noqa: E402
from ohmyboring.weekly import trend as weekly_trend  # noqa: E402
from ohmyboring.weekly.report import Report  # noqa: E402

ENV = {"SLACK_BOT_TOKEN": "tok", "SLACK_CARD_CHANNEL": "D0123456789"}


def load_weekly_card():
    spec = importlib.util.spec_from_file_location("weekly_card_under_test", ROOT / "weekly_card.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["weekly_card_under_test"] = module
    spec.loader.exec_module(module)
    return module


def run_main(weekly_card, env, report=None):
    """main() with the given Slack env set, everything else cleared; returns (rc, out, err, calls)."""
    saved = dict(os.environ)
    for key in ("SLACK_BOT_TOKEN", "SLACK_CARD_CHANNEL"):
        os.environ.pop(key, None)
    os.environ.update(env)
    calls: list[str] = []

    def fake_deliver(environ, split_frontmatter):
        calls.append("deliver_week")
        return report

    real = weekly_card.deliver_week
    weekly_card.deliver_week = fake_deliver
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = weekly_card.main()
    finally:
        weekly_card.deliver_week = real
        os.environ.clear()
        os.environ.update(saved)
    return rc, out.getvalue(), err.getvalue(), calls


def test_missing_env_refuses_before_running_the_graph():
    weekly_card = load_weekly_card()
    rc, _out, err, calls = run_main(weekly_card, {"SLACK_CARD_CHANNEL": "D0123456789"})
    assert rc == 2 and "SLACK_BOT_TOKEN must be set" in err
    assert calls == []
    rc, _out, err, calls = run_main(weekly_card, {"SLACK_BOT_TOKEN": "tok"})
    assert rc == 2 and "SLACK_CARD_CHANNEL must be set" in err
    assert calls == []


def test_the_entry_prints_the_report_and_returns_its_code():
    weekly_card = load_weekly_card()
    report = Report(1, ("out line",), ("err line",))
    rc, out, err, calls = run_main(weekly_card, ENV, report)
    assert (rc, out, err, calls) == (1, "out line\n", "err line\n", ["deliver_week"])


def test_the_entry_starts_no_child_process():
    source = (ROOT / "weekly_card.py").read_text(encoding="utf-8")
    assert "subprocess" not in source and "weekly-briefing" not in source


def test_the_blocks_themselves_render_bold_as_bold_with_a_header():
    """Work-item 3 as a pin: the weekly's blocks open with a header like the card, and bold is
    Slack mrkdwn `*x*` — a `**` anywhere means markdown leaked into Block Kit."""
    days = [
        (f"2026-09-2{i}", parse_brief(f"# kb-rag-bot\n- Blocked: 컨플루언스 상태 확인 불가 variant {i}\n"))
        for i in range(1, 8)
    ]
    days += [("2026-09-27", parse_brief("# foodspring-front\n- Next: 소프트남바 샘플링 확인"))]
    projects = weekly_trend.collect_week(days)
    span = len({d for d, _ in days})
    blocks = render_weekly_blocks(
        "📅 주간 브리핑",
        "2026-W40 · 2026-09-28 Mon · 스냅샷 7/7일",
        projects,
        [(w, label, n, span) for w, label, n in weekly_trend.needs_intervention(projects)],
        [(w, span) for w in weekly_trend.scoreboard(projects)],
        weekly_trend.label_trend(days),
        [],
    )
    assert blocks[0]["type"] == "header", "the title rides a header block, like the morning card"
    assert blocks[0]["text"]["text"] == "📅 주간 브리핑"
    blob = json.dumps(blocks, ensure_ascii=False)
    assert "**" not in blob, "markdown bold leaked into Block Kit — *x* is bold in mrkdwn"
    sections = [b for b in blocks if b.get("type") == "section" and "text" in b]
    intervention = next(s for s in sections if "개입 필요" in s["text"]["text"])
    assert intervention["text"]["text"].startswith("*개입 필요"), (
        "the intervention heading is bold — Slack mrkdwn *x*, not markdown **"
    )
    text = render_weekly_mrkdwn(
        "📅 주간 브리핑",
        "2026-W40 · 2026-09-28 Mon · 스냅샷 7/7일",
        [(w, label, n, span) for w, label, n in weekly_trend.needs_intervention(projects)],
        [(w, span) for w in weekly_trend.scoreboard(projects)],
        weekly_trend.label_trend(days),
        [],
    )
    assert "일자별 관측치이며 마감률이 아니다" in text, "the fallback text carries what the blocks carry"


if __name__ == "__main__":
    _module = sys.modules[__name__]
    _tests = [
        (name, obj)
        for name, obj in sorted(vars(_module).items())
        if name.startswith("test_") and callable(obj)
    ]
    for _name, _fn in _tests:
        _fn()
    print(f"ok - weekly card entry ({len(_tests)} tests)")
