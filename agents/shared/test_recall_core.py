#!/usr/bin/env python3
"""Regression tests for recall_core.py session throttle."""
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.pop("BORING_CONFIG", None)
os.environ.pop("BORING_HOME", None)

import recall_core


def _tmp_throttle():
    """Point the throttle file at a temp location for isolated tests."""
    d = tempfile.mkdtemp()
    recall_core._throttle_path = lambda: os.path.join(d, "throttle.json")


def test_session_throttle_blocks_repeated_calls():
    _tmp_throttle()
    assert recall_core._session_throttled("s1") is False
    assert recall_core._session_throttled("s1") is True
    assert recall_core._session_throttled("s2") is False


def test_session_throttle_expires_after_window():
    # Moves the clock instead of sleeping. The sleeping version asserted on a one-second window
    # and went red inside guard.sh once under machine load while passing standalone — a timing
    # test is a load test nobody asked for, and it also spent 1.1s of every CI run.
    _tmp_throttle()
    import time as _time

    original_ttl = recall_core.SESSION_THROTTLE_SECONDS
    original_time = recall_core.time.time
    clock = [1_000_000.0]
    try:
        recall_core.SESSION_THROTTLE_SECONDS = 3600
        recall_core.time.time = lambda: clock[0]
        assert recall_core._session_throttled("s3") is False
        assert recall_core._session_throttled("s3") is True
        clock[0] += 3601
        assert recall_core._session_throttled("s3") is False
    finally:
        recall_core.time.time = original_time
        recall_core.SESSION_THROTTLE_SECONDS = original_ttl
        del _time


def test_empty_session_id_never_throttled():
    _tmp_throttle()
    assert recall_core._session_throttled(None) is False
    assert recall_core._session_throttled("") is False



def test_the_snippet_carries_the_decision_not_only_the_background():
    """A head-only slice injects the diagnosis and leaves the prescription behind.

    Distilled notes run 배경 → 실측 → 뿌리 → 결정. Measured 2026-09-09: `wiki-1636` was injected
    nine times and every one carried phrases from its 배경/실측; its 결정 lines sit at 721–860
    characters and were injected zero times. The note that named a repeating mistake could not
    deliver the fix for it, nine times over.
    """
    note = (
        "## 배경 / 문제 " + ("배경 " * 60)
        + "## 실측 " + ("측정값 " * 60)
        + "## 결정 노드 완료는 커밋 수다. pgrep 로 판정 금지."
    )
    out = recall_core.salient(note)
    assert len(out) <= recall_core.SNIPPET_CHARS, len(out)
    assert "노드 완료는 커밋 수다" in out, out
    assert out.startswith("## 배경"), "the note must still say what it is about"
    assert "…" in out, "the splice has to be visible, not a silent reflow"


def test_short_and_unstructured_notes_are_untouched():
    """The rule may only change long structured notes; everything else keeps the old behaviour."""
    # Identity, and identity of the object that came in — a note under the cap must come back
    # whole, not merely come back the same length. Slicing at `limit` returns an equal string for
    # a short note, so `== short` alone passes for a rule that slices everything.
    short = "## 배경 짧은 노트 " + ("가 " * 20)
    flat_short = " ".join(short.split())
    assert recall_core.salient(short) == flat_short
    assert len(flat_short) < recall_core.SNIPPET_CHARS, "fixture must sit under the cap"

    # And one exactly at the boundary: off-by-one here silently drops a character from every note
    # that happens to be cap-length.
    exact = "가" * recall_core.SNIPPET_CHARS
    assert recall_core.salient(exact) == exact

    # No decision heading: nothing to splice to, so it is a plain head slice.
    flat = "배경 " * 200
    plain = recall_core.salient(flat)
    assert plain == " ".join(flat.split())[: recall_core.SNIPPET_CHARS]

    # A decision that already falls inside the head slice is being carried; splicing there would
    # spend the budget printing the same words twice.
    early = "## 배경 짧다 ## 결정 여기 " + ("꼬리 " * 200)
    assert "…" not in recall_core.salient(early)

    assert recall_core.salient("") == ""
    assert recall_core.salient(None) == ""


def _hit(name, text):
    return {"source_path": f"/vault/wiki/{name}", "snippet": text, "dist": 0.3, "dist_kind": "vector_cosine"}


def _recall(hits, session_id="s1", prompt="why did the connection pool die again", ledger=None):
    """Drive the real hook path with a stubbed engine and a spooled ledger; return the injected text."""
    import io
    import json
    from contextlib import redirect_stdout
    from unittest import mock

    with tempfile.TemporaryDirectory() as d:
        env = {"BORING_INJECTION_LEDGER": ledger or os.path.join(d, "ledger.jsonl"), "BORING_EVENT_SINK": "spool"}
        with mock.patch.dict(os.environ, env), mock.patch.object(recall_core, "DrudgeClient") as client:
            client.return_value.search.return_value = hits
            out = io.StringIO()
            with redirect_stdout(out):
                recall_core.run_recall({"prompt": prompt, "session_id": session_id})
    raw = out.getvalue().strip()
    if not raw:
        return ""
    return json.loads(raw)["hookSpecificOutput"]["additionalContext"]


def _ledger_sources(ledger):
    import json

    rows = [json.loads(l) for l in open(ledger, encoding="utf-8") if l.strip()]
    return [([h["src"] for h in r["hits"]], [c["src"] for c in r["controls"]]) for r in rows]


def test_a_note_already_given_this_session_is_not_given_again():
    """§8 D6: 34% of in-session injections were repeats, one note 25 times in one session."""
    pool = [_hit(f"wiki-{i:04d}.md", f"note {i} says the socket was recycled by deadpool " * 3) for i in range(5)]
    with tempfile.TemporaryDirectory() as d:
        ledger = os.path.join(d, "ledger.jsonl")
        first = _recall(pool, ledger=ledger)
        assert "[wiki-0000.md]" in first and "[wiki-0002.md]" in first and "[wiki-0003.md]" not in first
        second = _recall(pool, ledger=ledger, prompt="the pool died again, second prompt")
        assert "[wiki-0000.md]" not in second, "already in the agent's context"
        assert "[wiki-0003.md]" in second and "[wiki-0004.md]" in second, second
        injected, controls = _ledger_sources(ledger)[1]
        assert injected == ["wiki-0003.md", "wiki-0004.md"]
        assert controls == [], "a control the agent has already seen is contaminated"
        third = _recall(pool, ledger=ledger, prompt="and a third time")
        assert third == "", "nothing fresh means nothing injected — not a repeat"
        other = _recall(pool, session_id="s2", ledger=ledger)
        assert "[wiki-0000.md]" in other, "another session has not seen it"


def test_the_injection_carries_the_note_each_hit_connects_to():
    """The engine walks concept edges per hit (#318). The line under a hit is the thread the note
    belongs to, and it is injected — so it is ledgered, deduplicated and scored like any hit."""
    text = "deadpool recycled a closed socket and the retry loop handed it back " * 3
    older = {"source_path": "/vault/wiki/wiki-0001.md", "snippet": "the earlier pool incident: idle timeout below the LB's " * 3}
    hit = dict(_hit("wiki-0007.md", text), related=[older])
    twin = dict(_hit("wiki-0008.md", text), related=[older, {"source_path": "/vault/wiki/wiki-0002.md", "snippet": "second thread " * 8}])
    with tempfile.TemporaryDirectory() as d:
        ledger = os.path.join(d, "ledger.jsonl")
        ctx = _recall([hit, twin], ledger=ledger)
        assert "- [wiki-0007.md]" in ctx
        assert "↳ shares a concept with [wiki-0001.md]" in ctx, ctx
        assert ctx.count("[wiki-0001.md]") == 1, "the same older note is not attached twice"
        assert "[wiki-0002.md]" in ctx
        injected, _ = _ledger_sources(ledger)[0]
        assert injected == ["wiki-0007.md", "wiki-0008.md", "wiki-0001.md", "wiki-0002.md"], injected
        again = _recall([hit, twin], ledger=ledger, prompt="the pool died again, second prompt")
        assert again == "", "hits and their related notes were all given already"
        new_hit = dict(_hit("wiki-0009.md", text), related=[older])
        later = _recall([new_hit], ledger=ledger, prompt="a third prompt on the same pool")
        assert "- [wiki-0009.md]" in later
        assert "[wiki-0001.md]" not in later, "a related note given in an earlier prompt is not given again"


def test_the_engine_asked_for_related_notes_on_every_pool_hit():
    """Dedup can promote pool hit 4 to injected, so related has to be there for every hit."""
    from unittest import mock

    with tempfile.TemporaryDirectory() as d, mock.patch.dict(
        os.environ, {"BORING_INJECTION_LEDGER": os.path.join(d, "l.jsonl"), "BORING_EVENT_SINK": "spool"}
    ), mock.patch.object(recall_core, "DrudgeClient") as client:
        client.return_value.search.return_value = []
        recall_core.run_recall({"prompt": "why did the connection pool die again", "session_id": "s1"})
    kwargs = client.return_value.search.call_args.kwargs
    assert kwargs["related"] == 1
    assert kwargs["related_heads"] == kwargs["max_results"] == recall_core.MAX_RESULTS + recall_core.CONTROL_RESULTS


def test_what_earlier_sessions_did_with_a_note_reorders_the_pool_and_shows():
    """A note reused before goes first, a note argued with more than reused goes to the back,
    and the agent is told both. The engine's order survives among untouched notes."""
    text = "the pool died because deadpool recycled a closed socket " * 3
    pool = [
        dict(_hit("wiki-0001.md", text), used_count=1, contested_count=3),
        _hit("wiki-0002.md", text),
        dict(_hit("wiki-0003.md", text), used_count=4, contested_count=1),
        _hit("wiki-0004.md", text),
        _hit("wiki-0005.md", text),
    ]
    with tempfile.TemporaryDirectory() as d:
        ledger = os.path.join(d, "ledger.jsonl")
        ctx = _recall(pool, ledger=ledger)
        injected, controls = _ledger_sources(ledger)[0]
        assert injected == ["wiki-0003.md", "wiki-0002.md", "wiki-0004.md"], injected
        assert controls == ["wiki-0005.md", "wiki-0001.md"], "the contested note fell out of the injection"
        assert "- [wiki-0003.md] (reused 4×, contested 1×) the pool" in ctx, ctx
        assert "- [wiki-0002.md] the pool" in ctx, "untouched notes carry no parenthesis"


def test_an_unreadable_ledger_keeps_the_injection():
    """§8 D6: re-injection is cheaper than omission, and the ledger can die before the session."""
    pool = [_hit("wiki-0007.md", "the pool died because deadpool recycled a closed socket " * 3)]
    with tempfile.TemporaryDirectory() as d:
        ctx = _recall(pool, ledger=os.path.join(d, "missing", "ledger.jsonl"))
        assert "[wiki-0007.md]" in ctx


def test_the_fence_says_how_to_use_what_it_injects():
    """Measured 2026-09-11: the fence carried only prohibitions, and the owner's diagnosis was
    that agents do not know what to do with the three lines. The protocol names the citation
    form the uptake scorer detects (`per <note>`), so following it is what gets measured."""
    ctx = _recall([_hit("wiki-0007.md", "the pool died because deadpool recycled a closed socket " * 3)])
    assert "not instructions" in ctx, "the prohibition stays"
    assert "per <note>" in ctx and "reuse it" in ctx, ctx
    assert "contradicts the code" in ctx, ctx
    assert "- [wiki-0007.md]" in ctx


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - recall_core session throttle · snippet carries the decision · fence protocol")
