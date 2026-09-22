#!/usr/bin/env python3
"""Plain-runnable marker reliability tests.

Run: python3 agents/shared/test_markers.py
"""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import markers


class MarkerReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.old_mark_dir = markers.MARK_DIR

    def tearDown(self):
        markers.set_mark_dir(self.old_mark_dir)

    def test_mark_done_writes_done_and_cleans_pending_retry(self):
        with tempfile.TemporaryDirectory() as d:
            markers.set_mark_dir(d)
            base = Path(d) / "s1"
            (Path(f"{base}.pending")).write_text("pending")
            (Path(f"{base}.retry")).write_text("retry")

            markers.mark_done("s1")

            self.assertTrue(Path(f"{base}.ts").exists())
            self.assertFalse(Path(f"{base}.pending").exists())
            self.assertFalse(Path(f"{base}.retry").exists())

    def test_remove_pending_allows_absent_marker(self):
        with tempfile.TemporaryDirectory() as d:
            markers.set_mark_dir(d)

            markers.remove_pending("absent")

            self.assertFalse((Path(d) / "absent.pending").exists())

    def test_marker_write_apis_raise_when_marker_dir_cannot_be_created(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "blocker"
            blocker.write_text("not a directory")
            cases = (
                ("mark_done", lambda: markers.mark_done("s1")),
                ("mark_retry", lambda: markers.mark_retry("s1")),
                ("mark_pending", lambda: markers.mark_pending("s1")),
                ("write_ingest_pending", lambda: markers.write_ingest_pending("s1", 0, 1)),
            )

            for name, call in cases:
                with self.subTest(name=name):
                    markers.set_mark_dir(str(blocker / name / "markers"))
                    with self.assertRaises(NotADirectoryError):
                        call()

    def test_mark_retry_raises_when_cleanup_remove_fails(self):
        with tempfile.TemporaryDirectory() as d:
            markers.set_mark_dir(d)
            (Path(d) / "s1.ts").write_text("done")

            with mock.patch.object(markers.Path, "unlink", side_effect=OSError("remove failed")):
                with self.assertRaisesRegex(OSError, "remove failed"):
                    markers.mark_retry("s1")
            self.assertTrue((Path(d) / "s1.retry").exists())

    def test_write_ingest_pending_cleans_done_and_retry(self):
        with tempfile.TemporaryDirectory() as d:
            markers.set_mark_dir(d)
            (Path(d) / "s1.ts").write_text("done")
            (Path(d) / "s1.retry").write_text("retry")

            markers.write_ingest_pending("s1", 7, 2)

            self.assertEqual((Path(d) / "s1.pending").read_text(), "s1\n7\n2")
            self.assertFalse((Path(d) / "s1.ts").exists())
            self.assertFalse((Path(d) / "s1.retry").exists())

    def test_retry_marker_ttl(self):
        with tempfile.TemporaryDirectory() as d:
            markers.set_mark_dir(d)
            path = Path(d) / "s1.retry"
            path.write_text("retry")

            self.assertTrue(markers.is_retry("s1", ttl=60))
            stale = time.time() - 61
            os.utime(path, (stale, stale))
            self.assertFalse(markers.is_retry("s1", ttl=60))

    def test_remove_pending_raises_when_unlink_fails(self):
        with tempfile.TemporaryDirectory() as d:
            markers.set_mark_dir(d)
            (Path(d) / "s1.pending").write_text("pending")

            with mock.patch.object(markers.Path, "unlink", side_effect=OSError("remove failed")):
                with self.assertRaisesRegex(OSError, "remove failed"):
                    markers.remove_pending("s1")

    def test_mark_retry_counts_attempts_and_is_backward_compatible_with_legacy_marker(self):
        with tempfile.TemporaryDirectory() as d:
            markers.set_mark_dir(d)
            # Legacy on-disk format: timestamp only, no attempt count (pre-dates this change).
            (Path(d) / "s1.retry").write_text(str(time.time()))

            # The legacy marker's mere existence already counts as attempt 1, so the next
            # mark_retry call must not crash on it and must resume counting from 2.
            attempts = markers.mark_retry("s1")

            self.assertEqual(attempts, 2)
            self.assertEqual(markers.retry_count("s1"), 2)
            self.assertFalse(markers.is_dead("s1"))

    def test_mark_retry_dead_letters_after_threshold_and_stops_re_queueing(self):
        with tempfile.TemporaryDirectory() as d:
            markers.set_mark_dir(d)
            with mock.patch.dict(os.environ, {"MARKER_RETRY_MAX_ATTEMPTS": "3"}):
                self.assertEqual(markers.mark_retry("s1"), 1)
                self.assertFalse(markers.is_dead("s1"))
                self.assertEqual(markers.mark_retry("s1"), 2)
                self.assertFalse(markers.is_dead("s1"))
                self.assertEqual(markers.mark_retry("s1"), 3)
            self.assertTrue(markers.is_dead("s1"))
            self.assertFalse((Path(d) / "s1.retry").exists())

    def test_mark_retry_dead_letter_records_reason_and_attempts(self):
        with tempfile.TemporaryDirectory() as d:
            markers.set_mark_dir(d)
            with mock.patch.dict(os.environ, {"MARKER_RETRY_MAX_ATTEMPTS": "1"}):
                markers.mark_retry("s1", reason="engine down")

            content = (Path(d) / "s1.dead").read_text()
            lines = content.splitlines()
            self.assertEqual(lines[1], "1")
            self.assertEqual(lines[2], "engine down")

    def test_mark_done_and_mark_pending_clear_dead_marker(self):
        with tempfile.TemporaryDirectory() as d:
            markers.set_mark_dir(d)
            (Path(d) / "s1.dead").write_text("dead")
            markers.mark_done("s1")
            self.assertFalse((Path(d) / "s1.dead").exists())

            (Path(d) / "s2.dead").write_text("dead")
            markers.mark_pending("s2")
            self.assertFalse((Path(d) / "s2.dead").exists())

    def test_is_dead_false_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            markers.set_mark_dir(d)
            self.assertFalse(markers.is_dead("absent"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
