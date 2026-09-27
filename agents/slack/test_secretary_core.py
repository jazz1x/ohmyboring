#!/usr/bin/env python3
"""The secretary's brain, tested without Slack and without an engine.

Run: python3 agents/slack/test_secretary_core.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))

import drudge_client as dc  # noqa: E402
import secretary_core as sc  # noqa: E402


def _hit(src, text, claims=None, total=None):
    h = {"source_path": f"/vault/wiki/{src}", "snippet": text}
    if claims is not None:
        h["claims"] = claims
        h["claims_total"] = total if total is not None else len(claims)
    return h


def _handover_recorder():
    calls = []

    def fake(session_id, observed_at, paths):
        calls.append(
            {
                "session_id": session_id,
                "observed_at": observed_at,
                "paths": list(paths),
            }
        )
        return {"session": session_id, "handed": len(paths), "unknown": []}

    return calls, fake


def _verdict_recorder(result=None):
    calls = []

    def fake(session_id, observed_at, verdict=None, **kw):
        assert not kw, f"a verdict travels without path lists, got unexpected {sorted(kw)}"
        calls.append(
            {
                "session_id": session_id,
                "observed_at": observed_at,
                "verdict": verdict,
            }
        )
        return result if result is not None else {"used": 1, "contested": 0, "unknown": []}

    return calls, fake


def test_a_mention_is_stripped_before_it_is_asked():
    assert sc.strip_mention("<@U0123ABC> 크론 잡 어디부터 봤더라") == "크론 잡 어디부터 봤더라"
    assert sc.strip_mention("그냥 질문") == "그냥 질문"


def test_the_answer_is_the_hooks_lines_with_the_claims_under_each():
    hits = [
        _hit(
            "wiki-0435.md",
            "the branch naming question came up again and was settled " * 3,
            claims=[
                {"subject": "ohmyboring", "predicate": "branch-name", "value": "fix/...", "kind": "decision"}
            ],
            total=4,
        ),
    ]
    out = sc.answer("브랜치 이름 어떻게 정했더라", search=lambda q, **kw: hits)
    assert "*wiki-0435.md*" in out.text
    assert "[decision] ohmyboring branch-name: fix/..." in out.text
    assert "declared 4, handed over 1" in out.text, "the cut is stated to a person too"


def test_the_search_is_asked_for_claims_like_the_hook_is():
    seen = {}

    def search(q, **kw):
        seen.update(kw)
        return []

    sc.answer("아무 질문이나", search=search)
    assert seen["claims"] == sc.CLAIMS_PER_HIT >= 1
    assert seen["max_results"] == sc.MAX_HITS


def test_nothing_found_and_engine_down_are_different_answers():
    assert sc.answer("이 주제는 없다", search=lambda q, **kw: []).text == sc.NOTHING_FOUND

    def down(q, **kw):
        raise ConnectionError("refused")

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
        _hit("wiki-0999.md", ""),  # no snippet: rendered by neither, so handed by neither
        _hit("wiki-1000.md", "the pool question came up again and was settled " * 3),
        _hit("wiki-1001.md", "the cron question came up again and was settled " * 3),
    ]
    out = sc.answer("브랜치 이름 어떻게 정했더라", search=lambda q, **kw: hits)
    assert [h["source_path"] for h in out.hits] == [
        "/vault/wiki/wiki-0435.md",
        "/vault/wiki/wiki-1000.md",
    ], "the empty snippet drops out and the MAX_HITS cut still applies"
    assert "① " in out.text and "② " in out.text and "③" not in out.text, (
        "one circled number per handed note, nothing else"
    )
    assert len(out.hits) == 2
    assert "wiki-0999" not in out.text and "wiki-1001" not in out.text


def test_empty_and_failed_answers_carry_no_hits():
    assert sc.answer("이 주제는 없다", search=lambda q, **kw: []).hits == []

    def down(q, **kw):
        raise ConnectionError("refused")

    assert sc.answer("엔진이 죽었다", search=down).hits == []


def test_the_client_hands_paths_over_under_the_sessions_own_name():
    client = dc.DrudgeClient(base_url="http://drudge.test", retries=0)
    sent = []
    client._retry = lambda method, path, payload=None, timeout=None: (
        sent.append({"method": method, "path": path, "payload": payload})
        or {"session": "s", "handed": 1, "unknown": []}
    )
    client.handover("s", "2026-09-21T00:00:00+00:00", ["/vault/wiki/wiki-0435.md"])
    assert sent[0]["method"] == "POST" and sent[0]["path"] == "/handover"
    assert sent[0]["payload"] == {
        "session_id": "s",
        "observed_at": "2026-09-21T00:00:00+00:00",
        "paths": ["/vault/wiki/wiki-0435.md"],
    }


def test_the_client_sends_a_verdict_without_path_lists():
    client = dc.DrudgeClient(base_url="http://drudge.test", retries=0)
    sent = []
    client._retry = lambda method, path, payload=None, timeout=None: (
        sent.append(payload) or {"used": 0, "contested": 0}
    )
    client.consumption("probe:e1b", "2026-09-21T00:00:00+00:00", verdict="used")
    assert sent[0]["verdict"] == "used"
    assert "used" not in sent[0] and "contested" not in sent[0], (
        "paths beside a verdict are a 400; the verdict must travel alone"
    )


def test_the_client_refuses_a_verdict_beside_path_lists():
    # Silently dropping the lists would answer 200 for a request the engine never judged.
    client = dc.DrudgeClient(base_url="http://drudge.test", retries=0)
    sent = []
    client._retry = lambda method, path, payload=None, timeout=None: sent.append(payload) or {}
    try:
        client.consumption("k", "2026-09-21T00:00:00+00:00", used=["/vault/wiki/a.md"], verdict="used")
    except ValueError:
        pass
    else:
        raise AssertionError("verdict beside paths must raise, not send")
    assert sent == [], "nothing may reach the engine when the call is ambiguous"


def test_remember_handed_hands_the_engine_paths():
    key = "slack:C123:1726900000.000100"
    hits = [
        _hit("wiki-0435.md", "the branch naming question came up again and was settled " * 3),
        _hit("wiki-1000.md", "the pool question came up again and was settled " * 3),
    ]
    calls, fake = _handover_recorder()
    assert sc.remember_handed(key, "브랜치 이름 어떻게 정했더라", hits, handover=fake) is True
    assert calls[0]["session_id"] == key
    assert calls[0]["paths"] == ["/vault/wiki/wiki-0435.md", "/vault/wiki/wiki-1000.md"]
    assert calls[0]["observed_at"], "a default timestamp is stamped on the handover"


def test_remember_handed_with_empty_hands_sends_nothing():
    calls, fake = _handover_recorder()
    assert sc.remember_handed("slack:C123:1726900000.000300", "질문", [], handover=fake) is False
    assert calls == [], "empty hands mean nothing was handed over, so nothing is sent"


def test_remember_handed_survives_a_dead_engine():
    def boom(*a, **kw):
        raise ConnectionError("refused")

    assert (
        sc.remember_handed(
            "slack:C123:1726900000.000500",
            "질문",
            [_hit("wiki-0435.md", "the branch naming question came up again and was settled " * 3)],
            handover=boom,
        )
        is False
    )


def test_feedback_sends_only_the_verdict():
    calls, fake = _verdict_recorder()
    out = sc.feedback("slack:C123:1726900000.000100", "used", consumption=fake)
    assert calls[0]["session_id"] == "slack:C123:1726900000.000100"
    assert calls[0]["verdict"] == "used"
    assert calls[0]["observed_at"], "a default timestamp is stamped on the verdict"
    assert out == {"used": 1, "contested": 0, "unknown": []}


def test_feedback_contested_sends_the_other_verdict():
    calls, fake = _verdict_recorder()
    sc.feedback("slack:C123:1726900000.000200", "contested", consumption=fake)
    assert calls[0]["verdict"] == "contested"


def test_feedback_on_an_answer_the_engine_does_not_know_is_unknown():
    calls, fake = _verdict_recorder(result={"used": 0, "contested": 0, "unknown": []})
    out = sc.feedback("slack:C123:1726900000.000999", "used", consumption=fake)
    assert calls[0]["verdict"] == "used", "the verdict still travels; the engine is what knows nothing"
    assert out["unknown_answer"] is True
    assert out["used"] == 0 and out["contested"] == 0


def test_feedback_rejects_a_verdict_that_is_not_a_verdict():
    calls, fake = _verdict_recorder()
    try:
        sc.feedback("slack:C123:x", "meh", consumption=fake)
    except ValueError:
        pass
    else:
        raise AssertionError("'meh' is not an edge the engine has; it must be refused, not sent")
    assert calls == []


def test_feedback_survives_a_dead_engine():
    def boom(*a, **kw):
        raise ConnectionError("refused")

    assert sc.feedback("slack:C123:x", "used", consumption=boom) == {"error": "refused"}


def test_an_answer_numbers_its_notes_under_a_head():
    hits = [
        _hit("wiki-0435.md", "the branch naming question came up again and was settled " * 3),
        _hit("wiki-1000.md", "the pool question came up again and was settled " * 3),
    ]
    out = sc.answer("브랜치 이름 어떻게 정했더라", search=lambda q, **kw: hits)
    assert out.text.startswith("_기억에서 찾은 것 2개_"), (
        "the head names the count and marks the message as ours"
    )
    assert "① *wiki-0435.md*" in out.text and "② *wiki-1000.md*" in out.text


def test_parse_correction_shapes():
    assert sc.parse_correction("정정: 재시작은 2시에 한다") == (None, "재시작은 2시에 한다")
    assert sc.parse_correction("정정 2: 재시작은 2시에 한다") == (2, "재시작은 2시에 한다")
    assert sc.parse_correction("정정：전각 콜론도 된다") == (None, "전각 콜론도 된다")
    assert sc.parse_correction("  정정 :  앞뒤 공백  ") == (None, "앞뒤 공백")
    assert sc.parse_correction("그냥 정정 얘기하는 문장") is None, "정정 이 문장 중간에 있으면 대화다"
    assert sc.parse_correction("정정: ") is None, "빈 정정은 정정이 아니다"


def _remember_recorder():
    calls = []

    def fake(title, body, **kw):
        calls.append({"title": title, "body": body, **kw})
        return {
            "source_path": "/vault/wiki/wiki-1077.md",
            "wiki_id": "wiki-1077",
            "duplicate": None,
            "supersedes": len(kw.get("supersedes") or []),
            "unknown": 0,
        }

    return calls, fake


def test_correct_without_a_number_supersedes_the_answers_one_note():
    calls, fake = _remember_recorder()
    out = sc.correct(
        "slack:C123:1.000",
        "배포 언제였더라",
        ["/vault/wiki/wiki-0435.md"],
        "정정: 재시작은 매일 2시에 한다",
        remember=fake,
    )
    assert calls[0]["supersedes"] == ["/vault/wiki/wiki-0435.md"]
    assert calls[0]["tags"] == ["correction", "slack"]
    assert calls[0]["body"] == "질문: 배포 언제였더라\n\n정정: 재시작은 매일 2시에 한다"
    assert calls[0]["title"] == "재시작은 매일 2시에 한다"
    assert out["source_path"] == "/vault/wiki/wiki-1077.md", "the engine's answer travels back untouched"


def test_correct_with_a_number_supersedes_only_that_note():
    calls, fake = _remember_recorder()
    sc.correct(
        "slack:C123:1.000",
        "질문",
        ["/vault/wiki/wiki-0435.md", "/vault/wiki/wiki-1000.md", "/vault/wiki/wiki-1001.md"],
        "정정 2: 풀은 아니고 브랜치다",
        remember=fake,
    )
    assert calls[0]["supersedes"] == ["/vault/wiki/wiki-1000.md"], (
        "1-based, in the order the answer listed them"
    )


def test_correct_with_a_number_outside_the_answer_is_refused():
    calls, fake = _remember_recorder()
    out = sc.correct(
        "slack:C123:1.000", "질문", ["/vault/wiki/wiki-0435.md"], "정정 9: 없는 번호", remember=fake
    )
    assert out == {"error": "no such number"}
    assert calls == [], "nothing may reach the engine when the number points at no note"


def test_correct_titles_the_note_with_the_first_sentence_capped():
    calls, fake = _remember_recorder()
    sc.correct(
        "slack:C123:1.000",
        "",
        ["/vault/wiki/wiki-0435.md"],
        "정정: 첫 문장이다. 두 번째 문장은 노트 본문에만.",
        remember=fake,
    )
    assert calls[0]["title"] == "첫 문장이다."
    calls.clear()
    sc.correct("slack:C123:1.000", "", ["/vault/wiki/wiki-0435.md"], "정정: " + "아" * 100, remember=fake)
    assert calls[0]["title"] == "아" * 60


def test_correct_survives_a_dead_engine():
    def boom(*a, **kw):
        raise ConnectionError("refused")

    out = sc.correct("slack:C123:1.000", "질문", ["/vault/wiki/wiki-0435.md"], "정정: 무언가", remember=boom)
    assert out == {"error": "refused"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - secretary_core: answers from the engine, and a 👍/👎 closes the loop")
