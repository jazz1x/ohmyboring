#!/usr/bin/env python3
"""Network-free regression tests for the Codex adapter."""
import importlib.util
import io
import json
import os
import sys
import tempfile
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


if __name__ == "__main__":
    test_large_raw_parse_short_marks_retry()
    test_small_raw_parse_short_marks_done()
    print("ok - codex adapter")
