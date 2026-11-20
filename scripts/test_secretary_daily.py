#!/usr/bin/env python3
"""Tests for secretary_daily.py.

Run: python3 scripts/test_secretary_daily.py   (no pytest dependency)
"""

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("secretary_daily", HERE / "secretary_daily.py")
secretary_daily = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(secretary_daily)


class ScanDay(unittest.TestCase):
    def test_a_transcript_deleted_after_listing_is_skipped(self):
        gone = str(HERE / "no-such-transcript.jsonl")
        with mock.patch.object(secretary_daily.usage, "transcript_files", return_value=[gone]):
            files, received, runs = secretary_daily.scan_day("", "", 0)
        self.assertEqual((files, received, runs), ([], [], []))


if __name__ == "__main__":
    unittest.main()
