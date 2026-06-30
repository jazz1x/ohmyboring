#!/usr/bin/env python3
"""Network-free tests for append-only local event logs.

Run: python3 agents/shared/test_event_log.py
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import event_log


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.old_event_log = os.environ.get("BORING_EVENT_LOG")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["BORING_EVENT_LOG"] = os.path.join(self.tmp.name, "events.ndjson")

    def tearDown(self):
        if self.old_event_log is None:
            os.environ.pop("BORING_EVENT_LOG", None)
        else:
            os.environ["BORING_EVENT_LOG"] = self.old_event_log
        self.tmp.cleanup()

    def test_append_event_writes_one_ndjson_line(self):
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "ok",
            session_id="s1",
            verifier_status="pass",
        )

        with open(os.environ["BORING_EVENT_LOG"], encoding="utf-8") as f:
            event = json.loads(f.readline())

        self.assertEqual(event["component"], "distill-session")
        self.assertEqual(event["event"], "distill_resolution")
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["session_id"], "s1")
        self.assertIn("ts", event)

    def test_recent_resolution_failures_filters_resolution_failures(self):
        event_log.append_event("distill-session", "distill_resolution", "ok", session_id="pass")
        event_log.append_event("guard", "guard", "failed", session_id="other")
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "failed",
            session_id="bad",
            verifier_status="failed",
            missing_fields=["section:evidence"],
        )

        failures = event_log.recent_resolution_failures()

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["session_id"], "bad")

    def test_recent_resolution_failures_ignores_malformed_lines(self):
        with open(os.environ["BORING_EVENT_LOG"], "w", encoding="utf-8") as f:
            f.write("{bad json\n")
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "failed",
            session_id="bad",
            verifier_status="failed",
        )

        failures = event_log.recent_resolution_failures()

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["session_id"], "bad")

    def test_recent_resolution_failures_ignores_stale_failures(self):
        old = datetime.now(timezone.utc) - timedelta(hours=48)
        stale = {
            "ts": old.isoformat(),
            "component": "distill-session",
            "event": "distill_resolution",
            "status": "failed",
            "session_id": "old",
            "verifier_status": "failed",
        }
        with open(os.environ["BORING_EVENT_LOG"], "w", encoding="utf-8") as f:
            f.write(json.dumps(stale))
            f.write("\n")

        failures = event_log.recent_resolution_failures(hours=24)

        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
