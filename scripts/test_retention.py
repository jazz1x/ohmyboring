#!/usr/bin/env python3
"""Guardrail tests for retention.plan() — the data-loss-critical decisions.

retention.py --apply archives/deletes raw session transcripts, so the planning pass must NEVER put an
undistilled (unprocessed) session on the delete path, must honor age thresholds, and must not mutate
the filesystem while planning. These tests run fully offline against tmpdirs (no ~/.claude, no docker).

Owned guardrails (what would break in production if these regress):
  1. An UNPROCESSED session is never hard-deleted — only archived — even under delete_only.
  2. delete_only deletes only PROCESSED sessions.
  3. Sessions younger than the threshold are kept (not touched).
  4. PENDING sessions are skipped entirely.
  5. Ancient-archive deletion only targets PROCESSED archives.
  6. plan() is pure — it mutates nothing on disk.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import retention  # noqa: E402

DAY = 86400


class RetentionPlanGuardrails(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src = root / "projects"
        self.mark = root / "mark"
        self.archive = self.mark / "archive"
        self.src.mkdir()
        self.mark.mkdir()
        self.archive.mkdir()
        # Redirect the module's location helpers at the tmp dirs (call-time lookup → monkeypatch works).
        self._orig = (retention._source_dirs, retention._mark_dir, retention._archive_dir)
        retention._source_dirs = lambda: [self.src]
        retention._mark_dir = lambda: self.mark
        retention._archive_dir = lambda: self.archive
        self.now = time.time()

    def tearDown(self):
        retention._source_dirs, retention._mark_dir, retention._archive_dir = self._orig
        self.tmp.cleanup()

    # --- helpers -----------------------------------------------------------
    def _session(self, sid: str, age_days: float) -> Path:
        p = self.src / f"{sid}.jsonl"
        p.write_text("{}\n", encoding="utf-8")
        ts = self.now - age_days * DAY
        os.utime(p, (ts, ts))
        return p

    def _marker(self, sid: str, suffix: str, age_days: float = 0):
        p = self.mark / f"{retention._safe(sid)}{suffix}"
        p.write_text("x", encoding="utf-8")
        ts = self.now - age_days * DAY
        os.utime(p, (ts, ts))
        return p

    def _archived(self, sid: str, age_days: float) -> Path:
        p = self.archive / f"{sid}.jsonl.gz"
        p.write_text("gz", encoding="utf-8")
        ts = self.now - age_days * DAY
        os.utime(p, (ts, ts))
        return p

    def _plan(self, delete_only=False):
        return retention.plan(
            self.now,
            processed_days=30,
            unprocessed_days=90,
            archive_days=180,
            delete_only=delete_only,
        )

    # --- guardrails --------------------------------------------------------
    def test_unprocessed_old_is_archived_never_deleted(self):
        # No marker = unprocessed. Older than the unprocessed threshold.
        self._session("u1", age_days=120)
        p = self._plan(delete_only=False)
        arch = [s.name for s, _, _ in p["to_archive"]]
        self.assertIn("u1.jsonl", arch)
        self.assertEqual(p["to_delete"], [])

    def test_unprocessed_never_deleted_even_under_delete_only(self):
        # THE critical guardrail: delete_only must NOT hard-delete an undistilled session
        # (its raw transcript is the only thing it can ever be distilled from).
        self._session("u1", age_days=120)
        p = self._plan(delete_only=True)
        self.assertEqual(p["to_delete"], [], "unprocessed session must never be on the delete path")
        self.assertIn("u1.jsonl", [s.name for s, _, _ in p["to_archive"]])

    def test_processed_old_archived_or_deleted_by_mode(self):
        self._session("p1", age_days=60)
        self._marker("p1", ".ts")  # processed
        # default: archive
        self.assertIn("p1.jsonl", [s.name for s, _, _ in self._plan(False)["to_archive"]])
        # delete_only: delete
        po = self._plan(True)
        self.assertEqual([s.name for s in po["to_delete"]], ["p1.jsonl"])
        self.assertEqual(po["to_archive"], [])

    def test_young_session_is_kept(self):
        self._session("p1", age_days=5)
        self._marker("p1", ".ts")  # processed, but < 30d
        self._session("u1", age_days=10)  # unprocessed, < 90d
        p = self._plan(delete_only=True)
        self.assertEqual(p["to_archive"], [])
        self.assertEqual(p["to_delete"], [])

    def test_pending_session_is_skipped(self):
        self._session("x1", age_days=365)
        self._marker("x1", ".pending")  # in-flight → never touched
        p = self._plan(delete_only=True)
        self.assertEqual(p["to_archive"], [])
        self.assertEqual(p["to_delete"], [])

    def test_ancient_archive_deletes_only_processed(self):
        self._archived("p1", age_days=200)
        self._marker("p1", ".ts")  # processed → ancient archive may be deleted
        self._archived("u1", age_days=200)  # unprocessed → archive is sole re-distill source, keep
        p = self._plan()
        names = [a.name for a in p["ancient_archives"]]
        self.assertIn("p1.jsonl.gz", names)
        self.assertNotIn("u1.jsonl.gz", names)

    def test_plan_does_not_mutate_filesystem(self):
        s = self._session("u1", age_days=120)
        self._session("p1", age_days=60)
        self._marker("p1", ".ts")
        a = self._archived("p2", age_days=200)
        self._marker("p2", ".ts")
        before = {p for p in self.src.rglob("*")} | {p for p in self.mark.rglob("*")}
        self._plan(delete_only=True)
        after = {p for p in self.src.rglob("*")} | {p for p in self.mark.rglob("*")}
        self.assertEqual(before, after, "plan() must be read-only")
        self.assertTrue(s.exists() and a.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
