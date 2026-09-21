#!/usr/bin/env python3
"""Tests for usage.py — the three ways a token meter silently lies.

Run: python3 scripts/test_usage.py   (no pytest dependency)

A meter that is merely wrong is worse than no meter, because a number gets quoted. These pin the
three miscounts that produce a plausible total: charging a retry twice, pooling fan-out cost into
the conversation's own, and reporting a stale cache as current.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["BORING_EVENT_SINK"] = "spool"
_spec = importlib.util.spec_from_file_location("usage", HERE / "usage.py")
usage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(usage)


def _row(request_id, output, sidechain=False, day="2026-09-02"):
    return {
        "type": "assistant",
        "requestId": request_id,
        "timestamp": f"{day}T00:00:00Z",
        "cwd": "/nowhere",
        "isSidechain": sidechain,
        "message": {"model": "claude-opus-5", "usage": {"input_tokens": 1, "output_tokens": output}},
    }


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


class Counting(unittest.TestCase):
    def test_a_retried_request_is_one_bill(self):
        """Retries write several assistant rows sharing a `requestId` for one charge."""
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            _write(f, [_row("req-1", 100), _row("req-1", 100), _row("req-2", 5)])
            totals = usage.scan_file(f)
            out = sum(c["output_tokens"] for c in totals.values())
            self.assertEqual(out, 105, "a repeated requestId must not be charged twice")

    def test_fanout_never_lands_in_the_conversations_own_lane(self):
        """`subagents/` and `isSidechain` are fan-out cost — a separate pool, never a sum."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write(root / "proj" / "sess.jsonl", [_row("a", 10), _row("b", 20, sidechain=True)])
            _write(root / "proj" / "sess" / "subagents" / "agent-x.jsonl", [_row("c", 40)])

            lanes = {}
            for path in sorted(root.rglob("*.jsonl")):
                for key, counts in usage.scan_file(path).items():
                    lanes.setdefault(key.split("|")[3], 0)
                    lanes[key.split("|")[3]] += counts["output_tokens"]
            self.assertEqual(lanes, {"main": 10, "sidechain": 20, "subagent": 40})


class Incremental(unittest.TestCase):
    def test_a_changed_transcript_is_reread(self):
        """The index is keyed on size+mtime; a live session appends and must not stay stale."""
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            _write(f, [_row("a", 10)])
            index, scanned = usage.build_index([f], {})
            self.assertEqual(scanned, 1)

            again, scanned = usage.build_index([f], index)
            self.assertEqual(scanned, 0, "an unchanged file must come from the cache")

            _write(f, [_row("a", 10), _row("b", 90)])
            os.utime(f, (0, 0))  # a changed size must be enough on its own
            fresh, scanned = usage.build_index([f], again)
            self.assertEqual(scanned, 1, "an appended transcript must be re-read")
            folded = usage.fold(fresh)
            self.assertEqual(sum(c["output_tokens"] for c in folded.values()), 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
