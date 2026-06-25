#!/usr/bin/env python3
"""UserPromptSubmit hook — recalls *my past work experiences* relevant to the current
prompt from ohmyboring (vector+graph) and injects them as context.

This script is a thin agent-specific entry point; all shared recall logic lives in
`agents/shared/recall_core.py`.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import recall_core  # noqa: E402


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(f"[omb-recall] invalid stdin JSON: {e}", file=sys.stderr)
        return
    recall_core.run_recall(data)


if __name__ == "__main__":
    main()
