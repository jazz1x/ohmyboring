import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http import server
from pathlib import Path

# Load the module under test (ingest-worker.py) under a Python-valid name.
_ingest_worker_path = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "ingest-worker.py"
)
spec = importlib.util.spec_from_file_location("ingest_worker", _ingest_worker_path)
ingest_worker = importlib.util.module_from_spec(spec)
sys.modules["ingest_worker"] = ingest_worker
spec.loader.exec_module(ingest_worker)


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
        os.environ["BORING_VAULT_DIR"] = str(self.vault_root)
        os.environ["BORING_EVENT_LOG"] = str(Path(self.tmp.name) / "events.ndjson")

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

    def _pending(self, sid, before, attempts=0):
        path = Path(self.tmp.name) / f"{sid}.pending"
        path.write_text(f"{sid}\n{before}\n{attempts}\n")

    def _read_attempts(self, sid):
        path = Path(self.tmp.name) / f"{sid}.pending"
        if not path.exists():
            return None
        parts = path.read_text().strip().split("\n")
        return int(parts[2]) if len(parts) > 2 else 0

    def _done_exists(self, sid):
        return (Path(self.tmp.name) / f"{sid}.ts").exists()

    def _retry_exists(self, sid):
        return (Path(self.tmp.name) / f"{sid}.retry").exists()

    def _write_note(self, sid, wiki_id="wiki-9999"):
        note = self.wiki_dir / f"{wiki_id}.md"
        note.write_text(
            f"---\ntitle: test\nomb_session_id: {sid}\n---\nbody\n"
        )

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
        root_note.write_text(
            "---\ntitle: wrong\nomb_session_id: s-root\n---\nbody\n"
        )
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

        self.assertTrue(ingest_worker._eligible(str(session)))

    def test_health_failure_treated_as_wiki_and_retries(self):
        ingest_worker.BORING_URL = "http://127.0.0.1:1"  # unreachable
        self._pending("s6", 0)
        ingest_worker._reconcile()
        # Unreachable engine falls back to wiki-first → attempts incremented, not done yet.
        self.assertFalse(self._done_exists("s6"))
        self.assertEqual(self._read_attempts("s6"), 1)


if __name__ == "__main__":
    unittest.main()
