#!/usr/bin/env python3
"""Tests for rule-firings.py. Run: python3 scripts/test_rule_firings.py"""

import importlib.util
import os
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["BORING_EVENT_SINK"] = "spool"
_spec = importlib.util.spec_from_file_location("rule_firings", HERE / "rule-firings.py")
rule_firings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rule_firings)

RULES = (
    rule_firings.rules_core.HEADER + "\n- hermes 를 빼지 마세요 (출처 wiki-1955)\n- 하나씩 (출처 wiki-1956)"
)


def _prompt(uuid, text):
    return {"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}}


def _context(parent, text, uuid=None):
    return {
        "type": "attachment",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": "s",
        "timestamp": "2026-09-27T01:00:00Z",
        "attachment": {"type": "hook_additional_context", "hookName": "UserPromptSubmit", "content": [text]},
    }


class Firings(unittest.TestCase):
    def test_each_firing_is_judged_by_the_prompt_it_answered(self):
        # Live transcripts chain hook attachments onto each other before the prompt.
        rows = [
            _prompt("u1", "hermes 제거하자"),
            _context("u1", RULES),
            _prompt(
                "u2",
                'Another Claude session sent a message:\n<agent-message from="a">hermes 제거</agent-message>',
            ),
            _context("u2", "📚 My past work experience", uuid="a2"),
            _context("a2", RULES),
            _context("gone", RULES),
        ]
        self.assertEqual(
            rule_firings.firings(rows),
            [
                ("s", "2026-09-27", "owner", 2),
                ("s", "2026-09-27", "harness", 2),
                ("s", "2026-09-27", "unknown", 2),
            ],
        )


if __name__ == "__main__":
    unittest.main()
