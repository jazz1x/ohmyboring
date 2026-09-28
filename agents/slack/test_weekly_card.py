#!/usr/bin/env python3
"""Network-free tests for the weekly card poster (agents/slack/weekly_card.py).

The poster runs `weekly-briefing.py` in blocks mode and posts the payload with the bot
token. Slack and the child process are both fakes here — no network, no engine, no vault.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HERMES = ROOT.parent / "hermes"
sys.path.insert(0, str(HERMES))

TS = "1234.5678"
WEEKLY_OUT = json.dumps(
    {
        "text": "*📅 주간 브리핑*\n`2026-W40 · 2026-09-28 Mon`\n\n*개입 필요 — 상태가 주 내내 지속*\n• kb-rag-bot — 4/7일 🚨",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "📅 주간 브리핑", "emoji": True},
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "`2026-W40 · 2026-09-28 Mon` · 스냅샷 7/7일"}],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*개입 필요 — 상태가 주 내내 지속*\n• kb-rag-bot — 4/7일 🚨\n  컨플루언스 상태 확인 불가",
                },
            },
        ],
        "unfurl_links": False,
        "unfurl_media": False,
    },
    ensure_ascii=False,
)


class FakeSubprocess:
    """Stands in for the subprocess module inside weekly_card."""

    class TimeoutExpired(Exception):
        pass

    CompletedProcess = subprocess.CompletedProcess

    def __init__(self):
        self.calls: list[tuple[list, dict]] = []
        self.next = subprocess.CompletedProcess([], 0, stdout=WEEKLY_OUT, stderr="")

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if isinstance(self.next, Exception):
            raise self.next
        return self.next


class FakeSlackPost(types.ModuleType):
    def __init__(self):
        super().__init__("slack_post")
        self.posted: list[dict] = []
        self.fail_with: Exception | None = None

    def post_payload(self, payload):
        if self.fail_with is not None:
            raise self.fail_with
        self.posted.append(payload)
        return TS


class FakeEventLog:
    """Stands in for agents/shared/event_log.py inside weekly_card."""

    def __init__(self):
        self.recent: list[dict] = []
        self.appended: list[tuple] = []
        self.fail_append: OSError | None = None

    def recent_events(self, limit, component=None, event_name=None, status=None):
        return [dict(e) for e in self.recent]

    def append_event(self, component, event, status, **fields):
        if self.fail_append is not None:
            raise self.fail_append
        self.appended.append((component, event, status, fields))


def load_weekly_card(fake_post: FakeSlackPost):
    sys.modules["slack_post"] = fake_post
    spec = importlib.util.spec_from_file_location("weekly_card_under_test", ROOT / "weekly_card.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["weekly_card_under_test"] = module
    spec.loader.exec_module(module)
    return module


def run_main(weekly_card, fake_sub, env, fake_events: FakeEventLog | None = None):
    """main() with the given Slack env set, everything else cleared; returns (rc, out, err).
    The event log is always faked (empty unless given) so the guard never reads a live engine."""
    saved = dict(os.environ)
    for key in ("SLACK_BOT_TOKEN", "SLACK_CARD_CHANNEL"):
        os.environ.pop(key, None)
    os.environ.update(env)
    out, err = io.StringIO(), io.StringIO()
    real_sub = weekly_card.subprocess
    real_log = weekly_card.event_log
    weekly_card.subprocess = fake_sub
    weekly_card.event_log = fake_events if fake_events is not None else FakeEventLog()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = weekly_card.main()
    finally:
        weekly_card.subprocess = real_sub
        weekly_card.event_log = real_log
        os.environ.clear()
        os.environ.update(saved)
    return rc, out.getvalue(), err.getvalue()


ENV = {"SLACK_BOT_TOKEN": "tok", "SLACK_CARD_CHANNEL": "D0123456789"}


def test_missing_env_refuses_before_running_anything():
    weekly_card = load_weekly_card(FakeSlackPost())
    fake_sub = FakeSubprocess()
    rc, _out, err = run_main(weekly_card, fake_sub, {"SLACK_CARD_CHANNEL": "D0123456789"})
    assert rc == 2, err
    assert "SLACK_BOT_TOKEN" in err
    assert fake_sub.calls == [], "env must be validated before the weekly runs"

    rc, _out, err = run_main(weekly_card, fake_sub, {"SLACK_BOT_TOKEN": "tok"})
    assert rc == 2, err
    assert "SLACK_CARD_CHANNEL" in err


def test_posts_the_blocks_payload_and_prints_the_ts():
    """The mutation this kills: posting the text payload instead of the blocks."""
    fake_post = FakeSlackPost()
    weekly_card = load_weekly_card(fake_post)
    fake_sub = FakeSubprocess()
    rc, out, err = run_main(weekly_card, fake_sub, ENV)
    assert rc == 0, err
    assert f"[weekly] posted ts={TS}" in out
    assert len(fake_post.posted) == 1
    posted = fake_post.posted[0]
    assert "blocks" in posted, "the poster must post the Block Kit payload, not the text"
    assert posted["blocks"][0]["type"] == "header"
    assert posted["text"].startswith("*📅 주간 브리핑*")
    assert posted["unfurl_links"] is False and posted["unfurl_media"] is False
    argv, kwargs = fake_sub.calls[0]
    assert kwargs["env"]["BORING_BRIEFING_FORMAT"] == "blocks"
    assert argv[0] == sys.executable
    assert argv[1].endswith("weekly-briefing.py") and Path(argv[1]).is_file()
    assert kwargs["timeout"] >= 180, "the engine fallback's own timeout must fit inside the child's"


def test_nothing_to_say_posts_nothing_and_says_so():
    import slack_briefing

    fake_post = FakeSlackPost()
    weekly_card = load_weekly_card(fake_post)
    fake_sub = FakeSubprocess()
    fake_sub.next = subprocess.CompletedProcess(
        [],
        0,
        stdout=f"*📅 주간 브리핑*\n`2026-W40 · 2026-09-28 Mon`\n\n{slack_briefing.EMPTY_MESSAGE}",
        stderr="",
    )
    rc, out, err = run_main(weekly_card, fake_sub, ENV)
    assert rc == 0, err
    assert fake_post.posted == [], "an empty week must not post"
    line = out.strip()
    assert line.count("\n") == 0, f"one line, said so:\n{out}"
    assert "올릴 브리핑 없음" in line


def test_the_weeklys_own_failure_line_is_quoted_and_exits_3():
    fake_post = FakeSlackPost()
    weekly_card = load_weekly_card(fake_post)
    fake_sub = FakeSubprocess()
    fake_sub.next = subprocess.CompletedProcess(
        [],
        0,
        stdout="*📅 주간 브리핑*\n`2026-W40 · 2026-09-28 Mon`\n\n"
        "⚠️ ohmyboring(RAG) 응답 없음 — 엔진 가동 확인 필요. (connection refused)",
        stderr="",
    )
    rc, _out, err = run_main(weekly_card, fake_sub, ENV)
    assert rc == 3, err
    assert fake_post.posted == [], "a broken weekly is not a weekly to send"
    assert "connection refused" in err and "주간 브리핑 생성 실패" in err
    assert err.strip().count("\n") == 0


def test_a_failing_weekly_process_exits_3():
    fake_post = FakeSlackPost()
    weekly_card = load_weekly_card(fake_post)
    fake_sub = FakeSubprocess()
    fake_sub.next = subprocess.CompletedProcess(
        [], 1, stdout="", stderr="[weekly] 주간 브리핑 생성 실패: engine down\n"
    )
    rc, _out, err = run_main(weekly_card, fake_sub, ENV)
    assert rc == 3, err
    assert fake_post.posted == []
    assert "engine down" in err


def test_a_timed_out_weekly_exits_3():
    fake_post = FakeSlackPost()
    weekly_card = load_weekly_card(fake_post)
    fake_sub = FakeSubprocess()
    fake_sub.next = fake_sub.TimeoutExpired(["weekly-briefing.py"], 300)
    rc, _out, err = run_main(weekly_card, fake_sub, ENV)
    assert rc == 3, err
    assert "시간 초과" in err
    assert fake_post.posted == []


def test_a_slack_refusal_exits_1():
    fake_post = FakeSlackPost()
    fake_post.fail_with = Exception("channel_not_found")
    weekly_card = load_weekly_card(fake_post)
    fake_sub = FakeSubprocess()
    rc, out, err = run_main(weekly_card, fake_sub, ENV)
    assert rc == 1, err
    assert "channel_not_found" in err
    assert "posted" not in out


def test_already_posted_this_week_stops_before_running_anything():
    """The launchd→hermes handover guard: a weekly_card event for the current ISO week means
    the other scheduler already posted — exit 0, one line, no child, no post. A mutant that
    deletes the guard fails here: the child would run and the assertion on its calls fires."""
    weekly_card = load_weekly_card(FakeSlackPost())
    fake_sub = FakeSubprocess()
    fake_events = FakeEventLog()
    fake_events.recent = [{"event": "weekly_card", "week": weekly_card.this_week(), "ts": TS}]
    rc, out, err = run_main(weekly_card, fake_sub, ENV, fake_events)
    assert rc == 0, err
    line = out.strip()
    assert line.count("\n") == 0, f"one line, said so:\n{out}"
    assert f"[weekly] already posted this week (ts={TS})" in line
    assert fake_sub.calls == [], "the guard must stop the run before the child starts"
    assert fake_events.appended == []


def test_last_weeks_event_does_not_stop_this_weeks_run():
    weekly_card = load_weekly_card(FakeSlackPost())
    fake_sub = FakeSubprocess()
    fake_events = FakeEventLog()
    fake_events.recent = [{"event": "weekly_card", "week": "2020-W01", "ts": "1.0"}]
    rc, out, err = run_main(weekly_card, fake_sub, ENV, fake_events)
    assert rc == 0, err
    assert f"[weekly] posted ts={TS}" in out
    assert len(fake_sub.calls) == 1


def test_a_successful_post_records_the_weekly_card_event():
    """The guard's ledger: one weekly_card event per posted weekly, week label + ts, so the
    second scheduler of the ISO week sees the card. A mutant that drops the recording fails
    test_already_posted_this_week_stops_before_running_anything's fixture the other way."""
    weekly_card = load_weekly_card(FakeSlackPost())
    fake_sub = FakeSubprocess()
    fake_events = FakeEventLog()
    rc, _out, err = run_main(weekly_card, fake_sub, ENV, fake_events)
    assert rc == 0, err
    assert len(fake_events.appended) == 1
    component, event, status, fields = fake_events.appended[0]
    assert (component, event, status) == ("slack-card", "weekly_card", "ok")
    assert fields["ts"] == TS
    assert fields["week"] == weekly_card.this_week()


def test_a_failed_event_write_does_not_fail_the_post():
    weekly_card = load_weekly_card(FakeSlackPost())
    fake_sub = FakeSubprocess()
    fake_events = FakeEventLog()
    fake_events.fail_append = OSError("spool read-only")
    rc, out, err = run_main(weekly_card, fake_sub, ENV, fake_events)
    assert rc == 0, "the card is already out — a lost ledger row is a line, not a failure exit"
    assert f"[weekly] posted ts={TS}" in out
    assert "weekly_card event not recorded" in err


def test_the_blocks_themselves_render_bold_as_bold_with_a_header():
    """Work-item 3 as a pin: the weekly's blocks open with a header like the card, and bold is
    Slack mrkdwn `*x*` — a `**` anywhere means markdown leaked into Block Kit."""
    import weekly_trend
    from slack_briefing import parse_brief, render_weekly_blocks, render_weekly_mrkdwn

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
    print(f"ok - weekly card poster ({len(_tests)} tests)")
