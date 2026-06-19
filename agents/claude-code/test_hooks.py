#!/usr/bin/env python3
"""Network-free regression tests for the Claude Code hook helpers.

Run: python3 agents/claude-code/test_hooks.py   (no pytest dependency; unittest-based)
 or: python3 -m pytest agents/claude-code/test_hooks.py

Covers the PURE, no-network helpers in distill-session.py and recall.py:
  - distill-session._extract_json         — LLM-JSON extraction (fences + trailing prose)
  - distill-session._strip_trailing_metadata — drop trailing tags/tools/concepts blocks
  - distill-session._build_prompt         — prompt assembly (JSON skeleton + transcript)
  - distill-session._mark_path            — throttle-marker path/id sanitization
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
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
# Import boring_config the way the hooks do (insert agents/shared on sys.path). distill-session
# evaluates NOTE_LANG = boring_config.note_lang() at import time, so the dep must resolve.
SHARED_DIR = HERE.parent / "shared"
sys.path.insert(0, str(SHARED_DIR))

# Neutralize ambient policy/endpoint env so module-load + assertions are deterministic.
for _var in ("BORING_CONFIG", "OMB_HOME", "DRUDGE_URL", "RECALL_MAX_RESULTS",
             "RECALL_MAX_TOKENS", "RECALL_TIMEOUT", "RECALL_RETRIES"):
    os.environ.pop(_var, None)


def _load(name, filename):
    """Load a hook module by file path (handles the hyphenated filename)."""
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


distill = _load("distill_session_hook", "distill-session.py")
recall = _load("recall_hook", "recall.py")


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
        p = distill._mark_path("../../etc/passwd")
        self.assertEqual(Path(p).name, "etcpasswd.ts")  # slashes/dots stripped, no traversal

    def test_empty_session_fallback(self):
        self.assertEqual(Path(distill._mark_path("")).name, "nosession.ts")

    def test_preserves_safe_chars(self):
        self.assertEqual(Path(distill._mark_path("abc-123_XY")).name, "abc-123_XY.ts")


class RepoSlugTests(unittest.TestCase):
    def test_folder_fallback_when_no_git(self):
        # An empty cwd has no git remote → folder-name fallback; "" cwd → "".
        self.assertEqual(distill.repo_slug(""), "")

    def test_basename_of_cwd(self):
        # repo_slug calls git first; for a non-repo temp dir the remote is "" → folder name.
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "my-project")
            os.makedirs(sub)
            self.assertEqual(distill.repo_slug(sub), "my-project")


class ExtractTranscriptTests(unittest.TestCase):
    def test_extracts_user_and_assistant_text(self):
        lines = [
            {"message": {"role": "user", "content": "hi there"}},
            {"message": {"role": "assistant",
                         "content": [{"type": "text", "text": "hello"},
                                     {"type": "tool_use", "name": "x"}]}},
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


class RecallFormattingTests(unittest.TestCase):
    """recall.main reads stdin + posts to /search; we mock urlopen so NO network happens."""

    def _run_main(self, prompt, hits):
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
        with mock.patch.object(recall.sys, "stdin", io.StringIO(json.dumps({"prompt": prompt}))), \
             mock.patch.object(recall.urllib.request, "urlopen",
                               return_value=_Resp({"hits": hits})), \
             mock.patch.object(recall.sys, "stdout", captured):
            recall.main()
        return captured.getvalue()

    def test_short_prompt_is_noop(self):
        # < 8 chars → recall is meaningless → no output, urlopen never reached.
        self.assertEqual(self._run_main("hi", [{"source_path": "x", "snippet": "y"}]), "")

    def test_formats_context_block(self):
        out = self._run_main(
            "how did I fix the docker cache issue",
            [{"source_path": "vault/wiki/wiki-0007.md", "snippet": "fixed   the\ncache"}],
        )
        payload = json.loads(out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("- [wiki-0007.md] fixed the cache", ctx)  # basename + whitespace collapsed
        self.assertIn("self-augmenting RAG recall", ctx)        # the injection fence is present

    def test_empty_hits_noop(self):
        self.assertEqual(self._run_main("a sufficiently long prompt", []), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
