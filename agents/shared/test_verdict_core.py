#!/usr/bin/env python3
"""Tests for verdict_core.py — above all, that a short sample cannot be argued into a verdict."""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import verdict_core as V

REPO = HERE.parent.parent
PRD = REPO / "docs" / "PRD.md"


def test_a_short_sample_is_refused_before_any_rate_is_computed():
    # The floors were registered before the data existed so a thin window could not be talked
    # into a result afterwards. Rates that would otherwise read as a strong effect must not
    # change the label.
    v = V.verdict(sessions=5, used_prompts=50, total_prompts=100, used_control_prompts=1)
    assert v.label == V.REFUSED, v
    assert "세션 5" in v.reason and "주입 프롬프트 100" in v.reason, v.reason
    # The numbers are still carried so a reader can see what was withheld and why.
    assert v.treatment_pp == 50.0 and v.control_pp == 1.0


def test_sessions_alone_cannot_clear_the_floor():
    """Twenty one-prompt sessions is twenty sessions and almost no evidence."""
    v = V.verdict(sessions=25, used_prompts=5, total_prompts=25, used_control_prompts=0)
    assert v.label == V.REFUSED
    assert "주입 프롬프트" in v.reason and "세션" not in v.reason.split("—")[1]


def test_works_needs_the_ratio_and_the_gap_together():
    # 8pp vs 2pp: 4x and a 6pp gap.
    v = V.verdict(sessions=30, used_prompts=80, total_prompts=1000, used_control_prompts=20)
    assert v.label == V.WORKS, v

    # Same 4x ratio, but 0.8pp against 0.2pp. Ratio alone must not declare victory — and the
    # contract is stronger than "unproven" here: a gap inside the 1pp margin is 비작동, however
    # flattering the multiple looks.
    v = V.verdict(sessions=30, used_prompts=8, total_prompts=1000, used_control_prompts=2)
    assert v.label == V.BROKEN, v

    # A big gap with too small a ratio is not 작동 either: 30pp vs 25pp is 5pp apart but only
    # 1.2x, which is the shape of a corpus that echoes itself.
    v = V.verdict(sessions=30, used_prompts=300, total_prompts=1000, used_control_prompts=250)
    assert v.label == V.WITHHELD, v
    assert "대조 ×" in v.reason


def test_treatment_within_a_point_of_chance_is_declared_broken():
    # 2.0pp vs 1.5pp — a 0.5pp gap. The channel is doing what coincidence does.
    v = V.verdict(sessions=30, used_prompts=20, total_prompts=1000, used_control_prompts=15)
    assert v.label == V.BROKEN, v


def test_the_live_shape_of_the_window_lands_in_withheld():
    """The 2026-08-31 reading: 4.8x but only 1.85pp apart. Neither branch may claim it."""
    v = V.verdict(sessions=30, used_prompts=24, total_prompts=1026, used_control_prompts=5)
    assert v.label == V.WITHHELD, v
    assert v.ratio is not None and v.ratio > V.WORKS_RATIO
    assert v.gap_pp < V.WORKS_GAP_PP


def test_a_zero_control_is_not_an_infinite_effect():
    """No control events means the ratio has no denominator; the gap has to carry the claim."""
    v = V.verdict(sessions=30, used_prompts=100, total_prompts=1000, used_control_prompts=0)
    assert v.ratio is None
    assert v.label == V.WORKS, "a 10pp gap over a zero floor still clears the absolute bar"

    thin = V.verdict(sessions=30, used_prompts=5, total_prompts=1000, used_control_prompts=0)
    assert thin.label == V.BROKEN, "0.5pp over a zero floor is within the broken margin"


def _event(agent="claude-code", when="2026-09-01", control=1, **counts):
    row = {
        "event": "injection_uptake",
        "observed_at": f"{when}T00:00:00+00:00",
        "agent": agent,
        "used_prompts": counts.get("used_prompts", 2),
        "total_prompts": counts.get("total_prompts", 100),
    }
    if control is not None:
        row["used_control_prompts"] = control
    return row


def test_events_predating_the_control_counter_are_excluded_not_zeroed():
    """A missing measurement is not a chance rate of zero.

    Folding those rows in with `used_control_prompts = 0` would drag the control rate towards
    zero, which is the direction that makes the channel look effective — the single most
    flattering way to be wrong here.
    """
    rows = [_event(control=None), _event(control=None), _event(control=3)]

    per_agent, skipped = V.collect(rows)

    assert skipped == 2, skipped
    assert per_agent["claude-code"]["sessions"] == 1
    assert per_agent["claude-code"]["used_control_prompts"] == 3


def test_rows_are_not_pooled_across_adapters():
    """Claude Code injects every prompt; Kimi throttles to once a session. One rate answers
    neither, so the two never share a denominator."""
    rows = [_event(agent="claude-code"), _event(agent="kimi"), _event(agent="kimi")]

    per_agent, _ = V.collect(rows)

    assert set(per_agent) == {"claude-code", "kimi"}
    assert per_agent["kimi"]["sessions"] == 2


def test_since_and_agent_narrow_the_window():
    rows = [_event(when="2026-08-20"), _event(when="2026-09-02"), _event(agent="kimi")]

    recent, _ = V.collect(rows, since="2026-09-01")
    assert recent["claude-code"]["sessions"] == 1

    only, _ = V.collect(rows, agent="kimi")
    assert set(only) == {"kimi"}


def test_counters_are_read_from_attributes_when_that_is_where_they_are():
    """The store returns the numbers nested under `attributes`; a top-level-only read counts 0
    and a zero denominator refuses every verdict for a reason that is not true."""
    row = {
        "event": "injection_uptake",
        "observed_at": "2026-09-01T00:00:00+00:00",
        "attributes": {
            "agent": "claude-code",
            "used_prompts": 5,
            "total_prompts": 200,
            "used_control_prompts": 1,
        },
    }

    per_agent, skipped = V.collect([row])

    assert skipped == 0
    assert per_agent["claude-code"]["total_prompts"] == 200
    assert per_agent["claude-code"]["used_prompts"] == 5


def test_unmeasured_injections_are_reported_but_never_folded_into_the_rate():
    """A rate says what share of what it saw was used — not what share it saw.

    Sessions killed rather than exited never fire SessionEnd, so they are never scored. Folding
    them in either direction invents an outcome they do not have; leaving them out silently lets
    a biased sample be quoted as the population.
    """
    rows = [
        {"event": "injection_unreported", "aged_sessions": 2, "aged_rows": 40},
        {"event": "injection_unreported", "attributes": {"aged_sessions": 1, "aged_rows": 5}},
        # Carries the same keys under a different event name. Without the name filter this would
        # be summed in — and the coverage figure the verdict prints beside itself would count
        # something that is not an unmeasured injection at all.
        {"event": "ledger_maintenance", "aged_sessions": 99, "aged_rows": 999},
        _event(control=1, used_prompts=2, total_prompts=100),
    ]

    assert V.unreported(rows) == (3, 45)

    # The measured side is untouched by them.
    per_agent, _ = V.collect(rows)
    assert per_agent["claude-code"]["total_prompts"] == 100


def test_the_thresholds_still_match_the_registered_contract():
    """The numbers here are a transcription of docs/PRD.md §2, and transcriptions drift.

    A verdict computed from thresholds that quietly stopped matching the pre-registration is
    not a pre-registered verdict at all — it is a number chosen after seeing the data.
    """
    text = PRD.read_text(encoding="utf-8")
    section = text.split("## 2.")[1].split("\n## ")[0]
    for value, what in (
        (V.MIN_SESSIONS, "세션 하한"),
        (V.MIN_INJECTED_PROMPTS, "프롬프트 하한"),
        (int(V.WORKS_RATIO), "작동 배수"),
        (int(V.WORKS_GAP_PP), "작동 격차"),
        (int(V.BROKEN_MARGIN_PP), "비작동 여유"),
        # Dates and the midpoint gate travel with the thresholds. They used to be spelled in
        # peek.py, GOALS and a docstring at once, and the docstring still said the window closed
        # 2026-09-09 a day after the reset moved it to 09-14 — nobody could see that rot.
        (V.MIDPOINT_MIN_SCORED, "중간점 채점 세션 하한"),
    ):
        assert re.search(rf"\b{value}\b", section), f"{what} {value} 가 PRD §2 에 없다"
    for date, what in (
        (V.WINDOW_SINCE, "창 시작"),
        (V.WINDOW_UNTIL, "창 마감"),
        (V.MIDPOINT, "중간점"),
    ):
        assert date in section, f"{what} {date} 가 PRD §2 에 없다"
    # The zone is half the meaning of the dates: read as UTC they are a different day, and the
    # briefing runs at 08:00 in this one. A code comment registered that and the contract did not.
    hours = int(V.WINDOW_TZ.utcoffset(None).total_seconds() // 3600)
    assert f"UTC+{hours:02d}:00" in section, f"창 시간대 UTC+{hours:02d}:00 이 PRD §2 에 없다"


def test_the_repair_boundary_splits_by_instant_not_by_string():
    """Rows are compared as instants, and an unplaceable row falls on the side nobody reads.

    The two rows below are written with different UTC offsets and straddle the boundary. A lexical
    compare on `observed_at` — which is what the surrounding code does for the window's date
    bounds — puts the +09:00 row on the wrong side, and it would land in the half the verdict
    reads. That is the direction that matters: a pre-repair session counted as post is exactly the
    biased sample §8 D4 exists to keep out.
    """
    rows = [
        {"observed_at": "2026-09-11T09:00:00+09:00"},  # 00:00Z — before the repair
        {"observed_at": "2026-09-11T10:00:00+09:00"},  # 01:00Z — after it
        {"observed_at": None},
        {"observed_at": "not a timestamp"},
    ]
    pre, post = V.partition_at_repair(rows)
    assert [r["observed_at"] for r in post] == ["2026-09-11T10:00:00+09:00"], post
    assert len(pre) == 3, "an unplaceable row counts as pre — the half the verdict does not read"


def test_the_boundary_is_the_repair_commit_not_a_chosen_date():
    """A boundary someone can nudge is not a boundary. It has to trace to the commit."""
    assert V.LEDGER_REPAIR_AT.startswith("2026-09-11T00:31:53"), V.LEDGER_REPAIR_AT
    assert V._instant(V.LEDGER_REPAIR_AT) is not None


def test_coverage_takes_automated_runs_out_but_keeps_what_it_could_not_read():
    """§2 turns the verdict into an instrument investigation below half coverage.

    Measured 2026-09-07: 257 sessions distilled inside the window, 215 of them the security-review
    tool running itself. Counting those made coverage read 14% -- under the floor, so the contract
    would have refused a verdict -- when against people it was 83%. The instrument was not
    leaking; the denominator was not people.
    """
    raw, eligible, ok = V.coverage(257, 36)
    assert eligible == 257 and not ok, (raw, eligible, ok)
    assert 0.13 < raw < 0.15, raw

    ratio, eligible, ok = V.coverage(257, 35, automated_sessions=215)
    assert eligible == 42, eligible
    assert ok and 0.82 < ratio < 0.84, (ratio, ok)

    # A transcript that could not be read might be a person's. Subtracting it would let the
    # denominator shrink whenever the classifier fails, which is the direction that flatters
    # coverage -- so it stays in.
    with_unknown = V.coverage(257, 35, automated_sessions=215, unclassified_sessions=3)
    assert with_unknown == (ratio, eligible, ok), with_unknown


def test_coverage_reports_no_sample_rather_than_zero():
    """An empty window is not the worst possible coverage; it is no measurement at all."""
    ratio, eligible, ok = V.coverage(0, 0)
    assert ratio is None and eligible == 0 and not ok
    # Every session automated is the same case: nothing eligible, so nothing to divide.
    assert V.coverage(10, 0, automated_sessions=10)[0] is None


def test_the_self_check_is_measured_against_the_treatment_rate_not_a_constant():
    """§2's cross-session check asks how far chance sits from the treatment rate.

    A corpus whose notes share more vocabulary raises both numbers together, so an absolute
    ceiling would fail on wording rather than on validity. Live on 2026-09-07: 0.2% cross-session
    against 3.8% per-hit treatment.
    """
    assert V.self_check_verdict(0.002, 0.038) is True
    # Chance at three quarters of the treatment rate leaves nothing for the treatment to mean.
    assert V.self_check_verdict(0.03, 0.038) is False
    # No treatment rate is not a passing self-check; it is no answer.
    assert V.self_check_verdict(0.002, 0) is None
    assert V.self_check_verdict(0.002, None) is None



def test_two_zeros_are_not_a_verdict_until_the_detector_is_shown_to_see():
    """§2: 처치·대조 동시 0 은 "아무도 안 썼다"이기 전에 "검출기가 못 본다"일 수 있다.

    Live on 2026-09-08: treatment 0, control 0, denominator 515 — and the dashboard printed
    비작동 in the largest type on the page. The two readings are indistinguishable from the rates
    alone, which is exactly why the contract requires sensitivity to be shown first.
    """
    silent = V.verdict(29, 0, 515, 0)
    assert silent.label == V.REFUSED, silent
    assert "계측 조사" in silent.reason, silent.reason

    # Shown to see, and still nothing landed: that is a real "not working".
    shown = V.verdict(29, 0, 515, 0, detector_sensitive=True)
    assert shown.label == V.BROKEN, shown

    # Probed and blind is worse than unprobed, and says so differently.
    blind = V.verdict(29, 0, 515, 0, detector_sensitive=False)
    assert blind.label == V.REFUSED
    assert "손에 쥐여준" in blind.reason, blind.reason


def test_the_sensitivity_gate_only_guards_the_not_working_reading():
    """It must not become a way to withhold an inconvenient result.

    The clause is symmetric in the contract's words — it protects against reading a blind detector
    as evidence — so it may not touch a window where something was actually detected, in either
    direction, and it may not block 작동.
    """
    # One arm non-zero: the detector demonstrably sees, no gate needed.
    assert V.verdict(29, 2, 515, 3).label == V.BROKEN
    assert V.verdict(29, 0, 515, 4).label == V.BROKEN

    # Working is never gated, probe or no probe.
    assert V.verdict(29, 40, 515, 10).label == V.WORKS
    assert V.verdict(29, 40, 515, 10, detector_sensitive=False).label == V.WORKS

    # The floor still refuses first — a short sample is not rescued by a live detector.
    assert V.verdict(1, 0, 5, 0, detector_sensitive=True).label == V.REFUSED



def test_the_window_day_is_the_owners_calendar_day():
    """§2 registered the window in the owner's dates; the events are stamped UTC.

    The two disagree for nine hours a day. Measured 2026-09-08: five uptake events fall on
    different days under the two readings, four of them at the opening boundary where the
    disagreement decides whether they are in the sample at all.
    """
    # 20:00 UTC on 08-31 is already 09-01 in Seoul.
    assert V.window_day("2026-08-31T20:00:00+00:00") == "2026-09-01"
    assert V.window_day("2026-08-31T05:00:00+00:00") == "2026-08-31"
    # 15:00 UTC is the exact cutover.
    assert V.window_day("2026-09-07T15:00:00+00:00") == "2026-09-08"
    assert V.window_day("2026-09-07T14:59:59+00:00") == "2026-09-07"
    # `Z` is the spelling the engine emits.
    assert V.window_day("2026-08-31T20:00:00Z") == "2026-09-01"

    # An unparseable stamp keeps its prefix rather than vanishing: a row dropped for an odd clock
    # is worse than one filed a day off, because nothing counts what was dropped.
    assert V.window_day("not-a-date") == "not-a-date"
    assert V.window_day("") == ""
    assert V.window_day(None) == ""


def test_collect_closes_the_window_at_both_ends():
    """A floor with no ceiling stops being the registered window the day after it closes.

    `peek` filtered to the window and this did not, so from 09-15 the two surfaces would read the
    same ledger and count different samples — the dates would still be printed, and would no
    longer be what either number described.
    """
    def row(day, agent="claude-code"):
        return {
            "event": "injection_uptake",
            "observed_at": f"{day}T03:00:00+00:00",
            "agent": agent,
            "used_prompts": 1,
            "total_prompts": 10,
            "used_control_prompts": 0,
        }

    rows = [row("2026-09-10"), row("2026-09-20")]
    inside, _ = V.collect(rows, since="2026-08-31", until="2026-09-14")
    assert inside["claude-code"]["total_prompts"] == 10, inside

    # Which side is kept, not merely how many. A ceiling written as a floor drops the same count
    # and keeps the wrong row, so asserting on the total alone passes for the inverted comparison.
    kept = V.collect([row("2026-09-10")], since="2026-08-31", until="2026-09-14")[0]
    assert kept["claude-code"]["total_prompts"] == 10, kept
    dropped = V.collect([row("2026-09-20")], since="2026-08-31", until="2026-09-14")[0]
    assert not dropped, dropped

    # Without the ceiling the later row joins the sample — this is the old behaviour, kept
    # reachable so callers with no window of their own are not forced into one.
    unbounded, _ = V.collect(rows, since="2026-08-31")
    assert unbounded["claude-code"]["total_prompts"] == 20, unbounded



def test_coverage_decides_whether_a_verdict_is_allowed_at_all():
    """§2 puts coverage before the thresholds, not beside them.

    A rate computed over a seventh of the channel is not a smaller version of the right answer; it
    is an answer about a different population. Live on 2026-09-08 the coverage was 29/208 = 14%
    and both surfaces printed 비작동 — the largest type on the page asserting the thing the
    contract says cannot be asserted.
    """
    short = V.verdict(29, 0, 515, 0, detector_sensitive=True, coverage_ok=False)
    assert short.label == V.REFUSED, short
    assert "커버리지" in short.reason, short.reason

    # Cleared coverage restores the ordinary reading.
    assert V.verdict(29, 0, 515, 0, detector_sensitive=True, coverage_ok=True).label == V.BROKEN

    # It gates 작동 too. A working channel measured over a seventh of itself is not a working
    # channel that was measured — the clause is about the population, not about the direction.
    assert V.verdict(29, 40, 515, 10, coverage_ok=False).label == V.REFUSED
    assert V.verdict(29, 40, 515, 10, coverage_ok=True).label == V.WORKS



if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - verdict_core: floors refuse, thresholds match the pre-registration")
