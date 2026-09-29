import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http import server
from pathlib import Path
from unittest import mock

# Load the module under test (ingest-worker.py) under a Python-valid name.
_ingest_worker_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ingest-worker.py")
spec = importlib.util.spec_from_file_location("ingest_worker", _ingest_worker_path)
ingest_worker = importlib.util.module_from_spec(spec)
sys.modules["ingest_worker"] = ingest_worker
spec.loader.exec_module(ingest_worker)
Ok, Err = ingest_worker.Ok, ingest_worker.Err


class _FakeEngine(server.BaseHTTPRequestHandler):
    """Tiny HTTP server that mimics the ohmyboring engine for tests."""

    vector = False
    total_chunks = 0

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"status": "ok", "vector": self.vector}).encode()
        elif self.path == "/audit":
            body = json.dumps({"total_chunks": self.total_chunks}).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ingest_worker.MARK_DIR = self.tmp.name
        ingest_worker.markers.set_mark_dir(self.tmp.name)

        # BORING_VAULT_DIR is the vault root; notes live under vault/wiki.
        self.vault_root = Path(self.tmp.name) / "vault"
        self.wiki_dir = self.vault_root / "wiki"
        self.wiki_dir.mkdir(parents=True)
        self._orig_vault_dir = os.environ.get("BORING_VAULT_DIR")
        self._orig_event_log = os.environ.get("BORING_EVENT_LOG")
        self._orig_event_sink = os.environ.get("BORING_EVENT_SINK")
        os.environ["BORING_VAULT_DIR"] = str(self.vault_root)
        os.environ["BORING_EVENT_LOG"] = str(Path(self.tmp.name) / "events.ndjson")
        os.environ["BORING_EVENT_SINK"] = "spool"

        self.engine = server.HTTPServer(("127.0.0.1", 0), _FakeEngine)
        self.thread = threading.Thread(target=self.engine.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.engine.shutdown)
        self.addCleanup(self.engine.server_close)
        self.addCleanup(self._restore_vault_dir)

        port = self.engine.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        ingest_worker.BORING_URL = self.url

    def _restore_vault_dir(self):
        if self._orig_vault_dir is None:
            os.environ.pop("BORING_VAULT_DIR", None)
        else:
            os.environ["BORING_VAULT_DIR"] = self._orig_vault_dir
        if self._orig_event_log is None:
            os.environ.pop("BORING_EVENT_LOG", None)
        else:
            os.environ["BORING_EVENT_LOG"] = self._orig_event_log
        if self._orig_event_sink is None:
            os.environ.pop("BORING_EVENT_SINK", None)
        else:
            os.environ["BORING_EVENT_SINK"] = self._orig_event_sink

    def _pending(self, sid, before, attempts=0):
        path = Path(self.tmp.name) / f"{sid}.pending"
        path.write_text(f"{sid}\n{before}\n{attempts}\n")

    def _read_attempts(self, sid):
        path = Path(self.tmp.name) / f"{sid}.pending"
        if not path.exists():
            return None
        parts = path.read_text().strip().split("\n")
        return int(parts[2]) if len(parts) > 2 else 0

    def _last_event(self):
        event_path = Path(os.environ["BORING_EVENT_LOG"])
        return json.loads(event_path.read_text(encoding="utf-8").splitlines()[-1])

    def _done_exists(self, sid):
        return (Path(self.tmp.name) / f"{sid}.ts").exists()

    def _retry_exists(self, sid):
        return (Path(self.tmp.name) / f"{sid}.retry").exists()

    def _write_note(self, sid, wiki_id="wiki-9999"):
        note = self.wiki_dir / f"{wiki_id}.md"
        note.write_text(f"---\ntitle: test\nomb_session_id: {sid}\n---\nbody\n")

    def test_frontmatter_session_id_parsing(self):
        self._write_note("s-parse", "wiki-0001")
        self.assertEqual(
            ingest_worker._frontmatter_session_id(self.wiki_dir / "wiki-0001.md"),
            "s-parse",
        )

    def test_find_session_note_finds_marker(self):
        self._write_note("s-marker")
        found = ingest_worker._find_session_note("s-marker")
        self.assertEqual(Path(found), self.wiki_dir / "wiki-9999.md")

    def test_find_session_note_uses_vault_wiki_not_vault_root(self):
        root_note = self.vault_root / "wiki-0001.md"
        root_note.write_text("---\ntitle: wrong\nomb_session_id: s-root\n---\nbody\n")
        self._write_note("s-root", "wiki-0002")

        found = ingest_worker._find_session_note("s-root")

        self.assertEqual(Path(found), self.wiki_dir / "wiki-0002.md")

    def test_find_session_note_none_without_marker(self):
        note = self.wiki_dir / "wiki-0001.md"
        note.write_text("---\ntitle: other\n---\nbody\n")
        self.assertIsNone(ingest_worker._find_session_note("s-other"))

    def test_vector_mode_prefers_session_marker_over_chunk_count(self):
        _FakeEngine.vector = True
        _FakeEngine.total_chunks = 0
        self._pending("s1", 5)
        self._write_note("s1")
        ingest_worker._reconcile()
        self.assertTrue(self._done_exists("s1"))
        event = self._last_event()
        self.assertEqual(event["event"], "ingest_reconcile")
        self.assertEqual(event["workflow_node"], "done_marked")
        self.assertEqual(event["workflow_outcome"], "continue")

    def test_vector_mode_falls_back_to_chunk_count(self):
        _FakeEngine.vector = True
        _FakeEngine.total_chunks = 10
        self._pending("s2", 5)
        ingest_worker._reconcile()
        self.assertTrue(self._done_exists("s2"))

    def test_wiki_mode_uses_session_marker(self):
        _FakeEngine.vector = False
        _FakeEngine.total_chunks = 0
        self._pending("s3", 0)
        self._write_note("s3")
        ingest_worker._reconcile()
        self.assertTrue(self._done_exists("s3"))

    def test_wiki_mode_increments_attempts_without_marker(self):
        _FakeEngine.vector = False
        _FakeEngine.total_chunks = 0
        self._pending("s4", 0, attempts=0)
        ingest_worker._reconcile()
        self.assertFalse(self._done_exists("s4"))
        self.assertEqual(self._read_attempts("s4"), 1)

    def test_wiki_mode_moves_pending_to_retry_after_max_attempts(self):
        _FakeEngine.vector = False
        _FakeEngine.total_chunks = 0
        self._pending("s5", 0, attempts=ingest_worker.MAX_WIKI_ATTEMPTS)
        ingest_worker._reconcile()
        self.assertFalse(self._done_exists("s5"))
        self.assertTrue(self._retry_exists("s5"))
        self.assertIsNone(self._read_attempts("s5"))
        event = self._last_event()
        self.assertEqual(event["event"], "ingest_reconcile")
        self.assertEqual(event["workflow_node"], "retry_marked")
        self.assertEqual(event["workflow_outcome"], "fail")

    def test_unknown_worker_projection_raises(self):
        with self.assertRaises(ValueError):
            ingest_worker._log_worker_event("unknown", "ok")

    def test_fresh_retry_marker_is_not_reoffered(self):
        retry = Path(self.tmp.name) / "s-retry.retry"
        retry.write_text("0")
        session = Path(self.tmp.name) / "s-retry.jsonl"
        session.write_text("{}\n")

        self.assertFalse(ingest_worker._eligible(str(session)))

    def test_stale_retry_marker_is_reoffered(self):
        retry = Path(self.tmp.name) / "s-retry.retry"
        retry.write_text("0")
        stale = time.time() - ingest_worker.RETRY_TTL - 1
        os.utime(retry, (stale, stale))
        session = Path(self.tmp.name) / "s-retry.jsonl"
        session.write_text("{}\n")
        stable = time.time() - ingest_worker.STABLE_AGE_S - 1
        os.utime(session, (stable, stable))

        self.assertTrue(ingest_worker._eligible(str(session)))

    def test_health_failure_treated_as_wiki_and_retries(self):
        ingest_worker.BORING_URL = "http://127.0.0.1:1"  # unreachable
        self._pending("s6", 0)
        ingest_worker._reconcile()
        # Unreachable engine falls back to wiki-first → attempts incremented, not done yet.
        self.assertFalse(self._done_exists("s6"))
        self.assertEqual(self._read_attempts("s6"), 1)

    def _queue(self, sid):
        distill_queue = ingest_worker.distill_queue
        distill_queue.enqueue(distill_queue.QueueItem(sid, "claude-code", "personal", "repo", "text"))

    def _drain_with(self, result=None, reachable=None):
        reachable = reachable or Ok(None)
        with (
            mock.patch.object(ingest_worker, "_reachable", return_value=reachable),
            mock.patch.object(ingest_worker.distill_run, "distill_and_remember", return_value=result) as run,
        ):
            ingest_worker._drain_queue()
        return run

    def _events(self):
        lines = Path(os.environ["BORING_EVENT_LOG"]).read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def test_a_raising_item_costs_one_try_and_does_not_stop_the_next(self):
        self._queue("q-a-boom")
        self._queue("q-b-good")

        def graph(text, origin, repo, sid):
            if sid == "q-a-boom":
                raise TypeError("expected string or bytes-like object, got 'int'")
            return True

        with (
            mock.patch.object(ingest_worker, "_reachable", return_value=Ok(None)),
            mock.patch.object(ingest_worker.distill_run, "distill_and_remember", side_effect=graph),
        ):
            ingest_worker._drain_queue()

        self.assertEqual(ingest_worker.markers.retry_count("q-a-boom"), 1)
        self.assertTrue(self._done_exists("q-b-good"))
        failed = [e for e in self._events() if e.get("session_id") == "q-a-boom"][-1]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("TypeError", failed["reason"])

    def test_a_session_that_keeps_failing_is_dead_on_the_fifth_try_and_is_not_requeued(self):
        for _ in range(7):
            self._queue("q-loop")
            self._drain_with(False)
        self.assertTrue(ingest_worker.markers.is_dead("q-loop"))
        self.assertFalse(ingest_worker.distill_queue.is_queued("q-loop"))
        runs = [e for e in self._events() if e.get("session_id") == "q-loop"]
        self.assertEqual([e["status"] for e in runs], ["failed"] * 4 + ["dead"])

    def test_an_already_done_session_is_not_distilled_again(self):
        self._queue("q-done")
        ingest_worker.markers.mark_done("q-done")
        run = self._drain_with(True)
        run.assert_not_called()
        self.assertFalse(ingest_worker.distill_queue.is_queued("q-done"))
        self.assertEqual(self._last_event()["status"], "skipped")
        self.assertEqual(self._last_event()["reason"], "already_done")

    def test_an_unreachable_backend_defers_without_spending_a_try(self):
        self._queue("q-down")
        ingest_worker.markers.mark_retry("q-down", reason="earlier")
        run = self._drain_with(True, reachable=Err("llm down"))
        run.assert_not_called()
        self.assertEqual(ingest_worker.markers.retry_count("q-down"), 1)
        self.assertTrue(ingest_worker.distill_queue.is_queued("q-down"))
        self.assertEqual(self._last_event()["status"], "deferred")
        self.assertIn("llm down", self._last_event()["reason"])

    def test_drain_only_prints_nothing_to_stdout(self):
        self._queue("q-now")
        out = io.StringIO()
        with (
            mock.patch.object(ingest_worker, "_reachable", return_value=Ok(None)),
            mock.patch.object(ingest_worker.distill_run, "distill_and_remember", return_value=True),
            mock.patch.object(ingest_worker.sys, "stdout", out),
        ):
            ingest_worker.main(["--drain-only"])
        self.assertEqual(out.getvalue(), "")
        self.assertTrue(self._done_exists("q-now"))

    def test_queue_success_marks_done_and_removes_file(self):
        self._queue("q-ok")
        run = self._drain_with(True)
        run.assert_called_once_with("text", "personal", "repo", "q-ok")
        self.assertTrue(self._done_exists("q-ok"))
        self.assertFalse(ingest_worker.distill_queue.is_queued("q-ok"))
        self.assertEqual(self._last_event()["status"], "ok")
        self.assertEqual(self._last_event()["event"], "ingest_queue")

    def test_queue_failure_marks_retry_and_keeps_file(self):
        self._queue("q-fail")
        self._drain_with(False)
        self.assertTrue(self._retry_exists("q-fail"))
        self.assertTrue(ingest_worker.distill_queue.is_queued("q-fail"))
        self.assertEqual(self._last_event()["status"], "failed")

    def test_queue_dead_removes_file(self):
        self._queue("q-dead")
        with mock.patch.dict(os.environ, {"MARKER_RETRY_MAX_ATTEMPTS": "1"}):
            self._drain_with(False)
        self.assertTrue(ingest_worker.markers.is_dead("q-dead"))
        self.assertFalse(ingest_worker.distill_queue.is_queued("q-dead"))
        self.assertEqual(self._last_event()["status"], "dead")

    def test_reconcile_leaves_queued_pending_marker_alone(self):
        self._queue("q-pend")
        ingest_worker._reconcile()
        self.assertTrue(ingest_worker.markers.is_pending("q-pend"))
        session = Path(self.tmp.name) / "q-pend.jsonl"
        session.write_text("{}\n")
        self.assertFalse(ingest_worker._eligible(str(session)))


class BackstopClampTests(unittest.TestCase):
    """The backstop is the last-resort bound for a caller that forgot to clamp.

    It used to truncate silently with its own head/tail split, so raising a caller's clamp
    above it looked like it worked while the extra text was cut back here under a different
    rule. Both properties are pinned: it must announce itself, and it must use the shared
    truncation algorithm rather than a second one that disagrees.
    """

    def _run(self, text):
        stderr = io.StringIO()
        seen = {}

        def fake_llm(prompt):
            seen["prompt"] = prompt
            return None  # stop right after the clamp; nothing downstream is under test

        item = ingest_worker.distill_queue.QueueItem("s-backstop", "claude-code", "personal", "repo", text)
        with (
            mock.patch.object(ingest_worker.llm, "call_llm", fake_llm),
            mock.patch.object(ingest_worker.sys, "stderr", stderr),
        ):
            ingest_worker._distill(item)
        return seen.get("prompt", ""), stderr.getvalue()

    def test_backstop_sits_above_every_caller_default(self):
        # The previous backstop equalled the caller defaults, so raising a caller's clamp was
        # silently undone here. A guard at the same value as the knob it guards is a hidden knob.
        transcript = ingest_worker.transcript
        for accessor in (
            transcript.claude_distill_clamp,
            transcript.codex_distill_clamp,
            transcript.kimi_distill_clamp,
        ):
            self.assertGreater(ingest_worker.BACKSTOP_CLAMP, accessor())

    def test_over_backstop_is_cut_and_announced(self):
        text = "\n".join(f"line {i} " + "x" * 60 for i in range(4000))
        self.assertGreater(len(text), ingest_worker.BACKSTOP_CLAMP)
        _prompt, err = self._run(text)
        self.assertIn("backstop", err)
        self.assertIn("unclamped", err)

    def test_under_backstop_is_untouched_and_silent(self):
        text = "short enough\n" * 10
        _prompt, err = self._run(text)
        self.assertNotIn("backstop", err)

    def test_backstop_uses_the_shared_truncator(self):
        # clamp_text snaps to newline boundaries; the old inline split did not. If the backstop
        # ever goes back to slicing by index this lands mid-line and the assertion fails.
        text = "\n".join("y" * 200 for _ in range(1200))
        prompt, _err = self._run(text)
        body = prompt[prompt.find("y" * 200) :]
        self.assertNotIn("y" * 201, body)


if __name__ == "__main__":
    unittest.main()
