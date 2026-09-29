#!/usr/bin/env python3
"""Network-free regression tests for the Kimi Code CLI adapters."""

import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SHARED_DIR = HERE.parent / "shared"
sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(HERE.parents[1] / "src"))

# Neutralize ambient env so module-load + assertions are deterministic.
for _var in (
    "BORING_CONFIG",
    "BORING_HOME",
    "BORING_URL",
    "BORING_DOOR_URL",
    "BORING_EVENT_SINK",
    "BORING_EVENT_SPOOL",
    "BORING_EVENT_DB_MIRROR",
    "BORING_LLM_BASE_URL",
    "BORING_LLM_MODEL",
    "KIMI_CODE_HOME",
):
    os.environ.pop(_var, None)

# The pop above isolates these tests from the host's env, and in doing so it removed the one value
# that kept event writes off the production store: with no sink set, `event_log` defaults to the
# DB. Measured before this line, one run of this file wrote 2 rows into the live `event_log`, and
# `guard.sh` runs it every time — 1014 rows of fixture session `codex-abc` had accumulated by
# 2026-09-02, growing daily. Isolation and a closed write door are both required, so the pop is
# followed by an explicit spool rather than by nothing.
os.environ["BORING_EVENT_SINK"] = "spool"
os.environ.setdefault("BORING_EVENT_LOG", os.path.join(tempfile.gettempdir(), "omb-test-events.ndjson"))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


distill = _load("kimi_distill_session", "distill-session.py")
recall = _load("kimi_recall", "recall.py")
rules = _load("kimi_rules", "rules.py")
import recall_core  # noqa: E402

from ohmyboring import config as omb_env  # noqa: E402
from ohmyboring.adapters.engine import Unreachable  # noqa: E402
from ohmyboring.result import Err, Ok  # noqa: E402


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
            json.dumps({"sessionId": "session_abc", "sessionDir": str(session_dir), "workDir": "/x"}) + "\n",
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
    captured = io.StringIO()
    hits = [{"source_path": "vault/wiki/wiki-0007.md", "snippet": "fixed   the\ncache"}]
    with (
        mock.patch.object(
            recall.sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "prompt": "how did I fix the docker cache issue",
                        "origin": {"kind": "user"},
                    }
                )
            ),
        ),
        mock.patch.object(recall_core.DrudgeClient, "search", return_value=Ok(hits)),
        mock.patch.object(recall.sys, "stdout", captured),
    ):
        recall.main()
    payload = json.loads(captured.getvalue())
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "- [wiki-0007.md] fixed the cache" in ctx


def test_recall_failed_search_logs_to_stderr():
    recall_core.RETRIES = 0
    try:
        captured = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                recall.sys,
                "stdin",
                io.StringIO(
                    json.dumps(
                        {
                            "prompt": "how did I fix the docker cache issue",
                            "origin": {"kind": "user"},
                        }
                    )
                ),
            ),
            mock.patch.object(recall_core.DrudgeClient, "search", return_value=Err(Unreachable("down"))),
            mock.patch.object(recall.sys, "stdout", captured),
            mock.patch.object(recall.sys, "stderr", stderr),
        ):
            recall.main()
        assert captured.getvalue() == ""
        assert "[omb-recall] search failed" in stderr.getvalue()
    finally:
        recall_core.RETRIES = 1


def test_distill_invalid_stdin_logs_error():
    captured = io.StringIO()
    stderr = io.StringIO()
    with (
        mock.patch.object(distill.sys, "stdin", io.StringIO("not json")),
        mock.patch.object(distill.sys, "stdout", captured),
        mock.patch.object(distill.sys, "stderr", stderr),
        mock.patch.object(distill, "_throttled", return_value=False),
    ):
        rc = distill.main()
    assert captured.getvalue() == ""
    assert rc == 2
    assert "[omb-distill] invalid stdin JSON" in stderr.getvalue()


def test_distill_short_transcript_logs_skip_and_marks_done():
    with tempfile.TemporaryDirectory() as session_dir:
        root = Path(session_dir)
        event_path = root / "events.ndjson"
        captured = io.StringIO()
        stderr = io.StringIO()
        payload = {"session_id": "session_abc", "cwd": "/x", "hook_event_name": "SessionEnd"}
        with (
            mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(distill.sys, "stdout", captured),
            mock.patch.object(distill.sys, "stderr", stderr),
            mock.patch.object(distill, "_find_session_dir", return_value=session_dir),
            mock.patch.object(distill, "extract_session", return_value="too short"),
            mock.patch.object(distill, "git_remote_url", return_value=""),
            mock.patch.object(distill, "repo_slug", return_value="repo"),
            mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)),
            mock.patch.object(distill, "_mark") as mark,
            mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}),
        ):
            rc = distill.main()

        assert captured.getvalue() == ""
        assert rc == 0
        mark.assert_called_once_with("session_abc")
        assert "transcript too short" in stderr.getvalue()
        event = _read_last_event(event_path)
        assert event["reason"] == "too_short"
        assert event["workflow_node"] == "skipped"
        assert event["workflow_outcome"] == "skip"


def test_distill_queues_session_without_calling_the_llm():
    with tempfile.TemporaryDirectory() as session_dir:
        captured = io.StringIO()
        stderr = io.StringIO()
        payload = {"session_id": "session_abc", "cwd": "/x", "hook_event_name": "SessionEnd"}
        with (
            mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(distill.sys, "stdout", captured),
            mock.patch.object(distill.sys, "stderr", stderr),
            mock.patch.object(distill, "_find_session_dir", return_value=session_dir),
            mock.patch.object(distill, "extract_session", return_value="x" * 600),
            mock.patch.object(distill, "git_remote_url", return_value=""),
            mock.patch.object(distill, "repo_slug", return_value="repo"),
            mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)),
            mock.patch("distill_core._call_llm") as llm,
            mock.patch.object(distill.distill_queue, "enqueue") as enqueue,
            mock.patch("urllib.request.urlopen") as urlopen,
        ):
            rc = distill.main()

    assert captured.getvalue() == ""
    assert rc == 0
    llm.assert_not_called()
    urlopen.assert_not_called()
    item = enqueue.call_args.args[0]
    assert (item.session_id, item.agent, item.text) == ("session_abc", "kimi", "x" * 600)
    assert "queued for hermes" in stderr.getvalue()


def test_distill_run_returns_nonzero_on_crash():
    stderr = io.StringIO()
    with (
        mock.patch.object(distill, "main", side_effect=RuntimeError("boom")),
        mock.patch.object(distill.sys, "stderr", stderr),
    ):
        rc = distill.run()
    assert rc == 1
    assert "[omb-distill] crashed: boom" in stderr.getvalue()


def _read_last_event(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])


class _RulesStubHandler(BaseHTTPRequestHandler):
    """The door's stand-in: serves a fixed GET /rules payload and counts hits."""

    hits = 0
    payload: dict = {"rules": [], "incomplete": 0}

    def do_GET(self):  # noqa: N802 — http.server's spelling
        type(self).hits += 1
        body = json.dumps(type(self).payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


_RULES_RULE = {
    "subject": "rule-hermes-not-removed",
    "rule": "hermes 를 빼지 마세요",
    "trigger": "hermes + 제거|remove",
    "source_path": "/vault/wiki/wiki-1955.md",
}


def _run_rules(payload: dict, port: int, tmp: str):
    captured, stderr = io.StringIO(), io.StringIO()
    env = mock.patch.dict(
        os.environ,
        {
            "BORING_DOOR_URL": f"http://127.0.0.1:{port}",
            "BORING_EVENT_SINK": "spool",
            "BORING_EVENT_LOG": os.path.join(tmp, "events.ndjson"),
        },
    )
    with env:
        with (
            mock.patch.object(rules.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(rules.sys, "stdout", captured),
            mock.patch.object(rules.sys, "stderr", stderr),
        ):
            rules.main()
    return captured.getvalue(), stderr.getvalue()


def _run_rules_default_door(payload: dict, door: str, tmp: str):
    """Like _run_rules but with NO BORING_DOOR_URL at all — omb_env.door_url stands in for
    the engine-style default the hook now resolves on its own."""
    captured, stderr = io.StringIO(), io.StringIO()
    env = mock.patch.dict(
        os.environ,
        {
            "BORING_EVENT_SINK": "spool",
            "BORING_EVENT_LOG": os.path.join(tmp, "events.ndjson"),
        },
        clear=True,
    )
    with env, mock.patch.object(omb_env, "door_url", return_value=door):
        with (
            mock.patch.object(rules.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(rules.sys, "stdout", captured),
            mock.patch.object(rules.sys, "stderr", stderr),
        ):
            rules.main()
    return captured.getvalue(), stderr.getvalue()


def _rules_stub():
    stub = ThreadingHTTPServer(("127.0.0.1", 0), _RulesStubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    _RulesStubHandler.hits = 0
    _RulesStubHandler.payload = {"rules": [dict(_RULES_RULE)], "incomplete": 0}
    return stub


def test_rules_user_prompt_with_trigger_fires():
    """Kimi user-origin prompt carrying a trigger injects the rule (loopback stub door)."""
    stub = _rules_stub()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out, err = _run_rules(
                {"prompt": "hermes 제거해줘", "origin": {"kind": "user"}, "session_id": "s-1"},
                stub.server_address[1],
                tmp,
            )
            events_text = (Path(tmp) / "events.ndjson").read_text(encoding="utf-8")
        assert err == ""
        assert _RulesStubHandler.hits == 1
        payload = json.loads(out)
        assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert ctx.startswith("지켜야 할 규칙 (소유자가 바로잡은 것):")
        assert "- hermes 를 빼지 마세요 (출처 wiki-1955)" in ctx
        events = [json.loads(line) for line in events_text.splitlines()]
        fired = [e for e in events if e.get("event") == "rule_fired"]
        assert len(fired) == 1, fired
        assert fired[0]["subject"] == "rule-hermes-not-removed"
        assert fired[0]["session_id"] == "s-1"
    finally:
        stub.shutdown()
        stub.server_close()


def test_rules_no_door_url_uses_default_and_fires():
    """Unset BORING_DOOR_URL → the hook resolves the engine-style default and still fires.

    Same installed-env gap as Claude Code: the hook env has no BORING_DOOR_URL, and the
    old env-or-return made the rule silently skip. omb_env.door_url is patched to the
    stub the way the default would resolve; a user prompt with the trigger must fire.
    """
    stub = _rules_stub()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out, err = _run_rules_default_door(
                {"prompt": "hermes 제거해줘", "origin": {"kind": "user"}, "session_id": "s-2"},
                f"http://127.0.0.1:{stub.server_address[1]}",
                tmp,
            )
            events_file = Path(tmp) / "events.ndjson"
            events_text = events_file.read_text(encoding="utf-8") if events_file.exists() else ""
        assert err == ""
        assert _RulesStubHandler.hits == 1, "no env → the default door is still asked"
        payload = json.loads(out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert ctx.startswith("지켜야 할 규칙 (소유자가 바로잡은 것):")
        assert "- hermes 를 빼지 마세요 (출처 wiki-1955)" in ctx
        fired = [
            e
            for e in (json.loads(line) for line in events_text.splitlines())
            if e.get("event") == "rule_fired"
        ]
        assert len(fired) == 1, fired
        assert fired[0]["session_id"] == "s-2"
    finally:
        stub.shutdown()
        stub.server_close()


def test_rules_system_origin_prompt_with_trigger_stays_silent():
    """The live-measured bug: a Kimi system prompt containing the trigger fired the rule."""
    stub = _rules_stub()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out, err = _run_rules(
                {"prompt": "hermes 제거 관련 설정이야", "origin": {"kind": "system"}},
                stub.server_address[1],
                tmp,
            )
        assert out == ""
        assert err == ""
        assert _RulesStubHandler.hits == 0, "system-origin prompt must never reach the door"
    finally:
        stub.shutdown()
        stub.server_close()


if __name__ == "__main__":
    test_work_dir_key_format()
    test_find_session_dir_uses_index()
    test_extract_session_filters_injection()
    test_recall_skips_short_and_injection()
    test_recall_formats_context()
    test_recall_failed_search_logs_to_stderr()
    test_distill_invalid_stdin_logs_error()
    test_distill_short_transcript_logs_skip_and_marks_done()
    test_distill_queues_session_without_calling_the_llm()
    test_distill_run_returns_nonzero_on_crash()
    test_rules_user_prompt_with_trigger_fires()
    test_rules_no_door_url_uses_default_and_fires()
    test_rules_system_origin_prompt_with_trigger_stays_silent()
    print("ok - kimi agent adapters")
