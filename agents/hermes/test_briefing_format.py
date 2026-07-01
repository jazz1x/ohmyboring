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


if __name__ == "__main__":
    test_slack_mrkdwn_uses_flat_readable_bullets()
    test_slack_mrkdwn_handles_adversarial_inputs()
    print("ok - hermes briefing Slack formatting")
