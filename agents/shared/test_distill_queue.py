#!/usr/bin/env python3
"""Run: python3 agents/shared/test_distill_queue.py"""

import json
import os
import tempfile
import unittest
from unittest import mock

import distill_queue
import markers


def _item(sid, at, text="t"):
    return distill_queue.QueueItem(sid, "claude-code", "personal", "repo", text, enqueued_at=at)


class DistillQueueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(markers, "MARK_DIR", tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = tmp.name

    def test_enqueue_writes_one_json_and_marks_pending(self):
        distill_queue.enqueue(_item("s1", 1.0, "본문"))
        files = os.listdir(distill_queue.queue_dir())
        self.assertEqual(files, ["s1.json"])
        with open(os.path.join(distill_queue.queue_dir(), "s1.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["text"], "본문")
        self.assertTrue(markers.is_pending("s1"))

    def test_same_session_overwrites(self):
        distill_queue.enqueue(_item("s1", 1.0, "old"))
        distill_queue.enqueue(_item("s1", 2.0, "new"))
        self.assertEqual([i.text for i in distill_queue.drain()], ["new"])

    def test_drain_is_oldest_first_and_limited(self):
        distill_queue.enqueue(_item("b", 2.0))
        distill_queue.enqueue(_item("a", 1.0))
        distill_queue.enqueue(_item("c", 3.0))
        self.assertEqual([i.session_id for i in distill_queue.drain()], ["a", "b", "c"])
        self.assertEqual([i.session_id for i in distill_queue.drain(2)], ["a", "b"])

    def test_enqueue_keeps_the_attempt_count(self):
        distill_queue.enqueue(_item("s1", 1.0))
        markers.mark_retry("s1", reason="x")
        markers.mark_retry("s1", reason="x")
        distill_queue.enqueue(_item("s1", 2.0))
        self.assertEqual(markers.retry_count("s1"), 2)
        self.assertTrue(markers.is_pending("s1"))

    def test_a_dead_session_is_not_queued_again(self):
        with mock.patch.dict(os.environ, {"MARKER_RETRY_MAX_ATTEMPTS": "1"}):
            markers.mark_retry("s1", reason="x")
        distill_queue.enqueue(_item("s1", 1.0))
        self.assertFalse(distill_queue.is_queued("s1"))
        self.assertTrue(markers.is_dead("s1"))

    def test_a_failed_write_leaves_no_temp_file_and_a_stray_one_is_ignored(self):
        distill_queue.enqueue(_item("ok", 1.0))
        with mock.patch.object(distill_queue.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                distill_queue.enqueue(_item("s2", 2.0))
        with open(os.path.join(distill_queue.queue_dir(), "stray.tmp"), "w", encoding="utf-8") as f:
            f.write("{half")
        self.assertEqual(sorted(os.listdir(distill_queue.queue_dir())), ["ok.json", "stray.tmp"])
        self.assertEqual([i.session_id for i in distill_queue.drain()], ["ok"])

    def test_the_same_session_written_twice_uses_two_temp_names(self):
        sources = []

        with mock.patch.object(distill_queue.os, "replace", side_effect=lambda src, dst: sources.append(src)):
            distill_queue.enqueue(_item("s1", 1.0))
            distill_queue.enqueue(_item("s1", 2.0))
        self.assertEqual(len(set(sources)), 2)

    def test_unreadable_file_is_set_aside(self):
        distill_queue.enqueue(_item("ok", 1.0))
        bad = os.path.join(distill_queue.queue_dir(), "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual([i.session_id for i in distill_queue.drain()], ["ok"])
        self.assertTrue(os.path.exists(bad + ".bad"))
        self.assertFalse(os.path.exists(bad))


if __name__ == "__main__":
    unittest.main()
