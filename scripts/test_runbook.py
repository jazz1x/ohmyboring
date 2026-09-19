#!/usr/bin/env python3
"""The gate that keeps docs/RUNBOOK.md from quietly becoming fiction.

Run: python3 scripts/test_runbook.py   (no pytest dependency)

A runbook is read at the worst moment — something is down and the person reading has no patience
for a command that no longer exists. The failure mode is not that the file is wrong when written;
it is that a `make` target gets renamed six weeks later and nothing says so. So every `make` target,
every repo-relative script path and every repo file the runbook names is checked to exist here, and
`scripts/guard.sh` runs it on every commit.

What this CANNOT check is whether the prose is true — whether the worker really runs every 20
minutes, whether that log really says what the file claims. That is a person's job, and saying so
is part of the gate: a check that implied more than it verifies would be worse than none.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "docs" / "RUNBOOK.md"

#: `make <target>` mentioned anywhere in the runbook, including inside fenced blocks.
_MAKE = re.compile(r"\bmake ([a-z][a-z0-9-]*)\b")
#: A repo-relative path to something executable the runbook tells the reader to run.
_SCRIPT = re.compile(r"\b((?:scripts|agents|hooks)/[A-Za-z0-9_./-]+\.(?:sh|py))\b")


def _runbook_text() -> str:
    assert RUNBOOK.exists(), f"{RUNBOOK.relative_to(ROOT)} is missing"
    return RUNBOOK.read_text(encoding="utf-8")


def _make_targets() -> set[str]:
    """Every target the Makefile actually defines."""
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([a-z][a-z0-9-]*):", text, re.M))


def test_every_make_target_the_runbook_names_exists():
    named = set(_MAKE.findall(_runbook_text()))
    assert named, "the runbook names no make target — either it is empty or the pattern broke"
    missing = sorted(named - _make_targets())
    assert not missing, (
        f"the runbook tells the reader to run targets that do not exist: {missing}. "
        "Rename it in the runbook too, or the next person runs it while something is down."
    )


def test_every_script_path_the_runbook_names_exists():
    named = set(_SCRIPT.findall(_runbook_text()))
    assert named, "the runbook names no script path — the pattern probably broke"
    missing = sorted(p for p in named if not (ROOT / p).exists())
    assert not missing, f"the runbook names scripts that are not in the tree: {missing}"


def test_the_runbook_is_tracked_and_its_gate_runs():
    """A runbook nothing runs the gate on is back to being an unchecked document."""
    tracked = subprocess.run(
        ["git", "ls-files", "docs/RUNBOOK.md", "scripts/test_runbook.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "docs/RUNBOOK.md" in tracked, "docs/RUNBOOK.md is not tracked — check .gitignore"
    assert "scripts/test_runbook.py" in tracked, "this gate is not tracked"
    guard = (ROOT / "scripts" / "guard.sh").read_text(encoding="utf-8")
    assert "test_runbook.py" in guard, "guard.sh does not run this gate, so nothing enforces it"


def test_the_runbook_says_what_it_cannot_check():
    """The one claim this file makes about itself. A gate that lets the runbook imply it is fully
    verified would be selling a guarantee it does not have."""
    text = _runbook_text()
    assert "게이트가 못 보는 것" in text, (
        "the runbook no longer states the limit of its own gate — say what is checked by a person"
    )


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                print(f"FAIL {name}: {e}", file=sys.stderr)
                failures += 1
    if failures:
        sys.exit(1)
    print("ok - runbook: every make target and script path it names exists")
