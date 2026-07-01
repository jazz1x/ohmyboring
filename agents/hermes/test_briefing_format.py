#!/usr/bin/env python3
"""Network-free tests for Hermes Slack briefing formatting."""

from __future__ import annotations

import importlib.util
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

    expected = """*oh-my-boring*
• *Done* — fixed *DB* primary event logging
• *Next* — add ops status JSON
• *Blocked* — LM Studio embedding model is not loaded"""

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
    assert payload["blocks"][2]["type"] == "divider"
    assert payload["blocks"][4]["type"] == "section"
    fields = payload["blocks"][4]["fields"]
    assert fields[0]["text"] == "*Done*\nfixed *DB* primary event logging"
    assert fields[1]["text"] == "*Next*\nadd ops status JSON"
    assert fields[2]["text"] == "*Blocked*\nLM Studio embedding model is not loaded"
    assert "Blocked: -" not in payload["text"]
    assert payload["blocks"][-1]["type"] == "context"
    assert "wiki-0001.md" in payload["blocks"][-1]["elements"][0]["text"]


if __name__ == "__main__":
    test_slack_mrkdwn_uses_flat_readable_bullets()
    print("ok - hermes briefing Slack formatting")
