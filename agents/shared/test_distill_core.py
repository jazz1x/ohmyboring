#!/usr/bin/env python3
"""Network-free tests for shared distillation core behavior.

Run: python3 agents/shared/test_distill_core.py
"""
import io
import os
import unittest
from unittest import mock

import distill_core


SHALLOW_NOTE = {
    "title": "작업 정리",
    "body": "## Result\nEverything was checked.",
    "tags": ["omb"],
    "tools": ["git"],
    "concepts": ["ingest"],
    "claims": [
        {
            "subject": "work",
            "predicate": "status",
            "value": "done",
            "kind": "fact",
            "confidence": "certain",
        }
    ],
}


RICH_NOTE = {
    "title": "omb ingest truth witness PR #159",
    "body": "\n".join(
        [
            "## Problem",
            "Hermes ingestion could claim success without a witness.",
            "## As-Is",
            "The old state marked done after bounded attempts.",
            "## To-Be",
            "The new state keeps retry visible until a note witness exists.",
            "## Decision",
            "Use retry backoff instead of false done.",
            "## Evidence",
            "PR #159 had 8 CI checks passing and eval-gate took 2m10s.",
            "## Result",
            "The PR reached CLEAN state.",
            "## Next",
            "Add a resolution verifier before runtime enforcement.",
        ]
    ),
    "tags": ["omb"],
    "tools": ["git"],
    "concepts": ["ingest"],
    "claims": [
        {
            "subject": "ingest",
            "predicate": "completion-state",
            "value": "retry-visible",
            "kind": "decision",
            "confidence": "certain",
        },
        {
            "subject": "ci",
            "predicate": "passed-checks",
            "value": "8",
            "kind": "fact",
            "confidence": "certain",
        },
        {
            "subject": "eval-gate",
            "predicate": "duration",
            "value": "2m10s",
            "kind": "fact",
            "confidence": "certain",
        },
        {
            "subject": "resolution-gate",
            "predicate": "next-step",
            "value": "add verifier",
            "kind": "next",
            "confidence": "certain",
        },
    ],
}


class DistillCoreResolutionGateTests(unittest.TestCase):
    def setUp(self):
        self.old_resolution = os.environ.get("BORING_DISTILL_RESOLUTION")
        self.old_strict = os.environ.get("BORING_DISTILL_RESOLUTION_STRICT")
        os.environ["BORING_DISTILL_RESOLUTION"] = "evidence"
        os.environ.pop("BORING_DISTILL_RESOLUTION_STRICT", None)

    def tearDown(self):
        _restore_env("BORING_DISTILL_RESOLUTION", self.old_resolution)
        _restore_env("BORING_DISTILL_RESOLUTION_STRICT", self.old_strict)

    def test_prompt_contains_resolution_contract(self):
        prompt = distill_core._build_prompt("transcript", "personal", "repo", resolution="forensic")

        self.assertIn("RESOLUTION CONTRACT: forensic", prompt)
        self.assertIn("timeline", prompt)
        self.assertIn("root_cause", prompt)

    def test_invalid_env_resolution_falls_back_to_evidence(self):
        os.environ["BORING_DISTILL_RESOLUTION"] = "typo"
        stderr = io.StringIO()

        with mock.patch.object(distill_core.sys, "stderr", stderr):
            level = distill_core._distill_resolution()

        self.assertEqual(level, "evidence")
        self.assertIn("invalid BORING_DISTILL_RESOLUTION", stderr.getvalue())

    def test_report_only_resolution_failure_still_remembers(self):
        stderr = io.StringIO()
        with mock.patch.object(distill_core, "_call_llm", return_value=SHALLOW_NOTE), \
             mock.patch.object(distill_core, "_call_remember", return_value=True) as remember, \
             mock.patch.object(distill_core.sys, "stderr", stderr):
            ok = distill_core.distill_and_remember(
                "PR #159 had 8 CI checks passing and eval-gate took 2m10s.",
                "personal",
                "oh-my-boring",
                "s1",
            )

        self.assertTrue(ok)
        remember.assert_called_once()
        self.assertIn("resolution gate failed (evidence)", stderr.getvalue())

    def test_strict_resolution_failure_blocks_remember(self):
        os.environ["BORING_DISTILL_RESOLUTION_STRICT"] = "1"
        stderr = io.StringIO()
        with mock.patch.object(distill_core, "_call_llm", return_value=SHALLOW_NOTE), \
             mock.patch.object(distill_core, "_call_remember", return_value=True) as remember, \
             mock.patch.object(distill_core.sys, "stderr", stderr):
            ok = distill_core.distill_and_remember(
                "PR #159 had 8 CI checks passing and eval-gate took 2m10s.",
                "personal",
                "oh-my-boring",
                "s2",
            )

        self.assertFalse(ok)
        remember.assert_not_called()
        self.assertIn("resolution gate failed (evidence)", stderr.getvalue())

    def test_resolution_pass_calls_remember_without_warning(self):
        stderr = io.StringIO()
        with mock.patch.object(distill_core, "_call_llm", return_value=RICH_NOTE), \
             mock.patch.object(distill_core, "_call_remember", return_value=True) as remember, \
             mock.patch.object(distill_core.sys, "stderr", stderr):
            ok = distill_core.distill_and_remember(
                "PR #159 had 8 CI checks passing and eval-gate took 2m10s.",
                "personal",
                "oh-my-boring",
                "s3",
            )

        self.assertTrue(ok)
        remember.assert_called_once()
        self.assertNotIn("resolution gate failed", stderr.getvalue())


def _restore_env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


if __name__ == "__main__":
    unittest.main(verbosity=2)
