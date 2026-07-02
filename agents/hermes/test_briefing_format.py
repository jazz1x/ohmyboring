#!/usr/bin/env python3
"""Network-free tests for Hermes Slack briefing formatting."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_slack_mrkdwn_uses_flat_readable_bullets():
    briefing = load_module("briefing", ROOT / "briefing.py")
    weekly = load_module("weekly_briefing", ROOT / "weekly-briefing.py")
    slack_briefing = load_module("slack_briefing_test", ROOT / "slack_briefing.py")

    answer = """# oh-my-boring

- Done: fixed **DB** primary event logging
1. Next: add ops status JSON
Blocked:
- Blocked: -
## oh-my-boring
- 막힘： LM Studio embedding model is not loaded
"""

    expected = """🚨 1 · ▶️ 1 · ✅ 1

🚨 *막힘*
• oh-my-boring — LM Studio embedding model is not loaded

▶️ *다음 행동*
• oh-my-boring — add ops status JSON

✅ *완료*
• oh-my-boring — fixed *DB* primary event logging"""

    assert briefing.slack_mrkdwn(answer) == expected
    assert weekly.slack_mrkdwn(answer) == expected

    payload = slack_briefing.render_blocks_payload(
        "☀️ 아침 브리핑",
        "2026-07-01 Wed",
        answer,
        ["/vault/wiki/wiki-0001.md"],
        "비어 있음",
    )
    assert payload["text"].startswith("☀️ 아침 브리핑")
    assert payload["blocks"][0]["type"] == "header"
    assert payload["blocks"][1]["type"] == "context"
    assert payload["blocks"][2]["type"] == "context"
    assert "🚨 1" in payload["blocks"][2]["elements"][0]["text"]
    assert payload["blocks"][3]["type"] == "divider"
    assert payload["blocks"][4]["type"] == "section"
    assert "막힘" in payload["blocks"][4]["text"]["text"]
    assert payload["blocks"][5]["type"] == "section"
    assert "LM Studio" in payload["blocks"][5]["text"]["text"]
    assert "Blocked: -" not in payload["text"]
    assert payload["blocks"][-1]["type"] == "context"
    assert "wiki-0001.md" in payload["blocks"][-1]["elements"][0]["text"]


def test_slack_mrkdwn_handles_adversarial_inputs():
    slack_briefing = load_module("slack_briefing_test2", ROOT / "slack_briefing.py")

    # Empty answer falls back to the empty message.
    assert slack_briefing.render_body_mrkdwn("") == ""

    # Project heading with no items falls back to compact text (render_message_mrkdwn
    # will substitute the empty message when the body is empty).
    assert slack_briefing.render_body_mrkdwn("# empty-project\n") == "# empty-project"

    # Label without a value is skipped, and a label heading applies to multiple bullets.
    multi = """# p

Blocked:
- first blocker
- second blocker
- 없음
"""
    body = slack_briefing.render_body_mrkdwn(multi)
    assert "🚨 *막힘*" in body
    assert body.count("first blocker") == 1
    assert body.count("second blocker") == 1
    assert "없음" not in body  # EMPTY_VALUES should be dropped
    assert "기타" not in body  # both bullets inherited the Blocked label

    # Unknown labels and label-free bullets land in "기타".
    misc = """# p

- UnknownLabel: something odd
- plain bullet without a label
"""
    body = slack_briefing.render_body_mrkdwn(misc)
    assert "• *기타*" in body
    assert "something odd" in body
    assert "plain bullet without a label" in body

    # HTML-like characters are preserved in mrkdwn and escaped in Block Kit.
    html = """# p

- Next: fix <body> & "quotes"
"""
    body = slack_briefing.render_body_mrkdwn(html)
    assert "fix <body> & \"quotes\"" in body
    payload = slack_briefing.render_blocks_payload(
        "t", "s", html, [], "empty"
    )
    # Fallback mrkdwn keeps raw characters; Block Kit blocks escape them.
    block_blob = json.dumps(payload["blocks"], ensure_ascii=False)
    assert "&lt;body&gt;" in block_blob
    assert "&amp;" in block_blob


def test_slack_mrkdwn_dedups_duplicate_bullets_across_project_sections():
    slack_briefing = load_module("slack_briefing_test3", ROOT / "slack_briefing.py")

    answer = """## kb-rag-bot
- Done: README 최신화
- Next: 컨플루언스 문서 업데이트
## qa-tests
- Done: PoC 일정 전환
## kb-rag-bot
- Done: README 최신화
- Blocked: 토큰 문제
"""
    body = slack_briefing.render_body_mrkdwn(answer)
    # README 최신화는 exact duplicate → 1회만.
    assert body.count("README 최신화") == 1
    # Blocked from the second kb-rag-bot section is preserved.
    assert body.count("토큰 문제") == 1
    # Summary counts reflect dedup.
    assert "✅ 2" in body  # README 최신화 + PoC 일정 전환
    assert "🚨 1" in body


def test_slack_mrkdwn_filters_placeholders_and_noise():
    slack_briefing = load_module("slack_briefing_test4", ROOT / "slack_briefing.py")

    answer = """## kb-rag-bot
- Done: 게이트 4단계 구현
- Next: 다음 지시 기다림
- Blocked: -
- Risks: 없음
- Decisions: 출처 강등 처리
"""
    body = slack_briefing.render_body_mrkdwn(answer)
    # Vacuous bullets dropped.
    assert "다음 지시 기다림" not in body
    assert "Blocked: -" not in body
    assert "없음" not in body
    # Real bullets preserved.
    assert "게이트 4단계 구현" in body
    assert "출처 강등 처리" in body


def test_slack_mrkdwn_caps_done_items():
    slack_briefing = load_module("slack_briefing_test5", ROOT / "slack_briefing.py")

    answer = "## kb-rag-bot\n" + "\n".join(
        f"- Done: task {i}" for i in range(10)
    )
    body = slack_briefing.render_body_mrkdwn(answer)
    # Only first 3 Done items shown; rest collapsed.
    assert body.count("task") == 3
    assert "외 7개 항목" in body


if __name__ == "__main__":
    test_slack_mrkdwn_uses_flat_readable_bullets()
    test_slack_mrkdwn_handles_adversarial_inputs()
    test_slack_mrkdwn_dedups_duplicate_bullets_across_project_sections()
    test_slack_mrkdwn_filters_placeholders_and_noise()
    test_slack_mrkdwn_caps_done_items()
    print("ok - hermes briefing Slack formatting")
