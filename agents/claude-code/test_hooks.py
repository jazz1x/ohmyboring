#!/usr/bin/env python3
"""Network-free regression tests for the Claude Code hook helpers.

Run: python3 agents/claude-code/test_hooks.py   (no pytest dependency; unittest-based)
 or: python3 -m pytest agents/claude-code/test_hooks.py

Covers the PURE, no-network helpers in distill-session.py and recall.py:
  - distill-session._extract_json         — LLM-JSON extraction (fences + trailing prose)
  - distill-session._strip_trailing_metadata — drop trailing tags/tools/concepts blocks
  - distill-session._build_prompt         — prompt assembly (JSON skeleton + transcript)
  - markers.safe_id                       — throttle-marker id sanitization
  - distill-session.repo_slug             — folder-name fallback (no git remote needed)
  - distill-session.extract               — JSONL transcript → "[role] text" (file I/O only)
  - recall.main                           — context-injection formatting (urlopen mocked)

The two hook modules live in agents/claude-code/ and sys.path-insert ../shared to import
boring_config (test_boring_config.py guards that resolver). We load them by file path with
importlib the same way, after neutralizing ambient policy env so the run is deterministic.
HTTP is never touched: the pure helpers don't call out, and recall.main's urlopen is mocked.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
# Import boring_config the way the hooks do (insert agents/shared on sys.path). distill-session
# evaluates NOTE_LANG = boring_config.note_lang() at import time, so the dep must resolve.
SHARED_DIR = HERE.parent / "shared"
sys.path.insert(0, str(SHARED_DIR))

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
import distill_core  # noqa: E402

# The recall tests drive the real injection path, which appends to the injection ledger.
# Redirect it before import or the suite writes into the owner's live cache.
os.environ["BORING_INJECTION_LEDGER"] = str(Path(tempfile.mkdtemp()) / "injections.jsonl")
import markers  # noqa: E402
import recall_core  # noqa: E402


class ExtractJsonTests(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(distill._extract_json('{"a": 1}'), {"a": 1})

    def test_markdown_fenced(self):
        text = '```json\n{"title": "x", "body": "y"}\n```'
        self.assertEqual(distill._extract_json(text), {"title": "x", "body": "y"})

    def test_trailing_prose_ignored(self):
        # raw_decode stops at the first complete object; trailing garbage is dropped.
        text = '{"skip": true}\nHere is why I skipped it.'
        self.assertEqual(distill._extract_json(text), {"skip": True})

    def test_leading_prose_before_object(self):
        text = 'Sure! Here is the JSON:\n{"k": "v"}'
        self.assertEqual(distill._extract_json(text), {"k": "v"})

    def test_no_object_returns_none(self):
        self.assertIsNone(distill._extract_json("no json here at all"))

    def test_malformed_returns_none(self):
        self.assertIsNone(distill._extract_json('{"a": '))


class StripTrailingMetadataTests(unittest.TestCase):
    def test_strips_trailing_block(self):
        body = "## 결과\nfixed it.\n\ntags: [a, b]\ntools: [git]\nconcepts: [x]"
        self.assertEqual(distill._strip_trailing_metadata(body), "## 결과\nfixed it.")

    def test_keeps_clean_body(self):
        body = "## 배경\nproblem\n\n## 결과\nsolved"
        self.assertEqual(distill._strip_trailing_metadata(body), body.rstrip())

    def test_does_not_strip_midbody_mention(self):
        # A "tools:" line in the MIDDLE (not the trailing run) must be preserved.
        body = "intro\ntools: relevant here\nmore prose"
        self.assertEqual(distill._strip_trailing_metadata(body), body.rstrip())


class BuildPromptTests(unittest.TestCase):
    def test_contains_json_skeleton_and_transcript(self):
        prompt = distill._build_prompt("[user] hello world", "personal", "org/repo")
        self.assertIn('"title"', prompt)
        self.assertIn('"claims"', prompt)
        self.assertIn("=== SESSION TRANSCRIPT ===", prompt)
        self.assertIn("[user] hello world", prompt)

    def test_repo_and_origin_hints(self):
        with_repo = distill._build_prompt("t", "company", "org/repo")
        self.assertIn("repo='org/repo'", with_repo)
        self.assertIn("origin='company'", with_repo)
        no_repo = distill._build_prompt("t", "personal", "")
        self.assertNotIn("repo='", no_repo)
        self.assertIn("origin='personal'", no_repo)

    def test_skip_contract_present(self):
        # The prompt must teach the {"skip": true} escape hatch that distill_and_remember honors.
        self.assertIn('"skip": true', distill._build_prompt("t", "personal", ""))


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
                raise side_effect
            return context_resp

        module = _load("session_start_recall", "session-start-recall.py")
        with (
            mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(module.sys, "stdout", captured),
            mock.patch.object(module.sys, "stderr", stderr),
            mock.patch.object(module.DrudgeClient, "context", fake_context),
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

    The hook must prepend what the morning card's 「해」 judged when BORING_DOOR_URL
    points at a live door, say so on stderr and continue when the door is dead,
    and stay silent when no door URL is configured (not configuring is normal).
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
                lambda _s, project=None, max_items=5: {
                    "decisions": [],
                    "risks": [],
                    "facts": [],
                    "glossary": [],
                    "language": "ko",
                },
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

    def test_no_door_url_omits_silently(self):
        called = []

        def urlopen(url, timeout):
            called.append(url)
            return self._DoorResp({})

        out, err = self._run_with_door({}, urlopen)
        self.assertEqual(err, "", "an unconfigured door is normal, not an error")
        self.assertEqual(called, [])
        self.assertNotIn("오늘 승인한 것", out)


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

    def test_remember_failure_returns_nonzero_and_marks_retry(self):
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
                mock.patch.object(distill, "distill_and_remember", return_value=False),
                mock.patch.object(distill, "_mark") as mark,
            ):
                rc = distill.main()

            self.assertEqual(rc, 1)
            mark.assert_called_once_with("abc", retry=True, reason="remember failed")
            self.assertIn("remember failed", stderr.getvalue())
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
            search_mock = mock.MagicMock(return_value=hits)
        else:
            search_mock = mock.MagicMock(side_effect=search_side_effect)
        # The engine's handover door is a network call; it is mocked here so the tests never
        # reach it, and so a test can look at what was handed.
        handover_mock = handover or mock.MagicMock(return_value={"handed": 0, "unknown": 0})
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
        handover = mock.MagicMock(return_value={"handed": recall_core.MAX_RESULTS, "unknown": 0})
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
        handover = mock.MagicMock(side_effect=OSError("refused"))
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
            out, err = self._run_main("a sufficiently long prompt", [], search_side_effect=OSError("down"))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
