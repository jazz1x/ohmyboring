#!/usr/bin/env python3
"""Network-free regression tests for the Claude Code hook helpers.

Run: python3 agents/claude-code/test_hooks.py   (no pytest dependency; unittest-based)
 or: python3 -m pytest agents/claude-code/test_hooks.py

Covers the PURE, no-network helpers in distill-session.py and recall.py:
  - markers.safe_id                      — throttle-marker id sanitization
  - distill-session.repo_slug             — folder-name fallback (no git remote needed)
  - distill-session.extract               — JSONL transcript → "[role] text" (file I/O only)
  - recall.main                           — context-injection formatting (urlopen mocked)

The two hook modules live in agents/claude-code/ and sys.path-insert ../shared and <repo>/src
to import ohmyboring.config (src/ohmyboring/test_config.py guards that resolver). We load them
by file path with importlib, after neutralizing ambient policy env so the run is deterministic.
HTTP is never touched: the pure helpers don't call out, and recall.main's urlopen is mocked.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
# Same sys.path the hooks set up. distill-session evaluates NOTE_LANG = config.note_lang() at
# import time, so ohmyboring.config must resolve.
SHARED_DIR = HERE.parent / "shared"
sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(HERE.parents[1] / "src"))

# Neutralize ambient policy/endpoint env so module-load + assertions are deterministic.
for _var in (
    "BORING_CONFIG",
    "BORING_HOME",
    "BORING_URL",
    "BORING_EVENT_SINK",
    "BORING_EVENT_SPOOL",
    "BORING_EVENT_DB_MIRROR",
    "RECALL_MAX_RESULTS",
    "RECALL_MAX_TOKENS",
    "RECALL_TIMEOUT",
    "RECALL_RETRIES",
    "RECALL_RELEVANCE_MAX_DIST",
    "BORING_DOOR_URL",
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
    """Load a hook module by file path (handles the hyphenated filename)."""
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


distill = _load("distill_session_hook", "distill-session.py")
recall = _load("recall_hook", "recall.py")
rules_hook = _load("rules_hook", "rules.py")
import distill_core  # noqa: E402

from ohmyboring import config as omb_env  # noqa: E402

# The recall tests drive the real injection path, which appends to the injection ledger.
# Redirect it before import or the suite writes into the owner's live cache.
os.environ["BORING_INJECTION_LEDGER"] = str(Path(tempfile.mkdtemp()) / "injections.jsonl")
import markers  # noqa: E402
import recall_core  # noqa: E402

from ohmyboring.adapters import engine  # noqa: E402
from ohmyboring.adapters import llm as llm_adapter  # noqa: E402
from ohmyboring.adapters.engine import Unreachable  # noqa: E402
from ohmyboring.result import Err, Ok  # noqa: E402


class MarkPathTests(unittest.TestCase):
    def test_sanitizes_unsafe_chars(self):
        sid = markers.safe_id("../../etc/passwd")
        self.assertEqual(sid, "etcpasswd")  # slashes/dots stripped, no traversal

    def test_empty_session_fallback(self):
        self.assertEqual(markers.safe_id(""), "nosession")

    def test_preserves_safe_chars(self):
        self.assertEqual(markers.safe_id("abc-123_XY"), "abc-123_XY")


class RepoSlugTests(unittest.TestCase):
    def test_folder_fallback_when_no_git(self):
        # An empty cwd has no git remote → folder-name fallback; "" cwd → "".
        self.assertEqual(distill.repo_slug(""), "")

    def test_a_non_repo_directory_names_no_project(self):
        # Was `test_basename_of_cwd`: a folder outside git used to be named after itself, which
        # invented projects — 18 notes ended up filed under `다시한번`, and single notes under
        # `t5-parts` and `boro-janus-worker`. A phantom project takes a line in the briefing and
        # a row in the graph, and nothing later can tell it from a real one.
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "my-project")
            os.makedirs(sub)
            self.assertEqual(distill.repo_slug(sub), "")

    def test_git_remote_from_subdirectory(self):
        # Working from a subdir should still resolve to the repo's remote slug.
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", d], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", d, "remote", "add", "origin", "https://github.com/acme/widget.git"],
                check=True,
                capture_output=True,
            )
            sub = os.path.join(d, "src", "components")
            os.makedirs(sub)
            self.assertEqual(distill.repo_slug(sub), "widget")

    def test_repo_slug_various_remote_url_formats(self):
        cases = [
            ("https://github.com/acme/widget.git", "widget"),
            ("https://github.com/acme/widget", "widget"),
            ("git@github.com:acme/widget.git", "widget"),
            ("git@github.com:acme/widget", "widget"),
            ("ssh://git@github.com/acme/widget.git", "widget"),
        ]
        for url, expected in cases:
            with mock.patch.object(distill_core, "git_remote_url", return_value=url):
                self.assertEqual(distill.repo_slug("/tmp/foo"), expected)

    def test_no_remote_resolves_through_the_main_worktree_not_the_folder_name(self):
        # A task worktree whose remote cannot be read (never set, or the parent checkout is gone)
        # used to be named after its own folder — and that name is `<repo>-<task>`.
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "widget")
            os.makedirs(repo)
            for args in (
                ["init", "-q"],
                ["config", "user.email", "t@example.invalid"],
                ["config", "user.name", "t"],
                ["config", "commit.gpgsign", "false"],
            ):
                subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)
            open(os.path.join(repo, "f"), "w").close()
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True, capture_output=True)
            subprocess.run(["git", "-C", repo, "commit", "-qm", "init"], check=True, capture_output=True)
            wt = os.path.join(d, "widget-t5-parts")
            subprocess.run(
                ["git", "-C", repo, "worktree", "add", "-q", "--detach", wt, "HEAD"],
                check=True,
                capture_output=True,
            )

            with mock.patch.object(distill_core, "git_remote_url", return_value=""):
                self.assertEqual(distill.repo_slug(wt), "widget")
                self.assertEqual(distill.repo_slug(repo), "widget")
                self.assertEqual(distill.repo_slug(""), "")


class ExtractTranscriptTests(unittest.TestCase):
    def test_extracts_user_and_assistant_text(self):
        lines = [
            {"message": {"role": "user", "content": "hi there"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello"}, {"type": "tool_use", "name": "x"}],
                }
            },
            {"message": {"role": "system", "content": "ignored"}},
            {"type": "summary"},  # non user/assistant → skipped
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for obj in lines:
                f.write(json.dumps(obj) + "\n")
            f.write("{ not json\n")  # malformed line is tolerated
            path = f.name
        try:
            out = distill.extract(path)
        finally:
            os.unlink(path)
        self.assertEqual(out, "[user] hi there\n[assistant] hello")


class SessionStartRecallTests(unittest.TestCase):
    """session-start-recall.py reads stdin + calls /context; NO network."""

    def _run_main(self, payload, context_resp=None, side_effect=None):
        captured = io.StringIO()
        stderr = io.StringIO()
        if context_resp is None:
            context_resp = {
                "decisions": [],
                "risks": [],
                "facts": [],
                "glossary": [],
                "language": "ko",
            }

        def fake_context(_self, project=None, max_items=5):
            if side_effect is not None:
                return Err(Unreachable(str(side_effect)))
            return Ok(context_resp)

        module = _load("session_start_recall", "session-start-recall.py")
        with (
            mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(module.sys, "stdout", captured),
            mock.patch.object(module.sys, "stderr", stderr),
            mock.patch.object(module.DrudgeClient, "context", fake_context),
            # No BORING_DOOR_URL here → the hook now tries the default door; keep the suite offline.
            mock.patch.object(
                module.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("no door in unit tests"),
            ),
        ):
            module.main()
        return captured.getvalue(), stderr.getvalue()

    def test_session_start_with_project_formats_context_card(self):
        out, _err = self._run_main(
            {"hook_event_name": "SessionStart", "cwd": "/tmp/my-project"},
            context_resp={
                "decisions": [
                    {
                        "subject": "omb",
                        "predicate": "use",
                        "value": "context cards",
                        "kind": "decision",
                        "confidence": "certain",
                    }
                ],
                "risks": [
                    {
                        "subject": "omb",
                        "predicate": "risk",
                        "value": "token noise",
                        "kind": "risk",
                        "confidence": "likely",
                    }
                ],
                "facts": [],
                "glossary": [],
                "language": "ko",
            },
        )
        payload = json.loads(out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Decisions", ctx)
        self.assertIn("[decision|certain] omb use: context cards", ctx)
        self.assertIn("Risks", ctx)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_session_start_without_project_uses_recent_work(self):
        out, _err = self._run_main(
            {"hook_event_name": "SessionStart", "cwd": ""},
            context_resp={
                "decisions": [],
                "risks": [],
                "facts": [
                    {"subject": "x", "predicate": "is", "value": "y", "kind": "fact", "confidence": "certain"}
                ],
                "glossary": [],
                "language": "ko",
            },
        )
        payload = json.loads(out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("recent work", ctx)
        self.assertIn("Facts", ctx)

    def test_non_sessionstart_is_noop(self):
        out, _err = self._run_main({"hook_event_name": "UserPromptSubmit", "cwd": "/x"})
        self.assertEqual(out, "")

    def test_failed_context_is_silent(self):
        out, _err = self._run_main(
            {"hook_event_name": "SessionStart", "cwd": "/x"},
            side_effect=OSError("down"),
        )
        self.assertEqual(out, "")
        self.assertIn("context failed", _err)


class DoorApprovedSectionTests(unittest.TestCase):
    """The 「오늘 승인한 것」 section in session-start-recall.py — three fates (AC5).

    The hook must prepend what the morning card's 「해」 judged when the door answers,
    say so on stderr and continue when the door is dead, and with no BORING_DOOR_URL
    still attempt the fetch on the engine-style default — the installed hook env has
    no BORING_DOOR_URL, and the silent skip there was the 09-27 live bug.
    """

    class _DoorResp:
        def __init__(self, payload):
            self._body = json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self._body

    def _run_with_door(self, env, urlopen):
        captured = io.StringIO()
        stderr = io.StringIO()
        module = _load("session_start_recall_door", "session-start-recall.py")
        with (
            mock.patch.object(
                module.sys,
                "stdin",
                io.StringIO(json.dumps({"hook_event_name": "SessionStart", "cwd": "/tmp/my-project"})),
            ),
            mock.patch.object(module.sys, "stdout", captured),
            mock.patch.object(module.sys, "stderr", stderr),
            mock.patch.object(
                module.DrudgeClient,
                "context",
                lambda _s, project=None, max_items=5: Ok(
                    {
                        "decisions": [],
                        "risks": [],
                        "facts": [],
                        "glossary": [],
                        "language": "ko",
                    }
                ),
            ),
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(module.urllib.request, "urlopen", urlopen),
        ):
            module.main()
        return captured.getvalue(), stderr.getvalue()

    def test_live_door_prepends_approved_section(self):
        payload = {
            "since_hours": 24,
            "approved": [
                {"session": "slack:C1:1758470000.5", "note": "/vault/wiki/wiki-1734.md", "at": "t"},
                {"session": "slack:C1:1758470001.5", "note": "/vault/wiki/wiki-1735.md", "at": "t"},
            ],
            "contested": [],
        }
        seen_urls = []

        def urlopen(url, timeout):
            seen_urls.append(url)
            return self._DoorResp(payload)

        out, err = self._run_with_door({"BORING_DOOR_URL": "http://127.0.0.1:7710"}, urlopen)
        self.assertEqual(err, "")
        self.assertEqual(seen_urls, ["http://127.0.0.1:7710/approved?since_hours=24"])
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        approved_at = ctx.index("## 오늘 승인한 것 (2)")
        card_at = ctx.index("📚 Project context")
        self.assertLess(approved_at, card_at, "the approval section goes ahead of the card")
        self.assertIn("- /vault/wiki/wiki-1734.md", ctx)
        self.assertIn("- /vault/wiki/wiki-1735.md", ctx)

    def test_dead_door_logs_to_stderr_and_omits_the_section(self):
        def urlopen(url, timeout):
            raise urllib.error.URLError("connection refused")

        out, err = self._run_with_door({"BORING_DOOR_URL": "http://127.0.0.1:7710"}, urlopen)
        self.assertIn("[omb-start-recall] approved fetch failed", err)
        self.assertNotIn("오늘 승인한 것", out)
        # the card itself still reaches the session — the hook lives
        self.assertIn("📚 Project context", json.loads(out)["hookSpecificOutput"]["additionalContext"])

    def test_no_door_url_attempts_the_default_door(self):
        """Unset BORING_DOOR_URL → the engine-style default (localhost:7710), not a silent skip."""
        called = []

        def urlopen(url, timeout):
            called.append(url)
            return self._DoorResp({"approved": [{"note": "/vault/wiki/wiki-1734.md"}], "contested": []})

        with mock.patch.object(omb_env, "_in_container", return_value=False):
            out, err = self._run_with_door({}, urlopen)
        self.assertEqual(
            called,
            ["http://localhost:7710/approved?since_hours=24"],
            "no env → fetch the default door, as the engine URL already does",
        )
        self.assertEqual(err, "")
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("## 오늘 승인한 것 (1)", ctx)
        self.assertIn("- /vault/wiki/wiki-1734.md", ctx)


class DistillExitCodeTests(unittest.TestCase):
    def test_invalid_stdin_returns_input_error(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(distill.sys, "stdin", io.StringIO("not json")),
            mock.patch.object(distill.sys, "stderr", stderr),
        ):
            rc = distill.main()

        self.assertEqual(rc, 2)
        self.assertIn("[omb-distill] invalid stdin JSON", stderr.getvalue())

    def test_short_transcript_logs_skip_and_marks_done(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("{}\n")
            path = f.name
        try:
            with tempfile.TemporaryDirectory() as d:
                event_path = os.path.join(d, "events.ndjson")
                stderr = io.StringIO()
                payload = {
                    "transcript_path": path,
                    "session_id": "abc",
                    "cwd": "/work/oh-my-boring",
                    "hook_event_name": "SessionEnd",
                }
                with (
                    mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(distill.sys, "stderr", stderr),
                    mock.patch.object(distill, "extract", return_value="too short"),
                    mock.patch.object(distill, "git_remote_url", return_value=""),
                    mock.patch.object(distill, "repo_slug", return_value="oh-my-boring"),
                    mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)),
                    mock.patch.object(distill, "_mark") as mark,
                    mock.patch.dict(
                        os.environ, {"BORING_EVENT_LOG": event_path, "BORING_EVENT_SINK": "spool"}
                    ),
                ):
                    rc = distill.main()

                self.assertEqual(rc, 0)
                mark.assert_called_once_with("abc")
                self.assertIn("transcript too short", stderr.getvalue())
                event = _read_last_event(event_path)
                self.assertEqual(event["reason"], "too_short")
                self.assertEqual(event["workflow_node"], "skipped")
                self.assertEqual(event["workflow_outcome"], "skip")
        finally:
            os.unlink(path)

    def test_session_is_queued_without_calling_the_llm(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("{}\n")
            path = f.name
        try:
            stderr = io.StringIO()
            payload = {
                "transcript_path": path,
                "session_id": "abc",
                "cwd": "/work/oh-my-boring",
                "hook_event_name": "SessionEnd",
            }
            with (
                mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))),
                mock.patch.object(distill.sys, "stderr", stderr),
                mock.patch.object(distill, "extract", return_value="x" * 600),
                mock.patch.object(distill, "git_remote_url", return_value=""),
                mock.patch.object(distill, "repo_slug", return_value="oh-my-boring"),
                mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)),
                mock.patch.object(llm_adapter, "call_llm") as llm,
                mock.patch.object(engine, "call_remember") as remember,
                tempfile.TemporaryDirectory() as mark_dir,
                mock.patch.object(distill.distill_queue.markers, "MARK_DIR", mark_dir),
                mock.patch("urllib.request.urlopen") as urlopen,
            ):
                rc = distill.main()
                queued = distill.distill_queue.drain()

            self.assertEqual(rc, 0)
            llm.assert_not_called()
            urlopen.assert_not_called()
            remember.assert_not_called()
            self.assertEqual(
                [(i.session_id, i.agent, i.text) for i in queued], [("abc", "claude-code", "x" * 600)]
            )
            self.assertIn("queued for hermes", stderr.getvalue())
        finally:
            os.unlink(path)

    def test_run_returns_nonzero_on_crash(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(distill, "main", side_effect=RuntimeError("boom")),
            mock.patch.object(distill.sys, "stderr", stderr),
        ):
            rc = distill.run()

        self.assertEqual(rc, 1)
        self.assertIn("[omb-distill] crashed: boom", stderr.getvalue())


class RecallFormattingTests(unittest.TestCase):
    """recall.main reads stdin + posts to /search; we mock urlopen so NO network happens."""

    def _run_main(self, prompt, hits, search_side_effect=None, session_id=None, handover=None):
        captured = io.StringIO()
        stderr = io.StringIO()
        if search_side_effect is None:
            search_mock = mock.MagicMock(return_value=Ok(hits))
        else:
            search_mock = mock.MagicMock(side_effect=search_side_effect)
        # The engine's handover door is a network call; it is mocked here so the tests never
        # reach it, and so a test can look at what was handed.
        handover_mock = handover or mock.MagicMock(return_value=Ok({"handed": 0, "unknown": 0}))
        stdin = {"prompt": prompt}
        if session_id:
            stdin["session_id"] = session_id
        # recall_core writes its own diagnostics through its own `sys`, so both modules' stderr
        # must be captured or the over-ceiling report escapes the assertion.
        with (
            mock.patch.object(recall.sys, "stdin", io.StringIO(json.dumps(stdin))),
            mock.patch.object(recall_core.DrudgeClient, "search", search_mock),
            mock.patch.object(recall_core.DrudgeClient, "handover", handover_mock),
            mock.patch.object(recall.sys, "stdout", captured),
            mock.patch.object(recall.sys, "stderr", stderr),
            mock.patch.object(recall_core.sys, "stderr", stderr),
        ):
            recall.main()
        return captured.getvalue(), stderr.getvalue()

    # ── the engine learns what was handed, not what was fetched ─────────────────────
    def test_injected_notes_are_handed_to_the_engine_under_the_session(self):
        hits = [
            {"source_path": f"/vault/wiki/wiki-{i:04d}.md", "snippet": f"note {i} body text"}
            for i in range(recall_core.MAX_RESULTS + recall_core.CONTROL_RESULTS)
        ]
        handover = mock.MagicMock(return_value=Ok({"handed": recall_core.MAX_RESULTS, "unknown": 0}))
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"BORING_INJECTION_LEDGER": os.path.join(tmp, "l.jsonl")}),
        ):
            out, err = self._run_main(
                "a sufficiently long prompt", hits, session_id="s-hand", handover=handover
            )
        self.assertIn("additionalContext", out)
        self.assertEqual(handover.call_count, 1)
        session, _observed_at, paths = handover.call_args.args
        self.assertEqual(session, "s-hand")
        self.assertEqual(
            paths,
            [h["source_path"] for h in hits[: recall_core.MAX_RESULTS]],
            "controls were fetched, not handed — they must not be recorded",
        )
        self.assertNotIn("handover failed", err)

    def test_no_session_means_nothing_is_handed(self):
        handover = mock.MagicMock()
        self._run_main(
            "a sufficiently long prompt",
            [{"source_path": "/vault/wiki/wiki-0001.md", "snippet": "x y z"}],
            handover=handover,
        )
        handover.assert_not_called()

    def test_a_dead_handover_door_does_not_cost_the_prompt(self):
        handover = mock.MagicMock(return_value=Err(Unreachable("refused")))
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"BORING_INJECTION_LEDGER": os.path.join(tmp, "l.jsonl")}),
        ):
            out, err = self._run_main(
                "a sufficiently long prompt",
                [{"source_path": "/vault/wiki/wiki-0001.md", "snippet": "x y z"}],
                session_id="s-dead",
                handover=handover,
            )
        self.assertIn("additionalContext", out, "the context still reaches the prompt")
        self.assertIn("[omb-recall] handover failed", err)

    # ── 엔진이 200 비JSON 이나 소켓 끊김으로 답해도 옛 줄 그대로, 훅은 죽지 않는다 ──────────
    def _run_main_over_wire(self, engine_answers):
        """진짜 DrudgeClient 로 돌린다 — urlopen 만 가짜. `engine_answers(path)` 가 응답 객체를
        내놓거나 예외 객체를 내놓는다(예외면 던진다)."""

        def fake_urlopen(req, timeout):
            answer = engine_answers(req.full_url.rsplit("/", 1)[-1])
            if isinstance(answer, BaseException):
                raise answer
            return answer

        captured, stderr = io.StringIO(), io.StringIO()
        stdin = {"prompt": "a sufficiently long prompt", "session_id": "s-wire"}
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"BORING_INJECTION_LEDGER": os.path.join(tmp, "l.jsonl")}),
            mock.patch.object(recall.sys, "stdin", io.StringIO(json.dumps(stdin))),
            mock.patch.object(urllib.request, "urlopen", fake_urlopen),
            mock.patch.object(recall.sys, "stdout", captured),
            mock.patch.object(recall.sys, "stderr", stderr),
            mock.patch.object(recall_core.sys, "stderr", stderr),
        ):
            recall.main()
        return captured.getvalue(), stderr.getvalue()

    class _Body:
        def __init__(self, raw: bytes):
            self._raw = raw

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._raw

    def _hits_then(self, handover_answer):
        hits = {"hits": [{"source_path": "/vault/wiki/wiki-0001.md", "snippet": "x y z"}]}

        def answers(path):
            return self._Body(json.dumps(hits).encode()) if path == "search" else handover_answer

        return answers

    def test_a_handover_answered_with_non_json_keeps_the_context_and_the_old_line(self):
        old = json.JSONDecodeError("Expecting value", "<html>", 0)
        out, err = self._run_main_over_wire(self._hits_then(self._Body(b"<html>bad gateway</html>")))
        self.assertIn("additionalContext", out)
        self.assertIn(f"[omb-recall] handover failed: {old}", err)

    def test_a_handover_reset_by_the_peer_keeps_the_context_and_the_old_line(self):
        reset = ConnectionResetError(104, "Connection reset by peer")
        out, err = self._run_main_over_wire(self._hits_then(reset))
        self.assertIn("additionalContext", out)
        self.assertIn(f"[omb-recall] handover failed: {reset}", err)

    def test_a_search_answered_with_non_json_is_a_silent_noop_with_the_old_line(self):
        old = json.JSONDecodeError("Expecting value", "<html>", 0)
        out, err = self._run_main_over_wire(lambda _path: self._Body(b"<html>bad gateway</html>"))
        self.assertEqual(out, "")
        self.assertIn(f"[omb-recall] search failed after {recall_core.RETRIES} retries: {old}", err)

    def test_a_search_reset_by_the_peer_is_a_silent_noop_with_the_old_line(self):
        reset = ConnectionResetError(104, "Connection reset by peer")
        out, err = self._run_main_over_wire(lambda _path: reset)
        self.assertEqual(out, "")
        self.assertIn(f"[omb-recall] search failed after {recall_core.RETRIES} retries: {reset}", err)

    def test_short_prompt_is_noop(self):
        # < 8 chars → recall is meaningless → no output, urlopen never reached.
        out, err = self._run_main("hi", [{"source_path": "x", "snippet": "y"}])
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    # ── admission: harness-injected text must not trigger a retrieval ────────────────
    # `_MUST_NOT_SEARCH` makes these kill: if the skip fails, search raises, recall_core
    # catches it and writes "[omb-recall] search failed" to stderr, and the empty-stderr
    # assertion fires. Asserting only on empty stdout would pass even without the skip,
    # because a raising search also produces no context block.
    _MUST_NOT_SEARCH = AssertionError("recall searched on a prompt it should have skipped")

    def test_task_notification_is_noop(self):
        # 695 of 2,178 firings over 7 days were these — the harness talking, not the user.
        out, err = self._run_main(
            "<task-notification>\n<task-id>abc</task-id>\n<status>completed</status>",
            [],
            search_side_effect=self._MUST_NOT_SEARCH,
        )
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_image_only_prompt_is_noop(self):
        out, err = self._run_main("[Image #1] [Image #2]", [], search_side_effect=self._MUST_NOT_SEARCH)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_image_with_a_question_still_fires(self):
        # An image plus words is a real turn; only the placeholder-only case is inert.
        out, _err = self._run_main(
            "[Image #1] 이 그래프가 왜 이래?",
            [{"source_path": "vault/wiki/wiki-0007.md", "snippet": "graph"}],
        )
        self.assertIn("- [wiki-0007.md] graph", json.loads(out)["hookSpecificOutput"]["additionalContext"])

    def test_short_real_prompt_still_fires(self):
        # Guards against re-introducing a length rule. Sampling production showed the
        # sub-12-character prompts are mostly real turns; this one is exactly 8 chars and
        # must survive both the existing < 8 gate and the injection predicate.
        out, _err = self._run_main(
            "어드바이저 불러",
            [{"source_path": "vault/wiki/wiki-0007.md", "snippet": "advisor"}],
        )
        self.assertIn("- [wiki-0007.md] advisor", json.loads(out)["hookSpecificOutput"]["additionalContext"])

    def test_formats_context_block(self):
        out, _err = self._run_main(
            "how did I fix the docker cache issue",
            [{"source_path": "vault/wiki/wiki-0007.md", "snippet": "fixed   the\ncache"}],
        )
        payload = json.loads(out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("- [wiki-0007.md] fixed the cache", ctx)  # basename + whitespace collapsed
        self.assertIn("self-augmenting RAG recall", ctx)  # the injection fence is present

    _NEAR = {"source_path": "x/near.md", "snippet": "close", "dist": 0.3, "dist_kind": "vector_cosine"}
    _FAR = {"source_path": "x/far.md", "snippet": "irrelevant", "dist": 0.9, "dist_kind": "vector_cosine"}
    # The fixtures sit far either side of the constant so retuning it cannot make these vacuous.

    def test_hit_over_the_ceiling_is_kept_and_only_reported(self):
        # The ceiling is an instrument, not a filter: a hit at 0.9 — three times the constant —
        # still reaches the prompt, and the cost of filtering it is printed instead.
        out, err = self._run_main("a sufficiently long prompt", [self._NEAR, self._FAR])
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- [near.md] close", ctx)
        self.assertIn("- [far.md] irrelevant", ctx)
        self.assertIn("would drop 1/2", err)
        self.assertIn("far.md@0.9000", err)  # the dist is reported, so the cost stays measurable

    def test_every_hit_over_the_ceiling_still_injects(self):
        # The worst case for a filter: the only hit is over the ceiling. Enforcement would leave
        # the turn with no recall at all; this asserts the hit survives and is still measured.
        out, err = self._run_main("a sufficiently long prompt", [self._FAR])
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- [far.md] irrelevant", ctx)
        self.assertIn("would drop 1/1", err)

    def test_no_enforcement_knob_exists(self):
        # The guard against reintroducing a one-env-var kill switch. 0.514 would have discarded
        # 47.8% of 23 live injections (docs/reports/2026-08-15-program-audit.md), and the two
        # error classes overlap, so there is deliberately nothing to flip: a scalar ceiling cannot
        # separate them at any value. A replacement predicate has to clear data/eval first.
        self.assertFalse(hasattr(recall_core, "RELEVANCE_ENFORCE"))
        src = Path(recall_core.__file__).read_text()
        self.assertNotIn("RECALL_RELEVANCE_ENFORCE", src)

    def test_text_rank_hit_is_never_relevance_filtered(self):
        # text_rank is ts_rank (higher=better, unbounded) — not comparable to a cosine ceiling.
        out, _err = self._run_main(
            "a sufficiently long prompt",
            [{"source_path": "x/kw.md", "snippet": "keyword hit", "dist": 999.0, "dist_kind": "text_rank"}],
        )
        payload = json.loads(out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- [kw.md] keyword hit", ctx)

    def test_hit_without_dist_is_never_relevance_filtered(self):
        # wiki-recall fallback (BORING_VECTOR=off) has no comparable score — always passes through.
        out, _err = self._run_main(
            "a sufficiently long prompt",
            [{"source_path": "x/wiki.md", "snippet": "no score field"}],
        )
        payload = json.loads(out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- [wiki.md] no score field", ctx)

    def test_empty_hits_noop(self):
        out, _err = self._run_main("a sufficiently long prompt", [])
        self.assertEqual(out, "")

    def test_failed_search_logs_to_stderr(self):
        # search keeps raising → after retries main logs the failure to stderr and exits gracefully.
        recall_core.RETRIES = 0  # one attempt only for deterministic test
        try:
            out, err = self._run_main(
                "a sufficiently long prompt", [], search_side_effect=[Err(Unreachable("down"))]
            )
            self.assertEqual(out, "")
            self.assertIn("[omb-recall] search failed", err)
        finally:
            recall_core.RETRIES = 1


class DistillMainLoggingTests(unittest.TestCase):
    """distill-session.main failure paths must log to stderr instead of staying silent."""

    def _run_main(self, stdin_text, **patches):
        captured = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(distill.sys, "stdin", io.StringIO(stdin_text)),
            mock.patch.object(distill.sys, "stdout", captured),
            mock.patch.object(distill.sys, "stderr", stderr),
            mock.patch.object(distill, "_throttled", return_value=False),
        ):
            for key, val in patches.items():
                patcher = mock.patch.object(distill, key, val)
                patcher.start()
                self.addCleanup(patcher.stop)
            distill.main()
        return captured.getvalue(), stderr.getvalue()

    def test_invalid_stdin_logs_error(self):
        _out, err = self._run_main("not json")
        self.assertIn("[omb-distill] invalid stdin JSON", err)

    def test_missing_transcript_logs_error(self):
        _out, err = self._run_main(
            json.dumps({"session_id": "s1", "cwd": "/tmp", "hook_event_name": "SessionEnd"})
        )
        self.assertIn("[omb-distill] transcript not found", err)


class ClampWiringTest(unittest.TestCase):
    """The distill clamp must come from transcript.py, not a literal in the hook.

    This guards a defect that shipped: the hook read `or "2000"` while the extractor
    produced a median of 12,420 chars, so the model saw 0.16% of a median session and
    every upstream extraction improvement was discarded at this line. A literal here is
    the regression, so the assertion is against the shared constant rather than a number
    repeated in the test.
    """

    def test_clamp_comes_from_the_shared_constant(self):
        import transcript

        self.assertEqual(distill.CLAMP, transcript.CLAUDE_CLAMP_DEFAULT)

    def test_clamp_admits_a_median_extraction(self):
        # Measured over 40 real sessions (seed 11): extraction median 24,243 chars after
        # the tool-action allowlist, 12,420 before. A clamp under the pre-allowlist median
        # would mean the extractor's output never reaches the model intact.
        self.assertGreaterEqual(distill.CLAMP, 12000)


def _read_last_event(path):
    with open(path, encoding="utf-8") as f:
        return json.loads(f.readlines()[-1])


class RulesStubHandler(BaseHTTPRequestHandler):
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


class RulesHookTests(unittest.TestCase):
    """hooks/rules.py against a stub door on localhost — real HTTP loopback, nothing mocked.

    Covers: a firing prompt injects the header + one line per rule and records one
    rule_fired event per rule; a non-matching prompt is silent; a dead door is one
    stderr line and no output; harness-injected prompts never reach the door; with no
    BORING_DOOR_URL the hook falls back to the engine-style default (omb_env.door_url)
    and still reaches the door — the installed hook env has none, and the silent skip
    was the 09-27 live bug.
    """

    RULE = {
        "subject": "rule-hermes-not-removed",
        "rule": "hermes 를 빼지 마세요",
        "trigger": "hermes + remove|drop",
        "source_path": "/vault/wiki/wiki-1955.md",
    }

    def setUp(self):
        self.stub = ThreadingHTTPServer(("127.0.0.1", 0), RulesStubHandler)
        self.stub_thread = threading.Thread(target=self.stub.serve_forever, daemon=True)
        self.stub_thread.start()
        RulesStubHandler.hits = 0
        RulesStubHandler.payload = {"rules": [dict(self.RULE)], "incomplete": 0}
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {
                "BORING_DOOR_URL": f"http://127.0.0.1:{self.stub.server_address[1]}",
                "BORING_EVENT_SINK": "spool",
                "BORING_EVENT_LOG": os.path.join(self._tmp.name, "events.ndjson"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self):
        self.stub.shutdown()
        self.stub.server_close()

    def _run(self, payload):
        captured, stderr = io.StringIO(), io.StringIO()
        module = _load("rules_hook_live", "rules.py")
        with (
            mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(module.sys, "stdout", captured),
            mock.patch.object(module.sys, "stderr", stderr),
        ):
            module.main()
        return captured.getvalue(), stderr.getvalue()

    def test_firing_prompt_injects_the_rule_and_records_the_event(self):
        out, err = self._run({"prompt": "Hermes 좀 remove 해줘", "session_id": "s-1"})
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.startswith("지켜야 할 규칙 (소유자가 바로잡은 것):"), ctx)
        self.assertIn("- hermes 를 빼지 마세요 (출처 wiki-1955)", ctx)
        with open(os.path.join(self._tmp.name, "events.ndjson"), encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        fired = [e for e in events if e.get("event") == "rule_fired"]
        self.assertEqual(len(fired), 1, fired)
        self.assertEqual(fired[0]["component"], "rules")
        self.assertEqual(fired[0]["status"], "ok")
        self.assertEqual(fired[0]["subject"], "rule-hermes-not-removed")
        self.assertEqual(fired[0]["session_id"], "s-1")

    def test_no_trigger_match_is_silent_but_the_door_was_asked(self):
        out, err = self._run({"prompt": "그냥 평범한 질문이에요", "session_id": "s-2"})
        self.assertEqual(out, "")
        self.assertEqual(err, "")
        self.assertEqual(RulesStubHandler.hits, 1)
        self.assertFalse(
            os.path.exists(os.path.join(self._tmp.name, "events.ndjson")),
            "no firing → no rule_fired event",
        )

    def test_dead_door_is_one_stderr_line_and_no_output(self):
        os.environ["BORING_DOOR_URL"] = "http://127.0.0.1:1"  # nothing listens there
        out, err = self._run({"prompt": "hermes remove", "session_id": "s-3"})
        self.assertEqual(out, "")
        lines = [line for line in err.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, err)
        self.assertIn("[omb-rules] rules fetch failed", err)

    def test_injection_prompt_never_reaches_the_door(self):
        out, err = self._run({"prompt": "<task-notification>\n<status>done</status>"})
        self.assertEqual(out, "")
        self.assertEqual(err, "")
        self.assertEqual(RulesStubHandler.hits, 0)

    def test_subagent_report_never_reaches_the_door(self):
        # The report quotes owner words ("hermes remove"), so without the skip it would fire.
        for prompt in (
            'Another Claude session sent a message:\n<agent-message from="a1">hermes remove</agent-message>',
            '<agent-message from="a1">hermes remove</agent-message>',
        ):
            out, err = self._run({"prompt": prompt, "session_id": "s-sub"})
            self.assertEqual(out, "", prompt)
            self.assertEqual(err, "", prompt)
        self.assertEqual(RulesStubHandler.hits, 0)

    def test_no_door_url_falls_back_to_the_default_and_fires(self):
        # The installed-hook bug: Claude Code's hook env has no BORING_DOOR_URL and the
        # hook used to return silently. Now an unset env means the engine-style default,
        # so a firing prompt must still reach the door (door_url patched to this stub).
        os.environ.pop("BORING_DOOR_URL", None)
        with mock.patch.object(
            omb_env, "door_url", return_value=f"http://127.0.0.1:{self.stub.server_address[1]}"
        ):
            out, err = self._run({"prompt": "hermes remove", "session_id": "s-4"})
        self.assertEqual(err, "")
        self.assertEqual(RulesStubHandler.hits, 1)
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn(
            "- hermes 를 빼지 마세요 (출처 wiki-1955)", payload["hookSpecificOutput"]["additionalContext"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
