#!/usr/bin/env python3
"""Tests for the two hermes card-trigger scripts and their shared helper.

A stub http.server stands in for the door. The contract under test: the door's ok → exit 0
with one stdout line; ok:false → exit 1 with one stderr line carrying the exit code and the
tail; an HTTP error, an unreachable door, or a non-JSON answer → exit 1 with one line. The
two entry scripts must POST the right route under their own label.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent  # agents/hermes
SHARED = ROOT.parent / "shared"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SHARED))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StubDoor:
    """One canned answer per pushed request, FIFO; records every path it was asked."""

    def __init__(self):
        self.answers: list[tuple[int, bytes]] = []
        self.paths: list[str] = []

    def push(self, status: int, body) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.answers.append((status, body))

    def next_answer(self) -> tuple[int, bytes]:
        return self.answers.pop(0) if self.answers else (200, b"{}")

    def __enter__(self):
        Handler.door = self
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class Handler(BaseHTTPRequestHandler):
    door: StubDoor

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        type(self).door.paths.append(self.path)
        status, body = type(self).door.next_answer()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


card_door = load("card_door", ROOT / "card_door.py")


def run_trigger(route: str, label: str, door_url: str):
    saved = dict(os.environ)
    os.environ["BORING_DOOR_URL"] = door_url
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = card_door.trigger(route, label)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return rc, out.getvalue(), err.getvalue()


def test_ok_answers_exit_0_with_one_stdout_line():
    with StubDoor() as door:
        door.push(200, json.dumps({"ok": True, "exit": 0, "posted_ts": "1727480000.0001", "tail": ""}))
        rc, out, err = run_trigger("/run/morning-card", "[run-morning-card]", f"http://127.0.0.1:{door.port}")
    assert rc == 0, err
    assert err == ""
    line = out.strip()
    assert line.count("\n") == 0, f"one line, said so:\n{out}"
    assert "[run-morning-card] ok exit=0 posted_ts=1727480000.0001" in line
    assert door.paths == ["/run/morning-card"]


def test_ok_false_answers_exit_1_with_the_code_and_tail():
    """The mutation this kills: the script exiting 0 on ok:false — hermes would read a failed
    card as a healthy run and its failure alert would stay silent."""
    with StubDoor() as door:
        door.push(
            200,
            json.dumps(
                {
                    "ok": False,
                    "exit": 3,
                    "tail": "[card] 카드 거부: 문이 응답하지 않음",
                }
            ),
        )
        rc, out, err = run_trigger("/run/morning-card", "[run-morning-card]", f"http://127.0.0.1:{door.port}")
    assert rc == 1
    assert out == ""
    line = err.strip()
    assert line.count("\n") == 0, f"one line, said so:\n{err}"
    assert "[run-morning-card] FAILED: exit=3" in line
    assert "카드 거부" in line


def test_an_http_error_is_exit_1_with_one_line():
    with StubDoor() as door:
        door.push(500, b'{"error":"door exploded"}')
        rc, out, err = run_trigger("/run/weekly-card", "[run-weekly-card]", f"http://127.0.0.1:{door.port}")
    assert rc == 1
    assert out == ""
    line = err.strip()
    assert line.count("\n") == 0
    assert "HTTP 500" in line


def test_a_dead_door_is_exit_1_with_one_line():
    dead_port = _free_port()  # nothing listens there
    rc, out, err = run_trigger("/run/morning-card", "[run-morning-card]", f"http://127.0.0.1:{dead_port}")
    assert rc == 1
    assert out == ""
    line = err.strip()
    assert line.count("\n") == 0
    assert "door unreachable" in line


def test_a_non_json_body_is_exit_1():
    with StubDoor() as door:
        door.push(200, b"<html>gateway</html>")
        rc, _out, err = run_trigger("/run/weekly-card", "[run-weekly-card]", f"http://127.0.0.1:{door.port}")
    assert rc == 1
    assert err.strip().count("\n") == 0


def test_the_entry_scripts_post_their_own_route_and_label():
    morning = load("run_morning_card_under_test", ROOT / "run-morning-card.py")
    weekly = load("run_weekly_card_under_test", ROOT / "run-weekly-card.py")
    seen: list[tuple[str, str]] = []

    def fake_trigger(route: str, label: str) -> int:
        seen.append((route, label))
        return 0

    real_trigger = card_door.trigger
    card_door.trigger = fake_trigger
    try:
        assert morning.main() == 0
        assert weekly.main() == 0
    finally:
        card_door.trigger = real_trigger
    assert seen == [
        ("/run/morning-card", "[run-morning-card]"),
        ("/run/weekly-card", "[run-weekly-card]"),
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
