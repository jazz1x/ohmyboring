#!/usr/bin/env python3
"""UserPromptSubmit hook — surfaces the owner's standing rules the moment their trigger words appear.

The door's GET /rules holds owner corrections as claims (kind='rule': a 'rule' sentence
plus its 'trigger' words per subject). This hook fetches them on every prompt, keeps the
ones whose trigger matches, and injects them as additionalContext. Any failure is one
stderr line and nothing on stdout — a dead door never costs the user a prompt. Each firing
is recorded as a rule_fired event so fired rules can later be counted against broken ones.
"""

import json
import os
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))
sys.path.insert(0, _HERE)

import event_log  # noqa: E402
import recall  # noqa: E402
import rules_core  # noqa: E402

TIMEOUT = 2

HEADER = "지켜야 할 규칙 (소유자가 바로잡은 것):"


def _source_name(path: str) -> str:
    return os.path.basename(path or "").removesuffix(".md")


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(f"[omb-rules] invalid stdin JSON: {e}", file=sys.stderr)
        return
    if recall._is_injection(data):
        return
    door_url = os.environ.get("BORING_DOOR_URL")
    if not door_url:
        return
    try:
        with urllib.request.urlopen(f"{door_url.rstrip('/')}/rules", timeout=TIMEOUT) as resp:
            payload = json.load(resp)
    except (OSError, ValueError) as e:  # URLError/HTTPError/timeout/JSON — one class: the door is gone
        print(f"[omb-rules] rules fetch failed: {e}", file=sys.stderr)
        return
    hits = rules_core.fired(payload.get("rules") or [], (data.get("prompt") or ""))
    if not hits:
        return
    lines = [HEADER]
    lines += [f"- {r.get('rule')} (출처 {_source_name(r.get('source_path') or '')})" for r in hits]
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "\n".join(lines),
                }
            }
        )
    )
    for r in hits:
        event_log.try_append_event(
            "rules", "rule_fired", "ok", subject=r.get("subject"), session_id=data.get("session_id") or None
        )


if __name__ == "__main__":
    main()
