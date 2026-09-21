#!/usr/bin/env python3
"""The secretary's brain, tested without Slack and without an engine.

Run: python3 agents/slack/test_secretary_core.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import secretary_core as sc  # noqa: E402


def _hit(src, text, claims=None, total=None):
    h = {"source_path": f"/vault/wiki/{src}", "snippet": text}
    if claims is not None:
        h["claims"] = claims
        h["claims_total"] = total if total is not None else len(claims)
    return h


def test_a_mention_is_stripped_before_it_is_asked():
    assert sc.strip_mention("<@U0123ABC> 크론 잡 어디부터 봤더라") == "크론 잡 어디부터 봤더라"
    assert sc.strip_mention("그냥 질문") == "그냥 질문"


def test_the_answer_is_the_hooks_lines_with_the_claims_under_each():
    hits = [
        _hit("wiki-0435.md", "the branch naming question came up again and was settled " * 3,
             claims=[{"subject": "ohmyboring", "predicate": "branch-name", "value": "fix/...", "kind": "decision"}],
             total=4),
    ]
    out = sc.answer("브랜치 이름 어떻게 정했더라", search=lambda q, **kw: hits)
    assert "*wiki-0435.md*" in out
    assert "[decision] ohmyboring branch-name: fix/..." in out
    assert "declared 4, handed over 1" in out, "the cut is stated to a person too"


def test_the_search_is_asked_for_claims_like_the_hook_is():
    seen = {}
    def search(q, **kw):
        seen.update(kw); return []
    sc.answer("아무 질문이나", search=search)
    assert seen["claims"] == sc.CLAIMS_PER_HIT >= 1
    assert seen["max_results"] == sc.MAX_HITS


def test_nothing_found_and_engine_down_are_different_answers():
    assert sc.answer("이 주제는 없다", search=lambda q, **kw: []) == sc.NOTHING_FOUND
    def down(q, **kw): raise ConnectionError("refused")
    assert sc.answer("엔진이 죽었다", search=down) == sc.ENGINE_DOWN
    assert sc.NOTHING_FOUND != sc.ENGINE_DOWN


def test_a_question_too_short_to_search_asks_for_one():
    called = []
    sc.answer("<@U1> 응", search=lambda q, **kw: called.append(q) or [])
    assert called == [], "two characters are not a question; the engine is not consulted"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - secretary_core: answers from the engine, says when it cannot look")
