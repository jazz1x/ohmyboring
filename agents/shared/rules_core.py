#!/usr/bin/env python3
"""The trigger-word matcher for owner rules — pure: rules + prompt in, firing rules out.

A rule fires only when every trigger group matches the prompt; inside a group any one
alternative is enough. A match is a case-insensitive substring, so Korean trigger words
behave exactly like English ones. A rule with no trigger never fires — without trigger
words there is no moment to surface it at, and a group with no alternatives matches
nothing, so malformed triggers fire nothing either.
"""

from __future__ import annotations

import re

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
