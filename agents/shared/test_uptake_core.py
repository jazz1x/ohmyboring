#!/usr/bin/env python3
"""Tests for uptake_core.py — above all, that an injection cannot count as its own uptake."""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import uptake_core

SNIPPET = (
    "the connection pool died because deadpool recycled a socket the server had already closed "
    "and the retry loop kept handing the same broken object back to every caller"
)


def _hit(src="wiki-0007.md", snippet=SNIPPET):
    return {"source_path": f"/vault/wiki/{src}", "snippet": snippet}


def test_injection_is_not_its_own_uptake():
    # The injected block lands in the transcript as part of the user turn. Grep the whole
    # transcript and it matches itself; that is the failure this whole module guards.
    record = uptake_core.injection_record("s1", "why did the pool die", [_hit()], 3)
    transcript = (
        f"[user] 📚 My past work experience\n- [wiki-0007.md] {SNIPPET}\nwhy did the pool die\n"
        "[assistant] Let me look at the pool configuration first.\n"
    )
    r = uptake_core.session_uptake([record], transcript)
    used, total, used_prompts, prompts = r.used_hits, r.total_hits, r.used_prompts, r.total_prompts
    assert (used, total) == (0, 1), "the injected text quoting itself must not count"
    assert (used_prompts, prompts) == (0, 1)


def test_assistant_reusing_the_note_name_counts():
    record = uptake_core.injection_record("s1", "why did the pool die", [_hit()], 3)
    transcript = (
        f"[user] 📚 past experience\n- [wiki-0007.md] {SNIPPET}\nwhy did the pool die\n"
        "[assistant] Per wiki-0007.md this is the recycled-socket case again.\n"
    )
    r = uptake_core.session_uptake([record], transcript)
    used, total = r.used_hits, r.total_hits
    assert (used, total) == (1, 1)


def test_assistant_reusing_a_phrase_counts():
    record = uptake_core.injection_record("s1", "why did the pool die", [_hit()], 3)
    transcript = (
        f"[user] 📚 past experience\n- [wiki-0007.md] {SNIPPET}\nwhy did the pool die\n"
        "[assistant] Looks like deadpool recycled a socket the server had already closed.\n"
    )
    r = uptake_core.session_uptake([record], transcript)
    used, total = r.used_hits, r.total_hits
    assert (used, total) == (1, 1), "an assistant echoing the substance must count"


def test_a_phrase_the_user_already_said_is_not_evidence():
    # The agent would have said it anyway; crediting the injection here would turn uptake into
    # a similarity score between the prompt and the answer.
    prompt = "deadpool recycled a socket the server had already closed — why did the pool die"
    record = uptake_core.injection_record("s1", prompt, [_hit()], 3)
    transcript = (
        f"[user] {prompt}\n"
        "[assistant] Right, deadpool recycled a socket the server had already closed.\n"
    )
    r = uptake_core.session_uptake([record], transcript)
    used, total = r.used_hits, r.total_hits
    assert (used, total) == (0, 1)


def test_a_note_name_the_user_typed_is_not_evidence():
    # The subtraction applied to phrases was missing for the source name, which is the easiest
    # evidence for a user to hand the agent: naming the note in the prompt and having it echoed
    # back would have scored as uptake.
    prompt = "wiki-0007.md 다시 보고 pool 문제 정리해줘"
    record = uptake_core.injection_record("s1", prompt, [_hit()], 3)
    transcript = f"[user] {prompt}\n[assistant] wiki-0007.md 를 다시 읽어보겠습니다.\n"
    r = uptake_core.session_uptake([record], transcript)
    used, total = r.used_hits, r.total_hits
    assert (used, total) == (0, 1)


def test_a_source_name_must_match_on_word_boundaries():
    # Both sides are space-joined token streams, so a bare substring search let "pool.md" match
    # inside "connection-pool.md". Ledger sources are arbitrary basenames, not only wiki-NNNN.
    record = uptake_core.injection_record("s1", "why did it die", [_hit(src="pool.md")], 3)
    transcript = "[user] why did it die\n[assistant] see connection-pool.md for the details.\n"
    r = uptake_core.session_uptake([record], transcript)
    used, total = r.used_hits, r.total_hits
    assert (used, total) == (0, 1), "a longer name that merely contains ours is not our note"
    hit_transcript = "[user] why did it die\n[assistant] see pool.md for the details.\n"
    used = uptake_core.session_uptake([record], hit_transcript).used_hits
    assert used == 1, "the exact name must still count"


def test_a_long_running_session_does_not_lose_its_early_rows():
    # Sessions here get resumed across days. Row-by-row expiry would drop a live session's early
    # injections while another session's SessionEnd ran the prune, leaving it measured against a
    # denominator missing its own beginning.
    import json as _json
    import time as _time

    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "injections.jsonl")
        now = _time.time()
        # Expressed against the constant, not a literal: this test pinned "5 days is old" and
        # broke the moment the cutoff moved off 3, which is the wrong thing to notice about a
        # change to the cutoff.
        stale = now - (uptake_core.LEDGER_MAX_AGE_DAYS + 2) * 86400
        early = uptake_core.injection_record("long", "p", [_hit()], 3)
        early["ts"] = stale                       # older than the cutoff …
        recent = uptake_core.injection_record("long", "p", [_hit()], 3)
        recent["ts"] = now                        # … but the session is still alive
        dead = uptake_core.injection_record("dead", "p", [_hit()], 3)
        dead["ts"] = stale
        Path(path).write_text(
            "\n".join(_json.dumps(r, ensure_ascii=False) for r in (early, recent, dead)) + "\n",
            encoding="utf-8",
        )
        ok, aged_sessions, aged_rows = uptake_core.prune_session("unrelated", path, now=now)
        assert ok is True
        # The dead session leaves a count behind: its injections happened and were
        # never scored, and this is the only trace before the rows are deleted.
        assert (aged_sessions, aged_rows) == (1, 1), (aged_sessions, aged_rows)
        assert len(uptake_core.load_records("long", path)) == 2, "a live session keeps its history"
        assert uptake_core.load_records("dead", path) == [], "a session with no recent row ages out"


def test_controls_are_scored_but_never_injected():
    # Controls are hits the search returned and the hook did not inject. The agent cannot have
    # used them, so their rate is the chance rate the treatment number must beat.
    control = {"source_path": "/vault/wiki/wiki-0099.md", "snippet": SNIPPET.replace("pool", "queue")}
    record = uptake_core.injection_record("s1", "why did it die", [_hit()], 3, controls=[control])
    assert [h["src"] for h in record["hits"]] == ["wiki-0007.md"]
    assert [c["src"] for c in record["controls"]] == ["wiki-0099.md"]

    transcript = (
        "[user] why did it die\n"
        "[assistant] wiki-0099.md 를 보면 답이 있습니다.\n"   # echoes the CONTROL, not the hit
    )
    r = uptake_core.session_uptake([record], transcript)
    assert (r.used_hits, r.total_hits) == (0, 1), "the injected note was not echoed"
    assert (r.used_controls, r.total_controls) == (1, 1), "the control was, and that is the floor"
    # The pre-registered metric compares per-prompt rates on both sides, so the control needs the
    # same shape as the treatment. Counting only control hits answered a different ratio.
    assert (r.used_control_prompts, r.total_prompts) == (1, 1)


def test_a_record_with_no_controls_still_works():
    record = uptake_core.injection_record("s1", "p", [_hit()], 3)
    assert record["controls"] == []
    r = uptake_core.session_uptake([record], "[assistant] x\n")
    assert (r.used_controls, r.total_controls) == (0, 0)
    assert r.used_control_prompts == 0


def test_control_prompts_count_prompts_not_hits():
    """A prompt where two controls landed is one prompt, the same way treatment counts it.

    Without this the control rate is inflated relative to the treatment rate it is subtracted
    from, and the gap in percentage points -- the thing the contract's thresholds are written in
    -- stops meaning anything.
    """
    two = [
        {"source_path": "/vault/wiki/wiki-0099.md", "snippet": SNIPPET.replace("pool", "queue")},
        {"source_path": "/vault/wiki/wiki-0100.md", "snippet": SNIPPET.replace("pool", "cache")},
    ]
    record = uptake_core.injection_record("s1", "why did it die", [_hit()], 3, controls=two)
    transcript = "[user] why did it die\n[assistant] wiki-0099.md 와 wiki-0100.md 둘 다 봤다.\n"

    r = uptake_core.session_uptake([record], transcript)

    assert r.used_controls == 2, "both control hits were echoed"
    assert r.used_control_prompts == 1, "but that is one prompt, not two"
    assert r.total_prompts == 1


def test_only_assistant_turns_are_scanned():
    record = uptake_core.injection_record("s1", "unrelated question", [_hit()], 3)
    transcript = (
        "[user] unrelated question\n"
        f"[user] deadpool recycled a socket the server had already closed\n"
        "[assistant] I have no idea.\n"
    )
    used = uptake_core.session_uptake([record], transcript).used_hits
    assert used == 0, "a later user turn is not the agent using the memory"


def test_assistant_text_extracts_only_assistant_bodies():
    text = "[user] alpha\n[assistant] beta\n[user] gamma\n[assistant] delta\n"
    got = uptake_core.assistant_text(text)
    assert "beta" in got and "delta" in got
    assert "alpha" not in got and "gamma" not in got


def test_phrases_spread_across_the_snippet_not_just_the_head():
    # Distilled notes all begin with the same section headers, so head-only fingerprints would
    # match the template rather than the content.
    body = "## 배경 문제 " + " ".join(f"word{i}" for i in range(60))
    got = uptake_core.phrases(body)
    assert len(got) == uptake_core.MAX_PHRASES
    assert "배경" in got[0], "the first window still starts at the head"
    assert all("배경" not in p for p in got[1:]), "later windows must clear the boilerplate"
    assert any("word4" in p for p in got[-1:]), "the last window must reach the snippet's tail"
    assert len(set(got)) == len(got), "windows must not be duplicates"


def test_short_snippets_yield_no_phrases():
    assert uptake_core.phrases("too short") == []


def test_a_record_without_a_session_id_is_never_written():
    # SessionEnd looks records up by session id; one without an id can only inflate the
    # denominator. It is also the guard that keeps test suites out of the live ledger.
    assert uptake_core.injection_record("", "a real prompt", [_hit()], 3) is None
    assert uptake_core.injection_record(None, "a real prompt", [_hit()], 3) is None


def test_record_is_none_when_nothing_injectable():
    assert uptake_core.injection_record("s1", "p", [], 3) is None
    assert uptake_core.injection_record("s1", "p", [{"source_path": "", "snippet": ""}], 3) is None


def test_record_respects_max_results():
    hits = [_hit(f"wiki-000{i}.md") for i in range(5)]
    record = uptake_core.injection_record("s1", "p", hits, 2)
    assert len(record["hits"]) == 2


def test_ledger_roundtrip_and_prune_keep_other_sessions():
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "injections.jsonl")
        uptake_core.append_record(uptake_core.injection_record("s1", "p", [_hit()], 3), path)
        uptake_core.append_record(uptake_core.injection_record("s2", "p", [_hit()], 3), path)
        assert len(uptake_core.load_records("s1", path)) == 1
        uptake_core.prune_session("s1", path)
        assert uptake_core.load_records("s1", path) == []
        assert len(uptake_core.load_records("s2", path)) == 1, "pruning one session must not touch another"


def test_abandoned_sessions_are_pruned_by_age():
    # A session killed before SessionEnd leaves rows nothing will ever measure or remove.
    import json as _json
    import time as _time

    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "injections.jsonl")
        old = uptake_core.injection_record("dead", "p", [_hit()], 3)
        old["ts"] = _time.time() - (uptake_core.LEDGER_MAX_AGE_DAYS + 7) * 86400
        Path(path).write_text(_json.dumps(old, ensure_ascii=False) + "\n", encoding="utf-8")
        uptake_core.append_record(uptake_core.injection_record("fresh", "p", [_hit()], 3), path)

        uptake_core.prune_session("unrelated", path)
        assert uptake_core.load_records("dead", path) == [], "an abandoned session must age out"
        assert len(uptake_core.load_records("fresh", path)) == 1, "recent rows must survive"


def test_ledger_failures_are_silent_not_fatal():
    # A ledger write must never cost the user their prompt.
    assert uptake_core.append_record({"session_id": "s"}, "/nonexistent-root-dir/x/y.jsonl") is False
    assert uptake_core.load_records("s1", "/nonexistent-root-dir/x/y.jsonl") == []


def test_malformed_ledger_lines_are_skipped_not_crashed():
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "injections.jsonl")
        Path(path).write_text('not json\n{"session_id": "s1", "hits": []}\n', encoding="utf-8")
        assert len(uptake_core.load_records("s1", path)) == 1


def test_ledger_path_is_read_per_call_not_frozen_at_import():
    # A constant captured at import time made the redirect depend on import order, and the hook
    # suite appended seven rows to the owner's live ledger before this was caught.
    import os

    default = uptake_core.ledger_path()
    os.environ["BORING_INJECTION_LEDGER"] = "/tmp/redirected-after-import.jsonl"
    try:
        assert uptake_core.ledger_path() == "/tmp/redirected-after-import.jsonl"
    finally:
        os.environ.pop("BORING_INJECTION_LEDGER", None)
    assert uptake_core.ledger_path() == default


def test_snippet_text_is_not_stored_in_the_ledger():
    # The ledger must not become a second copy of the vault.
    record = uptake_core.injection_record("s1", "p", [_hit()], 3)
    blob = json.dumps(record, ensure_ascii=False)
    assert SNIPPET not in blob
    assert "wiki-0007.md" in blob


def test_a_prompt_recorded_twice_in_the_same_instant_is_one_prompt():
    """The shape #245 left behind: one UserPromptSubmit, two hook registrations, two rows.

    Nothing in the rate looks wrong when this happens — both halves of the fraction double — so
    the only place it can be caught is here, before it inflates a pre-registered sample floor.
    """
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "injections.jsonl")
        first = uptake_core.injection_record("s1", "why did it die", [_hit()], 3)
        twin = dict(first, ts=first["ts"] + 0.0001)     # the second hook, same instant
        later = dict(first, ts=first["ts"] + 900)       # the person genuinely asking again
        other = uptake_core.injection_record("s1", "unrelated", [_hit()], 3)
        Path(path).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in (first, twin, later, other))
            + "\n",
            encoding="utf-8",
        )

        extra, total, sessions = uptake_core.duplicate_injections(path)

        assert (extra, total, sessions) == (1, 4, 1), (extra, total, sessions)


def test_a_repeat_outside_the_window_is_a_real_repeat():
    """Fifteen minutes later is a person, not a second hook — counting it would erase evidence."""
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "injections.jsonl")
        first = uptake_core.injection_record("s1", "same question", [_hit()], 3)
        later = dict(first, ts=first["ts"] + 900)
        Path(path).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in (first, later)) + "\n",
            encoding="utf-8",
        )

        assert uptake_core.duplicate_injections(path)[0] == 0


def test_a_missing_ledger_is_not_a_duplicate_report():
    assert uptake_core.duplicate_injections("/nonexistent/injections.jsonl") == (0, 0, 0)


#: The longest session span measured in the live ledger on 2026-09-02 was 177h (7.4 days), with a
#: median of 48h. A cutoff below that stops guarding against sessions that never end and starts
#: discarding the ones that run longest — which are also the ones carrying the most injections.
OBSERVED_MAX_SESSION_DAYS = 7.4


def test_the_cutoff_outlives_the_longest_session_and_still_expires():
    """Both halves matter, and each kills a different mistake.

    Too short and long-running sessions lose their ledger before SessionEnd can score them (the
    3-day cutoff dropped 875 of 1125 rows this way — docs/PRD.md §8 D4). Never expiring and the
    rows of sessions that were killed before SessionEnd pile up forever, counted against
    transcripts that no longer exist.
    """
    import json as _json
    import time as _time

    assert uptake_core.LEDGER_MAX_AGE_DAYS > OBSERVED_MAX_SESSION_DAYS, (
        "the cutoff must clear the longest session actually observed, not a guessed one"
    )
    # And an upper bound, or "never expire" reads as a valid cutoff. 30 is not a taste: it is
    # `scripts/retention.py` DEFAULT_PROCESSED_DAYS, when a processed session's transcript is
    # archived. A ledger row older than that has nothing left to be scored against.
    assert uptake_core.LEDGER_MAX_AGE_DAYS <= 30, (
        "past transcript retention a ledger row can never be scored, only carried"
    )

    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "injections.jsonl")
        row = uptake_core.injection_record("abandoned", "p", [_hit()], 3)
        row["ts"] = _time.time() - (uptake_core.LEDGER_MAX_AGE_DAYS + 1) * 86400
        Path(path).write_text(_json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        uptake_core.prune_session("unrelated", path)
        assert uptake_core.load_records("abandoned", path) == [], (
            "expiry must still happen — an unbounded ledger is the other failure"
        )


def test_cross_session_scoring_pairs_every_session_with_one_it_never_saw():
    """§2's self-check: hits a session never received must not turn up in its answer.

    The sensitivity probe asks whether the detector can see a use; this asks whether it sees uses
    that are not there. Both pairs come out of one pass over the same sessions, because the bar is
    the cross rate *relative to* the treatment rate and a treatment rate from a different set of
    sessions would not be that comparison.
    """
    import tempfile

    ledger = os.path.join(tempfile.mkdtemp(), "inj.jsonl")
    phrase_a = "판정 원장에 테스트가 쓸 수 없다 라는 조항이"
    phrase_b = "관측창이 대상보다 짧다는 것은 결측이 아니라"
    with open(ledger, "w", encoding="utf-8") as handle:
        for sid, phrase in (("a-session", phrase_a), ("b-session", phrase_b)):
            handle.write(json.dumps({
                "session_id": sid, "ts": 1,
                "prompt_words": ["질문"],
                "hits": [{"src": f"{sid}.md", "phrases": [phrase]}],
                "controls": [],
            }, ensure_ascii=False) + "\n")

    # Each transcript echoes its OWN phrase and nothing else.
    def transcript_for(sid):
        own = phrase_a if sid == "a-session" else phrase_b
        return f"[user] 질문\n[assistant] {own}"

    (cross_used, cross_total), (own_used, own_total) = uptake_core.cross_session_rate(
        transcript_for, ledger
    )
    assert cross_total == 2 and own_total == 2, (cross_total, own_total)
    assert cross_used == 0, "a phrase the session never received was counted as used"
    assert own_used == 2, "the same scorer must find the phrase each session did receive"


def test_the_self_check_cannot_be_run_on_a_ledger_with_nothing_to_pair():
    """One session has no other session to be scored against, and that is not a clean result."""
    import tempfile

    ledger = os.path.join(tempfile.mkdtemp(), "inj.jsonl")
    with open(ledger, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "session_id": "only", "ts": 1, "prompt_words": [],
            "hits": [{"src": "x.md", "phrases": ["아무 문구"]}], "controls": [],
        }, ensure_ascii=False) + "\n")

    assert uptake_core.cross_session_rate(lambda _s: "x", ledger) == ((0, 0), (0, 0))
    # A path that cannot be read is the same answer, not a pass.
    assert uptake_core.cross_session_rate(lambda _s: "x", "/nonexistent") == ((0, 0), (0, 0))



def test_a_raw_transcript_gives_the_detector_nothing_to_read():
    raw = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "토큰 예산을 늘렸다"}]}}
    )
    assert uptake_core.assistant_text(raw) == "", "a raw jsonl line is not turn-formatted"
    extracted = "[user] 뭐지\n[assistant] 토큰 예산을 늘렸다"
    assert "토큰 예산을 늘렸다" in uptake_core.assistant_text(extracted)


def test_the_transcript_format_follows_the_directory():
    assert uptake_core._transcript_format("/Users/x/.codex/sessions/a.jsonl") == "codex-jsonl"
    assert uptake_core._transcript_format("/Users/x/.claude/projects/p/a.jsonl") == "claude-json"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - uptake_core: injections cannot count as their own uptake")
