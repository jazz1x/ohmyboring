#!/usr/bin/env python3
"""Network-free regression tests for the Kimi Code CLI adapters."""
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

# Neutralize ambient env so module-load + assertions are deterministic.
for _var in ("BORING_CONFIG", "OMB_HOME", "DRUDGE_URL", "DRUDGE_LLM_BASE_URL",
             "DRUDGE_LLM_MODEL", "KIMI_CODE_HOME"):
    os.environ.pop(_var, None)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


distill = _load("kimi_distill_session", "distill-session.py")
recall = _load("kimi_recall", "recall.py")


def test_work_dir_key_format():
    key = distill._work_dir_key("/home/user/my-project")
    assert key.startswith("wd_my-project_")
    assert len(key.split("_")[-1]) == 12


def test_find_session_dir_uses_index():
    with tempfile.TemporaryDirectory() as home:
        os.environ["KIMI_CODE_HOME"] = home
        session_dir = Path(home) / "sessions" / "wd_x_1234567890ab" / "session_abc"
        session_dir.mkdir(parents=True)
        index = Path(home) / "session_index.jsonl"
        index.write_text(
            json.dumps({"sessionId": "session_abc", "sessionDir": str(session_dir), "workDir": "/x"})
            + "\n",
            encoding="utf-8",
        )
        # Load a fresh module copy so the new KIMI_HOME constant is picked up.
        fresh = _load("kimi_distill_session_fresh", "distill-session.py")
        found = fresh._find_session_dir("session_abc", "/x")
        assert found == str(session_dir)


def test_extract_session_filters_injection():
    with tempfile.TemporaryDirectory() as d:
        wire = Path(d) / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        wire.write_text(
            json.dumps({"type": "turn.prompt", "input": [{"type": "text", "text": "hello"}]})
            + "\n"
            + json.dumps(
                {
                    "type": "context.append_message",
                    "message": {
                        "role": "user",
                        "origin": {"kind": "injection"},
                        "content": [{"type": "text", "text": "system reminder"}],
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "context.append_loop_event",
                    "event": {"type": "content.part", "part": {"type": "text", "text": "hi"}},
                }
            )
            + "\n"
        )
        out = distill.extract_session(str(d))
        assert "[user] hello" in out
        assert "[assistant] hi" in out
        assert "system reminder" not in out


def test_recall_skips_short_and_injection():
    assert recall._is_injection({"origin": {"kind": "injection"}}) is True
    assert recall._is_injection({"origin": {"kind": "user"}, "prompt": "hi"}) is False


def test_recall_formats_context():
    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    captured = io.StringIO()
    with mock.patch.object(recall.sys, "stdin", io.StringIO(json.dumps({
        "prompt": "how did I fix the docker cache issue",
        "origin": {"kind": "user"},
    }))), \
         mock.patch.object(recall.urllib.request, "urlopen",
                           return_value=_Resp({"hits": [{"source_path": "vault/wiki/wiki-0007.md",
                                                           "snippet": "fixed   the\ncache"}]})), \
         mock.patch.object(recall.sys, "stdout", captured):
        recall.main()
    payload = json.loads(captured.getvalue())
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "- [wiki-0007.md] fixed the cache" in ctx


def test_recall_failed_search_logs_to_stderr():
    recall.RETRIES = 0
    try:
        captured = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(recall.sys, "stdin", io.StringIO(json.dumps({
            "prompt": "how did I fix the docker cache issue",
            "origin": {"kind": "user"},
        }))), \
             mock.patch.object(recall.urllib.request, "urlopen", side_effect=OSError("down")), \
             mock.patch.object(recall.sys, "stdout", captured), \
             mock.patch.object(recall.sys, "stderr", stderr):
            recall.main()
        assert captured.getvalue() == ""
        assert "[omb-recall] search failed" in stderr.getvalue()
    finally:
        recall.RETRIES = 1


def test_distill_invalid_stdin_logs_error():
    captured = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(distill.sys, "stdin", io.StringIO("not json")), \
         mock.patch.object(distill.sys, "stdout", captured), \
         mock.patch.object(distill.sys, "stderr", stderr), \
         mock.patch.object(distill, "_throttled", return_value=False):
        distill.main()
    assert captured.getvalue() == ""
    assert "[omb-distill] invalid stdin JSON" in stderr.getvalue()


if __name__ == "__main__":
    test_work_dir_key_format()
    test_find_session_dir_uses_index()
    test_extract_session_filters_injection()
    test_recall_skips_short_and_injection()
    test_recall_formats_context()
    test_recall_failed_search_logs_to_stderr()
    test_distill_invalid_stdin_logs_error()
    print("ok - kimi agent adapters")
