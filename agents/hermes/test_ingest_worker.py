import importlib.util
import json
import os
import sys
import tempfile
import threading
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
            body = json.dumps(
                {"status": "ok", "vector": self.vector}
            ).encode()
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

        self.engine = server.HTTPServer(("127.0.0.1", 0), _FakeEngine)
        self.thread = threading.Thread(target=self.engine.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.engine.shutdown)
        self.addCleanup(self.engine.server_close)

        port = self.engine.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        ingest_worker.DRUDGE_URL = self.url

    def _pending(self, sid, before):
        path = Path(self.tmp.name) / f"{sid}.pending"
        path.write_text(f"{sid}\n{before}\n")

    def _done_exists(self, sid):
        return (Path(self.tmp.name) / f"{sid}.ts").exists()

    def test_wiki_mode_promotes_pending_to_done(self):
        _FakeEngine.vector = False
        _FakeEngine.total_chunks = 0
        self._pending("s1", 0)
        ingest_worker._reconcile()
        self.assertTrue(self._done_exists("s1"))

    def test_vector_mode_waits_for_chunk_increase(self):
        _FakeEngine.vector = True
        _FakeEngine.total_chunks = 0
        self._pending("s2", 5)
        ingest_worker._reconcile()
        self.assertFalse(self._done_exists("s2"))

    def test_vector_mode_promotes_on_chunk_increase(self):
        _FakeEngine.vector = True
        _FakeEngine.total_chunks = 10
        self._pending("s3", 5)
        ingest_worker._reconcile()
        self.assertTrue(self._done_exists("s3"))

    def test_health_failure_treated_as_wiki(self):
        ingest_worker.DRUDGE_URL = "http://127.0.0.1:1"  # unreachable
        self._pending("s4", 0)
        ingest_worker._reconcile()
        self.assertTrue(self._done_exists("s4"))


if __name__ == "__main__":
    unittest.main()
