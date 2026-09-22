#!/usr/bin/env python3
"""UserPromptSubmit hook — recalls *my past work experiences* relevant to the current
prompt from ohmyboring (vector+graph) and injects them as context.

This script is a thin agent-specific entry point; all shared recall logic lives in
`agents/shared/recall_core.py`.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import recall_core  # noqa: E402

# A prompt that is nothing but image placeholders carries no words to retrieve on.
_IMAGE_PLACEHOLDER = re.compile(r"\[Image #\d+\]")


def _is_injection(data: dict) -> bool:
    """Skip recall for text the harness injected rather than text the user typed.

    Claude Code's payload has no `origin` field (Kimi's does), so the test has to be
    structural: what the harness wraps in its own tags, and prompts that are only image
    placeholders. Both were measured over 7 days of `query_log` — task notifications are
    695 of 2,178 firings (31.9%) and image-only prompts 14 more, and each firing injects
    three notes, so this is ~2,130 note-injections spent on things nobody asked.

    Deliberately NOT a length test. Sampling the sub-12-character prompts showed they are
    mostly real turns — "어드바이저 불러", "D12 뭐지?", "아뇨 DB는 없어요" — so a character
    threshold would throw away exactly the terse questions it looks like it should catch.
    The `len(prompt) < 8` guard already in `recall_core` is left alone.
    """
    prompt = (data.get("prompt") or "").strip()
    if prompt.startswith("<task-notification>"):
        return True
    return bool(prompt) and not _IMAGE_PLACEHOLDER.sub("", prompt).strip()


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(f"[omb-recall] invalid stdin JSON: {e}", file=sys.stderr)
        return
    recall_core.run_recall(data, is_injection=_is_injection)


if __name__ == "__main__":
    main()
