#!/usr/bin/env python3
"""Test for the two hermes card-trigger scripts: each runs its own card under its own label
through card_runner.run_card."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent  # agents/hermes
SHARED = ROOT.parent / "shared"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SHARED))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_the_entry_scripts_run_their_own_card_and_label():
    morning = load("run_morning_card_under_test", ROOT / "run-morning-card.py")
    weekly = load("run_weekly_card_under_test", ROOT / "run-weekly-card.py")
    seen: list[tuple[str, str]] = []

    def fake_run_card(name: str, label: str) -> int:
        seen.append((name, label))
        return 0

    import card_runner

    real_run_card = card_runner.run_card
    card_runner.run_card = fake_run_card
    try:
        assert morning.main() == 0
        assert weekly.main() == 0
    finally:
        card_runner.run_card = real_run_card
    assert seen == [
        ("morning-card", "[run-morning-card]"),
        ("weekly-card", "[run-weekly-card]"),
    ]


if __name__ == "__main__":
    _module = sys.modules[__name__]
    _tests = [
        (name, obj)
        for name, obj in sorted(vars(_module).items())
        if name.startswith("test_") and callable(obj)
    ]
    for _name, _fn in _tests:
        _fn()
    print(f"ok - hermes card triggers ({len(_tests)} tests)")
