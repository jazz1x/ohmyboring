#!/usr/bin/env python3
"""Shared rules logic for agent UserPromptSubmit hooks.

Agent-specific entry points (Claude Code, Kimi, …) are thin wrappers that only supply
their injection filter and then delegate to `run_rules`. The trigger-word matcher below
is pure: rules + prompt in, firing rules out.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable

import event_log

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "src"))
from ohmyboring import config as omb_env  # noqa: E402

TIMEOUT = 2

HEADER = "지켜야 할 규칙 (소유자가 바로잡은 것):"

_SPLIT = re.compile(r"[|\s]+")


def parse_trigger(trigger: str) -> list[list[str]]:
    """'hermes 헤르메스 + 제거|빼|내리|remove|drop' → [['hermes', '헤르메스'], ['제거', '빼', '내리', 'remove', 'drop']].

    Groups are ' + '-separated; alternatives inside a group are '|'- or whitespace-separated.
    """
    return [[alt for alt in _SPLIT.split(group) if alt] for group in (trigger or "").split(" + ")]


def fired(rules: list[dict], prompt: str) -> list[dict]:
    """The rules whose triggers all match the prompt, in input order.

    No trigger (or a trigger whose every group is empty) never fires: `all` over
    groups that each require a matching alternative is False the moment one group
    has nothing to match with.
    """
    needle = prompt.lower()
    out = []
    for rule in rules:
        groups = parse_trigger(rule.get("trigger") or "")
        if groups and all(any(alt.lower() in needle for alt in group) for group in groups):
            out.append(rule)
    return out


def _source_name(path: str) -> str:
    return os.path.basename(path or "").removesuffix(".md")


def run_rules(data: dict, is_injection: Callable[[dict], bool]) -> None:
    """Fetch the door's rules, inject the ones whose triggers fire, record the firings.

    The door's GET /rules holds owner corrections as claims (kind='rule': a 'rule'
    sentence plus its 'trigger' words per subject). Every prompt fetches them, keeps
    the ones whose trigger matches, and injects them as additionalContext. Any failure
    is one stderr line and nothing on stdout — a dead door never costs the user a
    prompt. Each firing is recorded as a rule_fired event so fired rules can later be
    counted against broken ones.
    """
    if is_injection(data):
        return
    door_url = omb_env.door_url()
    try:
        with urllib.request.urlopen(f"{door_url.rstrip('/')}/rules", timeout=TIMEOUT) as resp:
            payload = json.load(resp)
    except (OSError, ValueError) as e:  # URLError/HTTPError/timeout/JSON — one class: the door is gone
        print(f"[omb-rules] rules fetch failed: {e}", file=sys.stderr)
        return
    hits = fired(payload.get("rules") or [], (data.get("prompt") or ""))
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
