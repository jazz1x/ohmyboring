#!/usr/bin/env python3
"""Kimi Code CLI UserPromptSubmit hook — surfaces the owner's standing rules at their trigger words.

This script is a thin agent-specific entry point; all shared rules logic lives in
`agents/shared/rules_core.py`. Kimi's payload carries an `origin` and wraps system text
in its own tags, so the injection filter is Kimi's own (`agents/kimi/recall.py`) —
a Kimi system prompt containing trigger words must not fire the rule.
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))
sys.path.insert(0, _HERE)

import recall  # noqa: E402
import rules_core  # noqa: E402


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(f"[omb-rules] invalid stdin JSON: {e}", file=sys.stderr)
        return
    rules_core.run_rules(data, is_injection=recall._is_injection)


if __name__ == "__main__":
    main()
