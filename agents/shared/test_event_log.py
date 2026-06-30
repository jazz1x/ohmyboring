#!/usr/bin/env python3
"""Network-free tests for append-only local event logs.

Run: python3 agents/shared/test_event_log.py
"""
import json
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

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
        self.assertEqual(event["run_id"], "s1")
        self.assertIn("ts", event)

    def test_record_cli_coerces_fields_and_tail_filters(self):
        with mock.patch.object(
            event_log.sys,
            "argv",
            [
                "event_log.py",
                "--record",
                "guard",
                "guard",
                "ok",
                "--field",
                "duration_s=12",
                "--field",
                "strict=true",
            ],
        ):
            self.assertEqual(event_log.main(), 0)

        stdout = io.StringIO()
        with (
            mock.patch.object(event_log.sys, "argv", ["event_log.py", "--tail", "--component", "guard", "--json"]),
            mock.patch.object(event_log.sys, "stdout", stdout),
        ):
            self.assertEqual(event_log.main(), 0)

        lines = stdout.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual(event["component"], "guard")
        self.assertEqual(event["duration_s"], 12)
        self.assertEqual(event["strict"], True)

    def test_try_append_event_returns_false_on_write_failure(self):
        with mock.patch.object(event_log, "append_event", side_effect=OSError("denied")):
            self.assertFalse(event_log.try_append_event("guard", "guard", "failed"))

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

    def test_recent_resolution_failures_ignores_resolved_session(self):
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "failed",
            session_id="resolved",
            resolution="evidence",
            verifier_status="failed",
            missing_fields=["claim-kind:decision"],
        )
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "ok",
            session_id="resolved",
            resolution="evidence",
            verifier_status="pass",
            remember_status="duplicate",
        )
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "failed",
            session_id="bad",
            resolution="evidence",
            verifier_status="failed",
        )

        failures = event_log.recent_resolution_failures()

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["session_id"], "bad")

    def test_recent_resolution_failures_keeps_latest_failure_after_success(self):
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "ok",
            session_id="regressed",
            resolution="evidence",
            verifier_status="pass",
        )
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "failed",
            session_id="regressed",
            resolution="evidence",
            verifier_status="failed",
        )

        failures = event_log.recent_resolution_failures()

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["session_id"], "regressed")

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
