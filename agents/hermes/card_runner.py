#!/usr/bin/env python3
"""Run a card program directly from hermes — the door's /run/* wrapper, moved here (C-PORT8).

`run_card(name, label)` runs one card program: exit code 0 only when the card exited 0, one
stdout/stderr line. It holds the lock and the timeout and reads the {ok, exit, posted_ts?, tail}
answer shape the door's retired /run/* routes used to give.

Two kinds of function: `execute` runs the card and returns an Answer (effects); `fold` turns an
Answer into (exit code, stream, line) (decision). `run_card` is the only one that prints.

Each cron run is its own process, so the one-at-a-time lock is a non-blocking flock on a file
per card; it is dropped by the kernel when the process dies, so a crashed run cannot wedge it.
The card scripts sit under <card root>/agents/slack/: the card root is $BORING_CARD_ROOT when set,
else the repo this file sits in (parents[2]). A root without agents/slack (e.g. this file copied
into ~/.hermes/scripts) fails the run. The scripts are mounted read-only in the hermes container,
so the lock files live in the hermes data dir instead.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]


def default_card_root() -> Path:
    return Path(os.environ.get("BORING_CARD_ROOT") or _REPO_ROOT)


#: how long one card run may take before it is given up on (the door's DOOR_RUN_CARD_TIMEOUT).
RUN_CARD_TIMEOUT_S = float(os.environ.get("DOOR_RUN_CARD_TIMEOUT", "900"))

CARD_PROGRAMS = {
    "morning-card": Path("agents") / "slack" / "card.py",
    "weekly-card": Path("agents") / "slack" / "weekly_card.py",
    "repair-judge": Path("agents") / "slack" / "card_repair_judge.py",
    "advice-write": Path("agents") / "slack" / "card_advice_write.py",
}

#: the tool prints "[card] posted ts=<slack ts>"; the newest match on stdout is posted_ts.
_POSTED_TS_RE = re.compile(r"ts=([0-9]+\.[0-9]+)")


def default_lock_dir() -> Path:
    """Under the hermes data dir (HERMES_HOME, else ~/.hermes) — writable where the card mount is not."""
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "card-locks"


def default_env_file() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / ".env"


def read_bot_token(env_file: Path) -> str | None:
    """The SLACK_BOT_TOKEN value from env_file, or None when the file or the name is missing/empty."""
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        name, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and name.strip() == "SLACK_BOT_TOKEN":
            return value.strip().strip("\"'") or None
    return None


@dataclass(frozen=True)
class Answer:
    """What the door would have answered: its HTTP status and JSON body."""

    status: int
    body: dict[str, Any]


def one_line(text: str) -> str:
    return " ".join(text.split())


def _card_run_body(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    merged = "\n".join(part for part in (completed.stderr.strip(), completed.stdout.strip()) if part)
    tail = "\n".join(merged.splitlines()[-5:])[-1000:]
    body: dict[str, Any] = {"ok": completed.returncode == 0, "exit": completed.returncode, "tail": tail}
    matches = _POSTED_TS_RE.findall(completed.stdout)
    if matches:
        body["posted_ts"] = matches[-1]
    return body


def _failed(error: str) -> Answer:
    return Answer(200, {"ok": False, "exit": None, "tail": "", "error": error})


def _run_locked(script: Path, root: Path, timeout_s: float, token: str) -> Answer:
    try:
        completed = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(root),
            env={**os.environ, "SLACK_BOT_TOKEN": token},
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return _failed(f"timeout after {timeout_s:.0f}s")
    except OSError as e:
        return _failed(f"spawn failed: {e}")
    body = _card_run_body(completed)
    return Answer(200, {**body, "tail": body["tail"].replace(token, "***")})


def execute(
    name: str,
    *,
    programs: dict[str, Path] = CARD_PROGRAMS,
    lock_dir: Path | None = None,
    timeout_s: float = RUN_CARD_TIMEOUT_S,
    env_file: Path | None = None,
    card_root: Path | None = None,
) -> Answer:
    """Run one card under its per-card lock. A second concurrent caller gets the door's 409."""
    program = programs.get(name)
    if program is None:
        return Answer(404, {"error": f"unknown card {name}"})
    root = card_root if card_root is not None else default_card_root()
    if not (root / "agents" / "slack").is_dir():
        return _failed(f"{name}: 카드 뿌리 없음: {root}")
    script = root / program
    # Read from $HERMES_HOME/.env, not terminal.env_passthrough: that is a hermes-global setting that
    # also reaches the agent's tool subprocesses (shell/code execution), leaking the bot token to every agent-run command.
    token = read_bot_token(env_file if env_file is not None else default_env_file())
    if token is None:
        return _failed(f"{name}: SLACK_BOT_TOKEN 없음")
    directory = lock_dir if lock_dir is not None else default_lock_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle = open(directory / f"{name}.lock", "a")
    except OSError as e:
        return _failed(f"lock unavailable: {e}")
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return Answer(409, {"error": f"{name} already running"})
        except OSError as e:
            return _failed(f"lock unavailable: {e}")
        return _run_locked(script, root, timeout_s, token)


def fold(label: str, answer: Answer) -> tuple[int, str, str]:
    """(exit code, 'stdout'|'stderr', line) — the line printed for this answer."""
    body = answer.body
    if answer.status != 200:
        detail = one_line(json.dumps(body, separators=(",", ":"), ensure_ascii=False))[:200]
        return 1, "stderr", f"{label} FAILED: door answered HTTP {answer.status} {detail}".rstrip()
    if body.get("ok"):
        suffix = f" posted_ts={body['posted_ts']}" if body.get("posted_ts") else ""
        return 0, "stdout", f"{label} ok exit={body.get('exit')}{suffix}"
    exit_code = body.get("exit")
    where = f"exit={exit_code}" if exit_code is not None else str(body.get("error") or "no exit code")
    tail = one_line(str(body.get("tail") or ""))[:200]
    return 1, "stderr", f"{label} FAILED: {where} tail={tail}"


def run_card(name: str, label: str) -> int:
    """Run the card, print one line, return the cron exit code."""
    code, stream, line = fold(label, execute(name))
    print(line, file=sys.stderr if stream == "stderr" else sys.stdout)
    return code
