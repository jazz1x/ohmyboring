#!/usr/bin/env python3
"""Network-free regression tests for the Codex adapter."""
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SHARED_DIR = HERE.parent / "shared"
sys.path.insert(0, str(SHARED_DIR))

for _var in (
    "BORING_CONFIG",
    "BORING_HOME",
    "BORING_URL",
    "BORING_LLM_BASE_URL",
    "BORING_LLM_MODEL",
):
    os.environ.pop(_var, None)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


distill = _load("codex_distill_session", "distill-session.py")
collect = _load("codex_collect_sessions", "collect-sessions.py")


def _run_main(payload: dict, extracted: str):
    stderr = io.StringIO()
    with (
        mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))),
        mock.patch.object(distill.sys, "stderr", stderr),
        mock.patch.object(distill, "extract", return_value=extracted),
        mock.patch.object(distill, "git_remote_url", return_value=""),
        mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)),
        mock.patch.object(distill, "_mark") as mark,
    ):
        rc = distill.main()
    return rc, stderr.getvalue(), mark


def test_large_raw_parse_short_marks_retry():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("{}\n")
        path = f.name
    try:
        payload = {
            "transcript_path": path,
            "session_id": "abc",
            "hook_event_name": "SessionEnd",
            "raw_bytes": 100,
            "min_raw_bytes_for_retry": 10,
        }
        rc, err, mark = _run_main(payload, "too short")
        assert rc == 1
        assert "marked for retry" in err
        mark.assert_called_once_with("codex-abc", retry=True)
    finally:
        os.unlink(path)


def test_small_raw_parse_short_marks_done():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("{}\n")
        path = f.name
    try:
        payload = {
            "transcript_path": path,
            "session_id": "abc",
            "hook_event_name": "SessionEnd",
            "raw_bytes": 5,
            "min_raw_bytes_for_retry": 10,
        }
        rc, err, mark = _run_main(payload, "too short")
        assert rc == 0
        assert "transcript too short" in err
        mark.assert_called_once_with("codex-abc")
    finally:
        os.unlink(path)


def test_success_passes_session_id_to_shared_core():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("{}\n")
        path = f.name
    try:
        payload = {
            "transcript_path": path,
            "session_id": "abc",
            "hook_event_name": "SessionEnd",
            "cwd": "/work/oh-my-boring",
        }
        stderr = io.StringIO()
        extracted = "x" * 600
        with (
            mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(distill.sys, "stderr", stderr),
            mock.patch.object(distill, "extract", return_value=extracted),
            mock.patch.object(distill, "git_remote_url", return_value=""),
            mock.patch.object(distill, "repo_slug", return_value="oh-my-boring"),
            mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)),
            mock.patch.object(distill, "_mark") as mark,
            mock.patch.object(distill, "distill_and_remember", return_value=True) as remember,
        ):
            rc = distill.main()

        assert rc == 0
        remember.assert_called_once_with(
            extracted,
            "personal",
            "oh-my-boring",
            "codex-abc",
        )
        mark.assert_called_once_with("codex-abc")
    finally:
        os.unlink(path)


def _write_codex_session(path: Path, subagent: bool = False) -> None:
    payload = {"thread_source": "subagent"} if subagent else {}
    path.write_text(json.dumps({"payload": payload}) + "\nbody\n", encoding="utf-8")


def test_collect_scan_classifies_queue_marked_rollout_and_subagent():
    old_mark_dir = collect.markers.MARK_DIR
    old_min_kb = collect.MIN_KB
    old_include = collect.INCLUDE_SUBAGENTS
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "sessions"
            source.mkdir()
            mark_dir = root / "markers"
            collect.markers.set_mark_dir(str(mark_dir))
            collect.MIN_KB = 0
            collect.INCLUDE_SUBAGENTS = False

            _write_codex_session(source / "todo.jsonl")
            _write_codex_session(source / "done.jsonl")
            _write_codex_session(source / "rollout-2026-06-29T19-29-28-demo.jsonl")
            _write_codex_session(source / "subagent.jsonl", subagent=True)
            collect.markers.mark_done("codex-done")

            scan = collect._scan_sessions(str(source), cutoff=0)

            assert scan["total"] == 4
            assert scan["already_marked"] == 1
            assert scan["rollout"] == 1
            assert scan["subagent"] == 1
            assert [Path(p).name for p in scan["todo"]] == ["todo.jsonl"]
    finally:
        collect.markers.set_mark_dir(old_mark_dir)
        collect.MIN_KB = old_min_kb
        collect.INCLUDE_SUBAGENTS = old_include


def test_collect_scan_reoffers_stale_pending_but_skips_fresh_pending():
    old_mark_dir = collect.markers.MARK_DIR
    old_min_kb = collect.MIN_KB
    old_pending_ttl = collect.PENDING_TTL
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "sessions"
            source.mkdir()
            mark_dir = root / "markers"
            collect.markers.set_mark_dir(str(mark_dir))
            collect.MIN_KB = 0
            collect.PENDING_TTL = 60

            _write_codex_session(source / "fresh.jsonl")
            _write_codex_session(source / "stale.jsonl")
            collect.markers.mark_pending("codex-fresh")
            collect.markers.mark_pending("codex-stale")
            stale_path = mark_dir / "codex-stale.pending"
            old = time.time() - 3600
            os.utime(stale_path, (old, old))

            scan = collect._scan_sessions(str(source), cutoff=0)

            assert scan["already_marked"] == 1
            assert [Path(p).name for p in scan["todo"]] == ["stale.jsonl"]
    finally:
        collect.markers.set_mark_dir(old_mark_dir)
        collect.MIN_KB = old_min_kb
        collect.PENDING_TTL = old_pending_ttl


def test_status_mode_reports_queue_worker_and_note_without_mutation():
    old_mark_dir = collect.markers.MARK_DIR
    old_min_kb = collect.MIN_KB
    old_include = collect.INCLUDE_SUBAGENTS
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "sessions"
            source.mkdir()
            mark_dir = root / "markers"
            vault = root / "vault"
            wiki = vault / "wiki"
            wiki.mkdir(parents=True)
            jobs = root / "jobs.json"

            collect.markers.set_mark_dir(str(mark_dir))
            collect.MIN_KB = 0
            collect.INCLUDE_SUBAGENTS = False
            _write_codex_session(source / "todo.jsonl")
            collect.markers.mark_done("codex-done")
            collect.markers.mark_done("codex-rollout-2026-06-29T19-29-28-demo")
            jobs.write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "name": "codex-memory-ingest-worker",
                                "enabled": True,
                                "state": "scheduled",
                                "last_status": "success",
                                "last_run_at": "2026-06-29T09:00:00+09:00",
                                "next_run_at": "2026-06-29T09:20:00+09:00",
                                "script": "/host/oh-my-boring/agents/codex/collect-sessions.py",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (wiki / "wiki-0001.md").write_text(
                "---\ntitle: codex note\nomb_session_id: codex-note\n---\nbody\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with (
                mock.patch.object(collect, "_source_dir", return_value=str(source)),
                mock.patch.object(collect, "_hermes_jobs_path", return_value=str(jobs)),
                mock.patch.object(
                    collect,
                    "_host_worker_status",
                    return_value={
                        "kind": "launchd",
                        "found": True,
                        "loaded": True,
                        "path": "/tmp/com.ohmyboring.codex-ingest.plist",
                    },
                ),
                mock.patch.dict(os.environ, {"BORING_VAULT_DIR": str(vault)}),
                mock.patch.object(collect.sys, "stdout", stdout),
                mock.patch.object(collect.subprocess, "run") as run,
                mock.patch.object(collect, "DrudgeClient") as client,
            ):
                rc = collect.main(["--status"])

            assert rc == 0
            run.assert_not_called()
            client.assert_not_called()
            out = stdout.getvalue()
            assert "queue_pending=1" in out
            assert "skipped_rollout=0 skipped_marked=0 skipped_subagent=0" in out
            assert "markers done=1 pending=0 retry=0" in out
            assert "codex-rollout-" not in out
            assert "worker found=true enabled=true state=scheduled last_status=success" in out
            assert "host_worker found=true loaded=true kind=launchd" in out
            assert "newest_note session_id=codex-note" in out
    finally:
        collect.markers.set_mark_dir(old_mark_dir)
        collect.MIN_KB = old_min_kb
        collect.INCLUDE_SUBAGENTS = old_include


if __name__ == "__main__":
    test_large_raw_parse_short_marks_retry()
    test_small_raw_parse_short_marks_done()
    test_success_passes_session_id_to_shared_core()
    test_collect_scan_classifies_queue_marked_rollout_and_subagent()
    test_collect_scan_reoffers_stale_pending_but_skips_fresh_pending()
    test_status_mode_reports_queue_worker_and_note_without_mutation()
    print("ok - codex adapter")
