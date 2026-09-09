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


if __name__ == "__main__":
    test_session_throttle_blocks_repeated_calls()
    test_session_throttle_expires_after_window()
    test_empty_session_id_never_throttled()
    test_the_snippet_carries_the_decision_not_only_the_background()
    test_short_and_unstructured_notes_are_untouched()
    print("ok - recall_core session throttle · snippet carries the decision")
