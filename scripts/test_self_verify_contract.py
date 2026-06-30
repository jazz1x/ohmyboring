#!/usr/bin/env python3
"""Tests for scripts/self-verify-contract.py."""

import os
import importlib.util
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "self_verify_contract", str(ROOT / "scripts" / "self-verify-contract.py")
)
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)


def test_bootstrap_passes_with_one_green_cycle_and_guard():
    rows = _rows(cycles=1, guard_cycles={1})
    result = contract.evaluate(rows, "bootstrap")
    assert result["status"] == "pass"
    assert result["next"] == "soak-2h"


def test_failed_row_blocks_stage_transition():
    rows = _rows(cycles=1, guard_cycles={1})
    rows[1]["status"] = "failed"
    rows[1]["exit_code"] = "2"
    result = contract.evaluate(rows, "bootstrap")
    assert result["status"] == "failed"
    assert result["next"] == "bootstrap"
    assert result["failed_rows"]


def test_soak_requires_six_cycles_and_two_guard_runs():
    rows = _rows(cycles=6, guard_cycles={1})
    result = contract.evaluate(rows, "soak-2h")
    assert result["status"] == "failed"
    assert "guard_runs 1 < required 2" in result["issues"]
    assert "missing_guard_cycles 6" in result["issues"]

    rows = _rows(cycles=6, guard_cycles={1, 6})
    result = contract.evaluate(rows, "soak-2h")
    assert result["status"] == "pass"
    assert result["next"] == "day"


def test_soak_rejects_non_contiguous_cycles_and_wrong_guard_positions():
    rows = [row for row in _rows(cycles=7, guard_cycles={2, 7}) if row["cycle"] != "1"]
    result = contract.evaluate(rows, "soak-2h")
    assert result["status"] == "failed"
    assert "missing_cycles 1" in result["issues"]
    assert "missing_guard_cycles 1,6" in result["issues"]


def test_day_requires_seventy_two_cycles_and_thirteen_guard_runs():
    rows = _rows(cycles=72, guard_cycles={1, *range(6, 73, 6)})
    result = contract.evaluate(rows, "day")
    assert result["status"] == "pass"
    assert result["guard_runs"] == 13
    assert result["next"] == "release-candidate"


def test_newest_summary_picks_latest_summary_file():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = root / "old"
        new = root / "new"
        old.mkdir()
        new.mkdir()
        old_summary = old / "summary.tsv"
        new_summary = new / "summary.tsv"
        old_summary.write_text("cycle\tstep\tstatus\texit_code\n", encoding="utf-8")
        new_summary.write_text("cycle\tstep\tstatus\texit_code\n", encoding="utf-8")
        os.utime(old_summary, (1, 1))
        os.utime(new_summary, (2, 2))
        assert contract.newest_summary(root) == new_summary


def _rows(cycles, guard_cycles):
    rows = []
    for cycle in range(1, cycles + 1):
        for step in contract.REQUIRED_EVERY_CYCLE:
            rows.append(_row(cycle, step))
        if cycle in guard_cycles:
            rows.append(_row(cycle, contract.GUARD_STEP))
    return rows


def _row(cycle, step):
    return {
        "cycle": str(cycle),
        "step": step,
        "status": "ok",
        "exit_code": "0",
        "started_at": "2026-06-30T00:00:00+0900",
        "ended_at": "2026-06-30T00:00:01+0900",
        "duration_s": "1",
    }


if __name__ == "__main__":
    test_bootstrap_passes_with_one_green_cycle_and_guard()
    test_failed_row_blocks_stage_transition()
    test_soak_requires_six_cycles_and_two_guard_runs()
    test_soak_rejects_non_contiguous_cycles_and_wrong_guard_positions()
    test_day_requires_seventy_two_cycles_and_thirteen_guard_runs()
    test_newest_summary_picks_latest_summary_file()
    print("ok - self verify contract")
