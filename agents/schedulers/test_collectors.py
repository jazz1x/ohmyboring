#!/usr/bin/env python3
"""Network-free regression tests for session collector status semantics."""
import importlib.util
import json
import os
import sys
import tempfile
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


def _last_event(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])


def test_claude_collector_fails_when_sync_fails():
    old_mark_dir = claude_collect.markers.MARK_DIR
    old_min_kb = claude_collect.MIN_KB
    old_limit = claude_collect.LIMIT
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "claude" / "project"
            source.mkdir(parents=True)
            session = source / "s1.jsonl"
            session.write_text(json.dumps({"cwd": "/work/repo"}) + "\nbody\n", encoding="utf-8")
            event_path = root / "events.ndjson"

            claude_collect.markers.set_mark_dir(str(root / "markers"))
            claude_collect.MIN_KB = 0
            claude_collect.LIMIT = 1

            with (
                mock.patch.object(claude_collect.sys, "argv", ["collect-sessions.py"]),
                mock.patch.object(claude_collect.boring_config, "source_dirs", return_value=[str(root / "claude")]),
                mock.patch.object(claude_collect, "_warm_llm"),
                mock.patch.object(claude_collect.subprocess, "run", return_value=mock.Mock(returncode=0)),
                mock.patch.object(claude_collect, "DrudgeClient") as client,
                mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}),
            ):
                client.return_value.sync.side_effect = OSError("sync down")
                rc = claude_collect.main()

            assert rc == 1
            event = _last_event(event_path)
            assert event["component"] == "claude-collector"
            assert event["status"] == "failed"
            assert event["sync_status"] == "failed"
            assert event["workflow"] == "memory_ingest"
            assert event["workflow_node"] == "retry_marked"
            assert event["workflow_outcome"] == "fail"
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
                + json.dumps({"sessionId": "k2", "sessionDir": str(second_session_dir), "workDir": "/work/repo"})
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
                mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}),
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


if __name__ == "__main__":
    test_claude_collector_fails_when_sync_fails()
    test_kimi_collector_fails_when_distill_fails()
    print("ok - scheduler collectors")
