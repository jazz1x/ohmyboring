#!/usr/bin/env python3
"""Unit tests for rules_core — pure matcher, no network, no pytest dependency.

Run: python3 agents/shared/test_rules_core.py

The trigger syntax under test: groups joined by ' + ' must ALL match; inside a
group alternatives are separated by '|' or whitespace; a match is a case-insensitive
substring of the prompt; a rule with no trigger never fires.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rules_core

HERMES = {
    "subject": "rule-hermes-not-removed",
    "rule": "hermes 를 빼지 마세요",
    "trigger": "hermes 헤르메스 + 제거|빼|내리|remove|drop",
    "source_path": "/vault/wiki/wiki-1955.md",
}


def _rule(trigger: str, subject: str = "s") -> dict:
    return {"subject": subject, "rule": "r", "trigger": trigger, "source_path": "x"}


def test_parse_trigger_splits_groups_and_alternatives():
    assert rules_core.parse_trigger(HERMES["trigger"]) == [
        ["hermes", "헤르메스"],
        ["제거", "빼", "내리", "remove", "drop"],
    ]


def test_all_groups_are_needed():
    rules = [HERMES]
    assert rules_core.fired(rules, "hermes 좀 봐줘") == [], "group 2 unmatched → no fire"
    assert rules_core.fired(rules, "제거해줘") == [], "group 1 unmatched → no fire"
    assert rules_core.fired(rules, "hermes 제거해줘") == [HERMES]
    assert rules_core.fired(rules, "Hermes 좀 DROP 해줘") == [HERMES], "case-insensitive"


def test_group_with_no_alternative_never_fires():
    rules = [_rule("hermes + ")]
    assert rules_core.fired(rules, "hermes whatever") == []


def test_no_trigger_never_fires():
    for trigger in ("", "   "):
        assert rules_core.fired([_rule(trigger)], "anything at all 제거") == []


def test_whitespace_and_pipe_alternatives_are_equivalent():
    assert rules_core.fired([_rule("aaa|bbb")], "xx bbb yy"), "pipe alternative fires"
    assert rules_core.fired([_rule("aaa bbb")], "xx bbb yy"), "whitespace alternative fires"
    assert rules_core.fired([_rule("aaa|bbb")], "xx aa yy") == [], "no alternative as a substring → no fire"


def test_korean_substrings_match():
    rules = [_rule("제거")]
    assert rules_core.fired(rules, "그냥 헤르메스 좀 제거해 주세요") == rules
    assert rules_core.fired(rules, "제거 없음도 아님") == rules
    assert rules_core.fired(rules, "재거해줘") == []


def test_substring_match_is_not_a_word_boundary_match():
    assert rules_core.fired([_rule("remove")], "remover") != [], "substring, so 'remover' fires"
    assert rules_core.fired([_rule("remove")], "unrelated") == []


def test_fired_keeps_input_order_and_the_rule_dict():
    a, b = _rule("aaa", "a"), _rule("bbb", "b")
    assert rules_core.fired([a, b], "aaa and bbb") == [a, b]
    assert rules_core.fired([b, a], "aaa and bbb") == [b, a]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(
        "ok - rules_core: all groups needed · |/whitespace alternatives · case-insensitive · no-trigger never fires"
    )
