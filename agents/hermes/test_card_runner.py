#!/usr/bin/env python3
"""C-PORT8: the same card run twice at once → exactly one is turned away in the door's 409
shape, the other completes. A fake card stands in — no real card, no Slack."""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import card_runner  # noqa: E402


def test_second_concurrent_run_is_rejected_like_the_door_409() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "fake_card.py"
        fake.write_text(
            "import time\nprint('[card] posted ts=123.456', flush=True)\ntime.sleep(1.0)\n",
            encoding="utf-8",
        )
        programs = {"fake-card": fake}
        answers: list[card_runner.Answer] = []

        def go() -> None:
            answers.append(
                card_runner.execute(
                    "fake-card", programs=programs, lock_dir=Path(tmp) / "locks", timeout_s=30.0
                )
            )

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    rejected = [a for a in answers if a.status == 409]
    done = [a for a in answers if a.status == 200]
    assert len(rejected) == 1 and len(done) == 1
    assert rejected[0].body == {"error": "fake-card already running"}
    assert done[0].body == {"ok": True, "exit": 0, "tail": "[card] posted ts=123.456", "posted_ts": "123.456"}
    assert card_runner.fold("[x]", rejected[0]) == (
        1,
        "stderr",
        '[x] FAILED: door answered HTTP 409 {"error":"fake-card already running"}',
    )
    unknown = card_runner.execute("nope", programs=programs)
    assert card_runner.fold("[x]", unknown) == (
        1,
        "stderr",
        '[x] FAILED: door answered HTTP 404 {"error":"unknown card nope"}',
    )
    assert card_runner.fold("[x]", done[0]) == (0, "stdout", "[x] ok exit=0 posted_ts=123.456")


if __name__ == "__main__":
    test_second_concurrent_run_is_rejected_like_the_door_409()
    print("ok")
