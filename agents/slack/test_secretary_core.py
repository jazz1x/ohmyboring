#!/usr/bin/env python3
"""The secretary's brain, tested without Slack and without an engine.

Run: python3 agents/slack/test_secretary_core.py
"""
import os
import sys
import tempfile

os.environ["BORING_INJECTION_LEDGER"] = os.path.join(tempfile.mkdtemp(), "injections.jsonl")

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import secretary_core as sc  # noqa: E402


def _hit(src, text, claims=None, total=None):
    h = {"source_path": f"/vault/wiki/{src}", "snippet": text}
    if claims is not None:
        h["claims"] = claims
        h["claims_total"] = total if total is not None else len(claims)
    return h


def _recorder():
    calls = []

    def fake(session_id, observed_at, used, contested, supersedes=None):
        calls.append({
            "session_id": session_id,
            "observed_at": observed_at,
            "used": list(used),
            "contested": list(contested),
        })
        return {"used": len(used), "contested": len(contested), "unknown": []}

    return calls, fake


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
    assert "*wiki-0435.md*" in out.text
    assert "[decision] ohmyboring branch-name: fix/..." in out.text
    assert "declared 4, handed over 1" in out.text, "the cut is stated to a person too"


def test_the_search_is_asked_for_claims_like_the_hook_is():
    seen = {}
    def search(q, **kw):
        seen.update(kw); return []
    sc.answer("아무 질문이나", search=search)
    assert seen["claims"] == sc.CLAIMS_PER_HIT >= 1
    assert seen["max_results"] == sc.MAX_HITS


def test_nothing_found_and_engine_down_are_different_answers():
    assert sc.answer("이 주제는 없다", search=lambda q, **kw: []).text == sc.NOTHING_FOUND
    def down(q, **kw): raise ConnectionError("refused")
    assert sc.answer("엔진이 죽었다", search=down).text == sc.ENGINE_DOWN
    assert sc.NOTHING_FOUND != sc.ENGINE_DOWN


def test_a_question_too_short_to_search_asks_for_one():
    called = []
    out = sc.answer("<@U1> 응", search=lambda q, **kw: called.append(q) or [])
    assert called == [], "two characters are not a question; the engine is not consulted"
    assert out.hits == []


def test_hits_are_exactly_the_notes_the_reader_saw():
    hits = [
        _hit("wiki-0435.md", "the branch naming question came up again and was settled " * 3),
        _hit("wiki-0999.md", ""),  # no snippet: rendered by neither, so ledgered by neither
        _hit("wiki-1000.md", "the pool question came up again and was settled " * 3),
        _hit("wiki-1001.md", "the cron question came up again and was settled " * 3),
    ]
    out = sc.answer("브랜치 이름 어떻게 정했더라", search=lambda q, **kw: hits)
    assert [h["source_path"] for h in out.hits] == [
        "/vault/wiki/wiki-0435.md",
        "/vault/wiki/wiki-1000.md",
    ], "the empty snippet drops out and the MAX_HITS cut still applies"
    assert out.text.count("• ") == len(out.hits) == 2, "one bullet per handed note, nothing else"
    assert "wiki-0999" not in out.text and "wiki-1001" not in out.text


def test_empty_and_failed_answers_carry_no_hits():
    assert sc.answer("이 주제는 없다", search=lambda q, **kw: []).hits == []
    def down(q, **kw): raise ConnectionError("refused")
    assert sc.answer("엔진이 죽었다", search=down).hits == []


def test_feedback_marks_the_handed_notes_used():
    key = "slack:C123:1726900000.000100"
    hits = [_hit("wiki-0435.md", "the branch naming question came up again and was settled " * 3)]
    assert sc.remember_handed(key, "브랜치 이름 어떻게 정했더라", hits) is True
    calls, fake = _recorder()
    out = sc.feedback(key, "used", consumption=fake)
    assert calls[0]["session_id"] == key
    assert calls[0]["used"] == ["/vault/wiki/wiki-0435.md"]
    assert calls[0]["contested"] == []
    assert calls[0]["observed_at"], "a default timestamp is stamped on the edge"
    assert out["used"] == 1 and out["contested"] == 0


def test_feedback_contested_marks_the_other_list():
    key = "slack:C123:1726900000.000200"
    sc.remember_handed(key, "브랜치 이름 어떻게 정했더라",
                       [_hit("wiki-0435.md", "the branch naming question came up again and was settled " * 3)])
    calls, fake = _recorder()
    sc.feedback(key, "contested", consumption=fake)
    assert calls[0]["contested"] == ["/vault/wiki/wiki-0435.md"]
    assert calls[0]["used"] == []


def test_feedback_on_an_unknown_answer_never_reaches_the_engine():
    calls, fake = _recorder()
    out = sc.feedback("slack:C123:1726900000.000999", "used", consumption=fake)
    assert calls == []
    assert out == {"unknown_answer": True}


def test_remember_handed_with_empty_hands_writes_nothing():
    key = "slack:C123:1726900000.000300"
    assert sc.remember_handed(key, "질문", []) is False
    calls, fake = _recorder()
    assert sc.feedback(key, "used", consumption=fake) == {"unknown_answer": True}
    assert calls == []


def test_feedback_rejects_a_verdict_that_is_not_a_verdict():
    calls, fake = _recorder()
    try:
        sc.feedback("slack:C123:x", "meh", consumption=fake)
    except ValueError:
        pass
    else:
        raise AssertionError("'meh' is not an edge the engine has; it must be refused, not sent")
    assert calls == []


def test_feedback_survives_a_dead_engine():
    key = "slack:C123:1726900000.000400"
    sc.remember_handed(key, "질문",
                       [_hit("wiki-0435.md", "the branch naming question came up again and was settled " * 3)])
    def boom(*a, **kw): raise ConnectionError("refused")
    assert sc.feedback(key, "used", consumption=boom) == {"error": "refused"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - secretary_core: answers from the engine, and a 👍/👎 closes the loop")
