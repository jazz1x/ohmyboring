#!/usr/bin/env python3
"""C-PORT8: the same card run twice at once → exactly one is turned away in the door's 409
shape, the other completes. A fake card stands in — no real card, no Slack."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import card_runner  # noqa: E402

FAKE_TOKEN = "fake-token-value-not-real"


def test_second_concurrent_run_is_rejected_like_the_door_409() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "fake_card.py"
        fake.write_text(
            "import time\nprint('[card] posted ts=123.456', flush=True)\ntime.sleep(1.0)\n",
            encoding="utf-8",
        )
        programs = {"fake-card": fake}
        env_file = Path(tmp) / ".env"
        env_file.write_text(f"OTHER=1\nSLACK_BOT_TOKEN={FAKE_TOKEN}\n", encoding="utf-8")
        answers: list[card_runner.Answer] = []

        def go() -> None:
            answers.append(
                card_runner.execute(
                    "fake-card",
                    programs=programs,
                    lock_dir=Path(tmp) / "locks",
                    timeout_s=30.0,
                    env_file=env_file,
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
    assert FAKE_TOKEN not in json.dumps(done[0].body)


def test_missing_token_fails_before_the_card_starts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "started"
        fake = Path(tmp) / "fake_card.py"
        fake.write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
        env_file = Path(tmp) / ".env"
        env_file.write_text("OTHER=1\n", encoding="utf-8")
        answer = card_runner.execute(
            "fake-card", programs={"fake-card": fake}, lock_dir=Path(tmp) / "locks", env_file=env_file
        )
        started = marker.exists()
    code, stream, line = card_runner.fold("[x]", answer)
    assert not started
    assert (code, stream) == (1, "stderr")
    assert line == "[x] FAILED: fake-card: SLACK_BOT_TOKEN 없음 tail="


def test_card_root_without_slack_dir_fails_before_the_card_starts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "started"
        fake = Path(tmp) / "fake_card.py"
        fake.write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
        env_file = Path(tmp) / ".env"
        env_file.write_text(f"SLACK_BOT_TOKEN={FAKE_TOKEN}\n", encoding="utf-8")
        root = Path(tmp) / "no-card-root"
        answer = card_runner.execute(
            "fake-card",
            programs={"fake-card": fake},
            lock_dir=Path(tmp) / "locks",
            env_file=env_file,
            card_root=root,
        )
        started = marker.exists()
    code, stream, line = card_runner.fold("[x]", answer)
    assert not started
    assert (code, stream) == (1, "stderr")
    assert line == f"[x] FAILED: fake-card: 카드 뿌리 없음: {root} tail="


if __name__ == "__main__":
    test_card_root_without_slack_dir_fails_before_the_card_starts()
    test_missing_token_fails_before_the_card_starts()
    test_second_concurrent_run_is_rejected_like_the_door_409()
    print("ok")
