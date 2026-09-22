#!/usr/bin/env python3
"""Network-free regression tests for session collector status semantics."""

import importlib.util
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SHARED_DIR = HERE.parent / "shared"
sys.path.insert(0, str(SHARED_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


claude_collect = _load("claude_collect_sessions", "collect-sessions.py")
kimi_collect = _load("kimi_collect_sessions", "collect-kimi-sessions.py")
codex_collect = _load("codex_collect_sessions", "../codex/collect-sessions.py")


def _last_event(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])


def test_claude_backfill_does_not_claim_to_be_a_session_end():
    old_mark_dir = claude_collect.markers.MARK_DIR
    old_min_kb = claude_collect.MIN_KB
    old_limit = claude_collect.LIMIT
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "claude" / "project"
            source.mkdir(parents=True)
            (source / "s1.jsonl").write_text(json.dumps({"cwd": "/work/repo"}) + "\nbody\n", encoding="utf-8")

            claude_collect.markers.set_mark_dir(str(root / "markers"))
            claude_collect.MIN_KB = 0
            claude_collect.LIMIT = 1

            with (
                mock.patch.object(claude_collect.sys, "argv", ["collect-sessions.py"]),
                mock.patch.object(
                    claude_collect.boring_config, "source_dirs", return_value=[str(root / "claude")]
                ),
                mock.patch.object(claude_collect, "_warm_llm"),
                mock.patch.object(
                    claude_collect.subprocess, "run", return_value=mock.Mock(returncode=0)
                ) as run,
                mock.patch.object(claude_collect, "DrudgeClient"),
                mock.patch.dict(
                    os.environ,
                    {"BORING_EVENT_LOG": str(root / "events.ndjson"), "BORING_EVENT_SINK": "spool"},
                ),
            ):
                claude_collect.main()

            payloads = [json.loads(c.kwargs["input"]) for c in run.call_args_list if "input" in c.kwargs]
            assert payloads, "the hook was never invoked"
            assert all(p["hook_event_name"] != "SessionEnd" for p in payloads), payloads
    finally:
        claude_collect.markers.set_mark_dir(old_mark_dir)
        claude_collect.MIN_KB = old_min_kb
        claude_collect.LIMIT = old_limit


def test_kimi_collector_fails_when_distill_fails():
    old_home = kimi_collect.KIMI_HOME
    old_hook = kimi_collect.HOOK
    old_limit = kimi_collect.LIMIT
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session_dir = root / "session"
            session_dir.mkdir()
            second_session_dir = root / "session-2"
            second_session_dir.mkdir()
            index = root / "session_index.jsonl"
            index.write_text(
                json.dumps({"sessionId": "k1", "sessionDir": str(session_dir), "workDir": "/work/repo"})
                + "\n"
                + json.dumps(
                    {"sessionId": "k2", "sessionDir": str(second_session_dir), "workDir": "/work/repo"}
                )
                + "\n",
                encoding="utf-8",
            )
            hook = root / "distill-session.py"
            hook.write_text("# stub\n", encoding="utf-8")
            event_path = root / "events.ndjson"

            kimi_collect.KIMI_HOME = str(root)
            kimi_collect.HOOK = str(hook)
            kimi_collect.LIMIT = 1

            with (
                mock.patch.object(kimi_collect, "_distill", return_value=False) as distill,
                # Stub the client so the write-door preflight does not make this test depend on
                # a reachable engine — it owns the distill-failure path, not readiness.
                mock.patch.object(kimi_collect, "DrudgeClient"),
                mock.patch.dict(
                    os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}
                ),
            ):
                rc = kimi_collect.main()

            assert rc == 1
            assert distill.call_count == 1
            event = _last_event(event_path)
            assert event["component"] == "kimi-collector"
            assert event["status"] == "failed"
            assert event["attempted"] == 1
            assert event["failed"] == 1
            assert event["workflow"] == "memory_ingest"
            assert event["workflow_node"] == "retry_marked"
            assert event["workflow_outcome"] == "fail"
    finally:
        kimi_collect.KIMI_HOME = old_home
        kimi_collect.HOOK = old_hook
        kimi_collect.LIMIT = old_limit


def test_claude_marked_excludes_dead_lettered_session():
    old_mark_dir = claude_collect.markers.MARK_DIR
    try:
        with tempfile.TemporaryDirectory() as d:
            claude_collect.markers.set_mark_dir(d)
            with mock.patch.dict(os.environ, {"MARKER_RETRY_MAX_ATTEMPTS": "1"}):
                claude_collect.markers.mark_retry("s1", reason="resolution gate failed")

            assert claude_collect.markers.is_dead("s1")
            assert claude_collect._marked("s1") is True
    finally:
        claude_collect.markers.set_mark_dir(old_mark_dir)


def test_kimi_marked_excludes_dead_lettered_session():
    old_mark_dir = kimi_collect.markers.MARK_DIR
    try:
        with tempfile.TemporaryDirectory() as d:
            kimi_collect.markers.set_mark_dir(d)
            with mock.patch.dict(os.environ, {"MARKER_RETRY_MAX_ATTEMPTS": "1"}):
                kimi_collect.markers.mark_retry("k1", reason="resolution gate failed")

            assert kimi_collect.markers.is_dead("k1")
            assert kimi_collect._marked("k1") is True
    finally:
        kimi_collect.markers.set_mark_dir(old_mark_dir)


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b'{"ok": true}'


def _recording_urlopen(calls: list):
    def fake_urlopen(req, timeout):
        calls.append((req.full_url, timeout))
        return _FakeResponse()

    return fake_urlopen


def _claude_fixture(root: Path) -> Path:
    source = root / "claude" / "project"
    source.mkdir(parents=True)
    (source / "s1.jsonl").write_text(json.dumps({"cwd": "/work/repo"}) + "\nbody\n", encoding="utf-8")
    return source


def _codex_fixture(root: Path) -> Path:
    source = root / "sessions"
    source.mkdir(parents=True)
    (source / "todo.jsonl").write_text(json.dumps({"payload": {}}) + "\nbody\n", encoding="utf-8")
    return source


def _kimi_fixture(root: Path) -> None:
    session_dir = root / "session"
    session_dir.mkdir()
    (root / "session_index.jsonl").write_text(
        json.dumps({"sessionId": "k1", "sessionDir": str(session_dir), "workDir": "/work/repo"}) + "\n",
        encoding="utf-8",
    )
    hook = root / "distill-session.py"
    hook.write_text("# stub\n", encoding="utf-8")


def test_claude_collector_run_makes_no_sync_request():
    """A run that distils one session must never POST /sync: remember ingests each note
    live, and the engine's own 4h scheduler re-scans the vault."""
    old_mark_dir = claude_collect.markers.MARK_DIR
    old_min_kb = claude_collect.MIN_KB
    old_limit = claude_collect.LIMIT
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _claude_fixture(root)
            event_path = root / "events.ndjson"
            calls = []

            claude_collect.markers.set_mark_dir(str(root / "markers"))
            claude_collect.MIN_KB = 0
            claude_collect.LIMIT = 1

            with (
                mock.patch.object(claude_collect.sys, "argv", ["collect-sessions.py"]),
                mock.patch.object(
                    claude_collect.boring_config, "source_dirs", return_value=[str(root / "claude")]
                ),
                mock.patch.object(claude_collect, "_warm_llm"),
                mock.patch.object(claude_collect.subprocess, "run", return_value=mock.Mock(returncode=0)),
                mock.patch.object(urllib.request, "urlopen", _recording_urlopen(calls)),
                mock.patch.dict(
                    os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}
                ),
            ):
                rc = claude_collect.main()

            assert rc == 0
            assert not [url for (url, _t) in calls if url.endswith("/sync")], calls
            event = _last_event(event_path)
            assert event["status"] == "ok"
            assert event["processed"] == 1
            assert event["failed"] == 0
            assert "sync_status" not in event
    finally:
        claude_collect.markers.set_mark_dir(old_mark_dir)
        claude_collect.MIN_KB = old_min_kb
        claude_collect.LIMIT = old_limit


def test_claude_collector_fails_when_distill_fails():
    old_mark_dir = claude_collect.markers.MARK_DIR
    old_min_kb = claude_collect.MIN_KB
    old_limit = claude_collect.LIMIT
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _claude_fixture(root)
            event_path = root / "events.ndjson"
            calls = []

            claude_collect.markers.set_mark_dir(str(root / "markers"))
            claude_collect.MIN_KB = 0
            claude_collect.LIMIT = 1

            with (
                mock.patch.object(claude_collect.sys, "argv", ["collect-sessions.py"]),
                mock.patch.object(
                    claude_collect.boring_config, "source_dirs", return_value=[str(root / "claude")]
                ),
                mock.patch.object(claude_collect, "_warm_llm"),
                mock.patch.object(claude_collect.subprocess, "run", return_value=mock.Mock(returncode=1)),
                mock.patch.object(urllib.request, "urlopen", _recording_urlopen(calls)),
                mock.patch.dict(
                    os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}
                ),
            ):
                rc = claude_collect.main()

            assert rc == 1
            assert not [url for (url, _t) in calls if url.endswith("/sync")], calls
            event = _last_event(event_path)
            assert event["status"] == "failed"
            assert event["failed"] == 1
            assert "sync_status" not in event
    finally:
        claude_collect.markers.set_mark_dir(old_mark_dir)
        claude_collect.MIN_KB = old_min_kb
        claude_collect.LIMIT = old_limit


def test_kimi_collector_run_makes_no_sync_request():
    old_home = kimi_collect.KIMI_HOME
    old_hook = kimi_collect.HOOK
    old_limit = kimi_collect.LIMIT
    old_mark_dir = kimi_collect.markers.MARK_DIR
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _kimi_fixture(root)
            event_path = root / "events.ndjson"
            calls = []

            kimi_collect.KIMI_HOME = str(root)
            kimi_collect.HOOK = str(root / "distill-session.py")
            kimi_collect.LIMIT = 1
            kimi_collect.markers.set_mark_dir(str(root / "markers"))

            with (
                mock.patch.object(kimi_collect.subprocess, "run", return_value=mock.Mock(returncode=0)),
                mock.patch.object(urllib.request, "urlopen", _recording_urlopen(calls)),
                mock.patch.dict(
                    os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}
                ),
            ):
                rc = kimi_collect.main()

            assert rc == 0
            assert not [url for (url, _t) in calls if url.endswith("/sync")], calls
            event = _last_event(event_path)
            assert event["status"] == "ok"
            assert event["processed"] == 1
            assert event["failed"] == 0
            assert "sync_status" not in event
    finally:
        kimi_collect.KIMI_HOME = old_home
        kimi_collect.HOOK = old_hook
        kimi_collect.LIMIT = old_limit
        kimi_collect.markers.set_mark_dir(old_mark_dir)


def test_codex_collector_run_makes_no_sync_request():
    old_mark_dir = codex_collect.markers.MARK_DIR
    old_min_kb = codex_collect.MIN_KB
    old_stable_age = codex_collect.STABLE_AGE_S
    old_limit = codex_collect.LIMIT
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _codex_fixture(root)
            event_path = root / "events.ndjson"
            calls = []

            codex_collect.markers.set_mark_dir(str(root / "markers"))
            codex_collect.MIN_KB = 0
            codex_collect.STABLE_AGE_S = 0
            codex_collect.LIMIT = 1

            with (
                mock.patch.object(codex_collect, "_source_dir", return_value=str(root / "sessions")),
                mock.patch.object(codex_collect.subprocess, "run", return_value=mock.Mock(returncode=0)),
                mock.patch.object(urllib.request, "urlopen", _recording_urlopen(calls)),
                mock.patch.dict(
                    os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}
                ),
            ):
                rc = codex_collect.main([])

            assert rc == 0
            assert not [url for (url, _t) in calls if url.endswith("/sync")], calls
            event = _last_event(event_path)
            assert event["status"] == "ok"
            assert event["processed"] == 1
            assert event["failed"] == 0
            assert "sync_status" not in event
    finally:
        codex_collect.markers.set_mark_dir(old_mark_dir)
        codex_collect.MIN_KB = old_min_kb
        codex_collect.STABLE_AGE_S = old_stable_age
        codex_collect.LIMIT = old_limit


def test_codex_collector_fails_when_distill_fails():
    old_mark_dir = codex_collect.markers.MARK_DIR
    old_min_kb = codex_collect.MIN_KB
    old_stable_age = codex_collect.STABLE_AGE_S
    old_limit = codex_collect.LIMIT
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _codex_fixture(root)
            event_path = root / "events.ndjson"
            calls = []

            codex_collect.markers.set_mark_dir(str(root / "markers"))
            codex_collect.MIN_KB = 0
            codex_collect.STABLE_AGE_S = 0
            codex_collect.LIMIT = 1

            with (
                mock.patch.object(codex_collect, "_source_dir", return_value=str(root / "sessions")),
                mock.patch.object(codex_collect.subprocess, "run", return_value=mock.Mock(returncode=1)),
                mock.patch.object(urllib.request, "urlopen", _recording_urlopen(calls)),
                mock.patch.dict(
                    os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}
                ),
            ):
                rc = codex_collect.main([])

            assert rc == 1
            assert not [url for (url, _t) in calls if url.endswith("/sync")], calls
            event = _last_event(event_path)
            assert event["status"] == "failed"
            assert event["failed"] == 1
            assert "sync_status" not in event
    finally:
        codex_collect.markers.set_mark_dir(old_mark_dir)
        codex_collect.MIN_KB = old_min_kb
        codex_collect.STABLE_AGE_S = old_stable_age
        codex_collect.LIMIT = old_limit


if __name__ == "__main__":
    test_claude_backfill_does_not_claim_to_be_a_session_end()
    test_kimi_collector_fails_when_distill_fails()
    test_claude_marked_excludes_dead_lettered_session()
    test_kimi_marked_excludes_dead_lettered_session()
    test_claude_collector_run_makes_no_sync_request()
    test_claude_collector_fails_when_distill_fails()
    test_kimi_collector_run_makes_no_sync_request()
    test_codex_collector_run_makes_no_sync_request()
    test_codex_collector_fails_when_distill_fails()
    print("ok - scheduler collectors")
