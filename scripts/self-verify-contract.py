#!/usr/bin/env python3
"""Evaluate the self-verification loop summary against stage thresholds."""

import argparse
import csv
from pathlib import Path
import sys


REQUIRED_EVERY_CYCLE = ("codex-status-strict", "readiness", "quality", "recent-events")
GUARD_STEP = "guard"

STAGES = {
    "bootstrap": {"min_cycles": 1, "min_guard_runs": 1, "next": "soak-2h"},
    "soak-2h": {"min_cycles": 6, "min_guard_runs": 2, "next": "day"},
    "day": {"min_cycles": 72, "min_guard_runs": 13, "next": "release-candidate"},
}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check self-verification stage contract")
    parser.add_argument("--summary", help="summary.tsv path; defaults to newest under /private/tmp/omb-self-verify")
    parser.add_argument("--stage", choices=sorted(STAGES), default="bootstrap")
    args = parser.parse_args(argv)

    summary = Path(args.summary) if args.summary else newest_summary(Path("/private/tmp/omb-self-verify"))
    if summary is None:
        print("self_verify_contract status=failed reason=no_summary_found")
        return 1

    rows = read_rows(summary)
    result = evaluate(rows, args.stage)
    print(
        "self_verify_contract "
        f"stage={args.stage} status={result['status']} "
        f"summary={summary} cycles={result['cycles']} guard_runs={result['guard_runs']} "
        f"failed_rows={len(result['failed_rows'])} next={result['next']}"
    )
    for issue in result["issues"]:
        print(f"  issue={issue}")
    return 0 if result["status"] == "pass" else 1


def newest_summary(root):
    candidates = [p for p in root.glob("*/summary.tsv") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def read_rows(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def evaluate(rows, stage):
    thresholds = STAGES[stage]
    issues = []
    cycles = sorted({int(row["cycle"]) for row in rows if row.get("cycle", "").isdigit()})
    cycle_count = len(cycles)
    guard_cycles = sorted(
        {int(row["cycle"]) for row in rows if row.get("step") == GUARD_STEP and row.get("cycle", "").isdigit()}
    )
    guard_runs = len(guard_cycles)
    failed_rows = [row for row in rows if row.get("status") != "ok" or row.get("exit_code") != "0"]
    by_cycle = {}
    for row in rows:
        if row.get("cycle", "").isdigit():
            by_cycle.setdefault(int(row["cycle"]), set()).add(row.get("step", ""))

    expected_cycles = set(range(1, thresholds["min_cycles"] + 1))
    missing_cycles = sorted(expected_cycles - set(cycles))
    expected_guard_cycles = _expected_guard_cycles(thresholds["min_cycles"])
    missing_guard_cycles = sorted(expected_guard_cycles - set(guard_cycles))

    if cycle_count < thresholds["min_cycles"]:
        issues.append(f"cycles {cycle_count} < required {thresholds['min_cycles']}")
    if missing_cycles:
        issues.append(f"missing_cycles {','.join(str(cycle) for cycle in missing_cycles)}")
    if guard_runs < thresholds["min_guard_runs"]:
        issues.append(f"guard_runs {guard_runs} < required {thresholds['min_guard_runs']}")
    if missing_guard_cycles:
        issues.append(f"missing_guard_cycles {','.join(str(cycle) for cycle in missing_guard_cycles)}")
    if failed_rows:
        issues.append(f"failed_rows {len(failed_rows)} > 0")

    for cycle in range(1, thresholds["min_cycles"] + 1):
        missing = [step for step in REQUIRED_EVERY_CYCLE if step not in by_cycle.get(cycle, set())]
        if missing:
            issues.append(f"cycle {cycle} missing {','.join(missing)}")

    status = "pass" if not issues else "failed"
    return {
        "status": status,
        "cycles": cycle_count,
        "guard_runs": guard_runs,
        "failed_rows": failed_rows,
        "issues": issues,
        "next": thresholds["next"] if status == "pass" else stage,
    }


def _expected_guard_cycles(min_cycles):
    return {1, *range(6, min_cycles + 1, 6)}


if __name__ == "__main__":
    sys.exit(main())
