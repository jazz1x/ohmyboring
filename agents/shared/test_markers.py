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
import repo_table


class MarkerReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.old_mark_dir = markers.MARK_DIR

    def tearDown(self):
        markers.set_mark_dir(self.old_mark_dir)

    def test_repo_table_roundtrip_overwrite_and_absence(self):
        with tempfile.TemporaryDirectory() as d:
            markers.set_mark_dir(d)
            self.assertIsNone(repo_table.read("/Users/a/x"))
            self.assertFalse(repo_table.has("/Users/a/x"))
            repo_table.write("/Users/a/x", "company", "ds-governance")
            self.assertTrue(repo_table.has("/Users/a/x"))
            self.assertEqual(repo_table.read("/Users/a/x"), ("company", "ds-governance"))
            self.assertIsNone(repo_table.read("/Users/a/y"))  # control: another cwd is another file
            repo_table.write("/Users/a/x", "personal", "ohmyboring")  # the newest value wins
            self.assertEqual(repo_table.read("/Users/a/x"), ("personal", "ohmyboring"))
            self.assertEqual([p for p in os.listdir(d) if p.endswith(".tmp")], [])

    def test_read_nearest_exact_prefix_longest_boundary_and_sibling(self):
        with tempfile.TemporaryDirectory() as d:
            markers.set_mark_dir(d)
            repo_table.write("/u/a/b", "personal", "root")
            repo_table.write("/u/a/b/sub", "personal", "inner")
            repo_table.write("/u/a/bc", "company", "other")
            self.assertEqual(repo_table.read_nearest("/u/a/b"), ("personal", "root"))  # exact
            self.assertEqual(
                repo_table.read_nearest("/u/a/b/.claude/worktrees/agent-1"), ("personal", "root")
            )  # prefix
            self.assertEqual(repo_table.read_nearest("/u/a/b/sub/deep"), ("personal", "inner"))
            self.assertEqual(repo_table.read_nearest("/u/a/bc/x"), ("company", "other"))
            self.assertIsNone(repo_table.read_nearest("/u/a/bd/x"))  # boundary control
            self.assertIsNone(repo_table.read_nearest("/u/a/b.worktrees/c21"))  # sibling folder
            self.assertIsNone(repo_table.read_nearest(""))

    def test_record_repo_from_worktree_also_writes_main_root(self):
        import subprocess

        import distill_core

        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as tbl:
            markers.set_mark_dir(tbl)
            main = os.path.realpath(os.path.join(d, "proj"))
            wt = os.path.join(main, ".claude", "worktrees", "agent-1")
            git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
            os.makedirs(main)
            subprocess.run([*git, "-C", main, "init", "-q"], check=True)
            subprocess.run([*git, "-C", main, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
            subprocess.run([*git, "-C", main, "worktree", "add", "-q", wt], check=True)
            wt = os.path.realpath(wt)
            origin, repo = distill_core.record_repo(wt)
            self.assertEqual(repo_table.read(wt), (origin, repo))
            self.assertEqual(repo_table.read(main), (origin, repo))
            before = sorted(os.listdir(tbl))
            distill_core.record_repo(main)  # control: root == cwd writes one entry, no extra
            self.assertEqual(sorted(os.listdir(tbl)), before)

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

    def test_marker_write_apis_raise_when_marker_dir_cannot_be_created(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "blocker"
            blocker.write_text("not a directory")
            cases = (
                ("mark_done", lambda: markers.mark_done("s1")),
                ("mark_retry", lambda: markers.mark_retry("s1")),
                ("mark_pending", lambda: markers.mark_pending("s1")),
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

    def test_retry_marker_ttl(self):
        with tempfile.TemporaryDirectory() as d:
            markers.set_mark_dir(d)
            path = Path(d) / "s1.retry"
            path.write_text("retry")

            self.assertTrue(markers.is_retry("s1", ttl=60))
            stale = time.time() - 61
            os.utime(path, (stale, stale))
            self.assertFalse(markers.is_retry("s1", ttl=60))

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
