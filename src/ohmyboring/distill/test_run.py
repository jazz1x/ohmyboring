#!/usr/bin/env python3
"""Network-free tests for the distillation flow: prompts in, notes gated, remember called.

Run: python3 src/ohmyboring/distill/test_run.py
"""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ohmyboring.adapters import engine, events  # noqa: E402
from ohmyboring.adapters import llm as llm_adapter  # noqa: E402
from ohmyboring.distill import resolution_event, run, settings  # noqa: E402
from ohmyboring.distill.nodes import claims as claims_node  # noqa: E402
from ohmyboring.distill.nodes import evidence as evidence_node  # noqa: E402
from ohmyboring.distill.nodes import note as note_node  # noqa: E402
from ohmyboring.distill.prompts.draft import build_prompt  # noqa: E402
from ohmyboring.distill.prompts.repair import build_repair_prompt  # noqa: E402
from ohmyboring.distill.resolution import verify_note_resolution  # noqa: E402

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
        self.old_event_log = os.environ.get("BORING_EVENT_LOG")
        self.old_event_sink = os.environ.get("BORING_EVENT_SINK")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["BORING_DISTILL_RESOLUTION"] = "evidence"
        os.environ["BORING_EVENT_LOG"] = os.path.join(self.tmp.name, "events.ndjson")
        os.environ["BORING_EVENT_SINK"] = "spool"

    def tearDown(self):
        _restore_env("BORING_DISTILL_RESOLUTION", self.old_resolution)
        _restore_env("BORING_EVENT_LOG", self.old_event_log)
        _restore_env("BORING_EVENT_SINK", self.old_event_sink)
        self.tmp.cleanup()

    def test_prompt_contains_resolution_contract(self):
        prompt = build_prompt("transcript", "personal", "repo", resolution="forensic")

        self.assertIn("RESOLUTION CONTRACT: forensic", prompt)
        self.assertIn("timeline", prompt)
        self.assertIn("root_cause", prompt)

    def test_evidence_prompt_uses_verifier_section_headings(self):
        prompt = build_prompt(
            "transcript",
            "personal",
            "repo",
            note_lang="en",
            resolution="evidence",
        )

        self.assertIn("## As-Is", prompt)
        self.assertIn("## To-Be", prompt)
        self.assertIn("## Evidence", prompt)
        self.assertIn("Do not rename required headings", prompt)

    def test_forensic_prompt_includes_forensic_section_headings(self):
        prompt = build_prompt(
            "transcript",
            "personal",
            "repo",
            note_lang="en",
            resolution="forensic",
        )

        self.assertIn("## Timeline", prompt)
        self.assertIn("## Root Cause", prompt)
        self.assertIn("## Regression / Repro", prompt)

    def test_invalid_env_resolution_falls_back_to_evidence(self):
        os.environ["BORING_DISTILL_RESOLUTION"] = "typo"
        stderr = io.StringIO()

        with mock.patch.object(sys, "stderr", stderr):
            level = settings.distill_resolution()

        self.assertEqual(level, "evidence")
        self.assertIn("invalid BORING_DISTILL_RESOLUTION", stderr.getvalue())

    def test_repair_prompt_treats_previous_json_as_non_evidence(self):
        report = verify_note_resolution(SHALLOW_NOTE, "PR #159 took 2m10s", "evidence")

        prompt = build_repair_prompt(
            "PR #159 took 2m10s",
            "personal",
            "oh-my-boring",
            SHALLOW_NOTE,
            report,
            "evidence",
        )

        self.assertIn("previous JSON is a draft, not evidence", prompt)
        self.assertIn("transcript is the only evidence source", prompt)
        self.assertIn("Do not rename required headings", prompt)
        self.assertIn("copy the required number of exact tokens", prompt)

    def test_distill_skip_logs_workflow_event(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(llm_adapter, "call_llm", return_value={"skip": True}),
            mock.patch.object(engine, "call_remember") as remember,
            mock.patch.object(sys, "stderr", stderr),
        ):
            ok = run.distill_and_remember(
                "pure chit-chat",
                "personal",
                "oh-my-boring",
                "s-skip",
            )

        self.assertTrue(ok)
        remember.assert_not_called()
        self.assertIn("LLM decided SKIP", stderr.getvalue())
        event = _read_last_event()
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["verifier_status"], "skipped")
        self.assertEqual(event["remember_status"], "skipped")
        self.assertEqual(event["workflow"], "memory_ingest")
        self.assertEqual(event["workflow_node"], "skipped")
        self.assertEqual(event["workflow_outcome"], "skip")

    def test_resolution_failure_repairs_once_then_remembers(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(llm_adapter, "call_llm", side_effect=[SHALLOW_NOTE, RICH_NOTE]) as llm,
            mock.patch.object(
                engine,
                "call_remember",
                return_value=engine.RememberOutcome(True, "remembered"),
            ) as remember,
            mock.patch.object(sys, "stderr", stderr),
        ):
            ok = run.distill_and_remember(
                "PR #159 had 8 CI checks passing and eval-gate took 2m10s.",
                "personal",
                "oh-my-boring",
                "s1",
            )

        self.assertTrue(ok)
        self.assertEqual(llm.call_count, 2)
        remember.assert_called_once()
        self.assertIn("resolution gate failed (evidence)", stderr.getvalue())
        self.assertIn("resolution repair passed", stderr.getvalue())
        event = _read_last_event()
        self.assertEqual(event["verifier_status"], "repaired")
        self.assertEqual(event["remember_status"], "remembered")
        self.assertEqual(event["workflow"], "memory_ingest")
        self.assertEqual(event["workflow_node"], "remember_requested")
        self.assertEqual(event["workflow_outcome"], "pass")

    def test_resolution_repair_failure_blocks_remember(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(llm_adapter, "call_llm", side_effect=[SHALLOW_NOTE, SHALLOW_NOTE]),
            mock.patch.object(
                engine,
                "call_remember",
                return_value=engine.RememberOutcome(True, "remembered"),
            ) as remember,
            mock.patch.object(sys, "stderr", stderr),
        ):
            ok = run.distill_and_remember(
                "PR #159 had 8 CI checks passing and eval-gate took 2m10s.",
                "personal",
                "oh-my-boring",
                "s2",
            )

        self.assertFalse(ok)
        remember.assert_not_called()
        self.assertIn("resolution gate failed (evidence)", stderr.getvalue())
        self.assertIn("resolution repair failed", stderr.getvalue())
        event = _read_last_event()
        self.assertEqual(event["verifier_status"], "failed")
        self.assertEqual(event["remember_status"], "not_called")
        self.assertEqual(event["workflow_node"], "resolution_repaired")
        self.assertEqual(event["workflow_outcome"], "fail")

    def test_failed_language_retry_keeps_the_original_note(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(settings, "NOTE_LANG", "ko"),
            mock.patch.object(llm_adapter, "call_llm", return_value=RICH_NOTE) as llm,
            mock.patch.object(
                engine,
                "call_remember",
                return_value=engine.RememberOutcome(True, "remembered"),
            ) as remember,
            mock.patch.object(sys, "stderr", stderr),
        ):
            ok = run.distill_and_remember(
                "PR #159 had 8 CI checks passing and eval-gate took 2m10s.",
                "personal",
                "oh-my-boring",
                "s-lang",
            )

        self.assertTrue(ok)
        self.assertEqual(llm.call_count, 2)
        self.assertEqual(remember.call_args.args[0], RICH_NOTE["title"])
        self.assertIn("language retry failed — keeping original", stderr.getvalue())

    def test_unpreparable_repair_response_logs_failed_not_called(self):
        prepared = note_node.prepare_note(SHALLOW_NOTE)
        with (
            mock.patch.object(llm_adapter, "call_llm", side_effect=[SHALLOW_NOTE, {"title": "x"}]),
            mock.patch.object(note_node, "prepare_note", side_effect=[prepared, None]),
            mock.patch.object(engine, "call_remember") as remember,
            mock.patch.object(resolution_event, "log_resolution_event") as log_event,
            mock.patch.object(sys, "stderr", io.StringIO()),
        ):
            ok = run.distill_and_remember(
                "PR #159 had 8 CI checks passing and eval-gate took 2m10s.",
                "personal",
                "oh-my-boring",
                "s-noprep",
            )

        self.assertFalse(ok)
        remember.assert_not_called()
        log_event.assert_called_once()
        self.assertEqual(log_event.call_args.args[-2:], ("failed", "not_called"))

    def test_resolution_pass_calls_remember_and_logs_event(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(llm_adapter, "call_llm", return_value=RICH_NOTE),
            mock.patch.object(
                engine,
                "call_remember",
                return_value=engine.RememberOutcome(True, "duplicate"),
            ) as remember,
            mock.patch.object(sys, "stderr", stderr),
        ):
            ok = run.distill_and_remember(
                "PR #159 had 8 CI checks passing and eval-gate took 2m10s.",
                "personal",
                "oh-my-boring",
                "s3",
            )

        self.assertTrue(ok)
        remember.assert_called_once()
        self.assertNotIn("resolution gate failed", stderr.getvalue())
        event = _read_last_event()
        self.assertEqual(event["verifier_status"], "pass")
        self.assertEqual(event["remember_status"], "duplicate")
        self.assertEqual(event["workflow_node"], "remember_requested")
        self.assertEqual(event["workflow_outcome"], "duplicate")

    def test_prepare_note_promotes_semantic_decision_claim_kind(self):
        parsed = {
            "title": "의미 기반 claim kind 정규화",
            "body": "## Result\nVerifier can see the decision claim.",
            "claims": [
                {
                    "subject": "distill-prompt",
                    "predicate": "decision",
                    "value": "use verifier-matched section headings",
                    "kind": "fact",
                    "confidence": "certain",
                }
            ],
        }

        note = note_node.prepare_note(parsed)

        self.assertEqual(note["claims"][0]["kind"], "decision")

    def test_required_decision_claim_is_derived_from_decision_section(self):
        note = {
            "title": "olympus: MCP 분석",
            "body": "\n".join(
                [
                    "## 배경 / 문제",
                    "MCP 분석이 필요했다.",
                    "## 현재 상태",
                    "보고서가 0개였다.",
                    "## 목표 상태",
                    "분석 결과를 남긴다.",
                    "## 결정",
                    "hermes-rs MCP 기능을 먼저 분석하기로 했다.",
                    "## 근거 / 검증",
                    "2026-06-18 기준 보고서 0개를 확인했다.",
                    "## 결과",
                    "다음 분석 대상이 정해졌다.",
                    "## 남은 일",
                    "추가 분석이 필요하다.",
                ]
            ),
            "claims": [
                {
                    "subject": "olympus",
                    "predicate": "report-count",
                    "value": "0개",
                    "kind": "fact",
                    "confidence": "certain",
                },
                {
                    "subject": "olympus",
                    "predicate": "date",
                    "value": "2026-06-18",
                    "kind": "fact",
                    "confidence": "certain",
                },
                {
                    "subject": "olympus",
                    "predicate": "target",
                    "value": "hermes-rs",
                    "kind": "fact",
                    "confidence": "certain",
                },
                {
                    "subject": "olympus",
                    "predicate": "next-step",
                    "value": "추가 분석",
                    "kind": "next",
                    "confidence": "certain",
                },
            ],
        }

        fixed = claims_node.ensure_required_claim_kinds(note, "evidence", "olympus")
        report = verify_note_resolution(
            {"title": fixed["title"], "body": fixed["body"], "claims": fixed["claims"]},
            "2026-06-18 보고서 0개",
            "evidence",
        )

        self.assertTrue(report.ok, report.missing)
        self.assertIn("decision", {claim["kind"] for claim in fixed["claims"]})

    def test_required_evidence_tokens_are_derived_from_transcript_excerpts(self):
        transcript = "PR #165 fixed the readiness gate and 42 checks stayed green."
        note = {
            "title": "readiness gate",
            "body": "\n".join(
                [
                    "## Problem",
                    "The readiness gate could stay red after a resolved failure.",
                    "## As-Is",
                    "The note omitted exact transcript evidence.",
                    "## To-Be",
                    "The note preserves concrete evidence from the transcript.",
                    "## Decision",
                    "Use transcript excerpts only when exact evidence tokens are missing.",
                    "## Evidence",
                    "The verifier saw the shape but no exact token.",
                    "## Result",
                    "Evidence can be checked before remember.",
                    "## Next",
                    "No follow-up.",
                ]
            ),
            "claims": [
                {
                    "subject": "evidence",
                    "predicate": "policy",
                    "value": "derive excerpt",
                    "kind": "decision",
                    "confidence": "certain",
                },
                {
                    "subject": "verifier",
                    "predicate": "state",
                    "value": "strict",
                    "kind": "fact",
                    "confidence": "certain",
                },
                {
                    "subject": "readiness",
                    "predicate": "status",
                    "value": "checked",
                    "kind": "fact",
                    "confidence": "certain",
                },
                {
                    "subject": "follow-up",
                    "predicate": "next-step",
                    "value": "none",
                    "kind": "next",
                    "confidence": "certain",
                },
            ],
        }

        fixed = evidence_node.ensure_required_evidence_tokens(note, transcript, "evidence")
        report = verify_note_resolution(
            {"title": fixed["title"], "body": fixed["body"], "claims": fixed["claims"]},
            transcript,
            "evidence",
        )

        self.assertTrue(report.ok, report.missing)
        self.assertIn("PR #165", fixed["body"])
        self.assertIn("42", fixed["body"])

    def test_remember_failure_logs_failed_status(self):
        with (
            mock.patch.object(llm_adapter, "call_llm", return_value=RICH_NOTE),
            mock.patch.object(
                engine,
                "call_remember",
                return_value=engine.RememberOutcome(False, "failed"),
            ),
        ):
            ok = run.distill_and_remember(
                "PR #159 had 8 CI checks passing and eval-gate took 2m10s.",
                "personal",
                "oh-my-boring",
                "s4",
            )

        self.assertFalse(ok)
        event = _read_last_event()
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["remember_status"], "failed")
        self.assertEqual(event["workflow_node"], "remember_requested")
        self.assertEqual(event["workflow_outcome"], "fail")

    def test_event_log_write_failure_does_not_override_remember_success(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(llm_adapter, "call_llm", return_value=RICH_NOTE),
            mock.patch.object(
                engine,
                "call_remember",
                return_value=engine.RememberOutcome(True, "remembered"),
            ),
            mock.patch.object(events, "append_event", side_effect=OSError("denied")),
            mock.patch.object(sys, "stderr", stderr),
        ):
            ok = run.distill_and_remember(
                "PR #159 had 8 CI checks passing and eval-gate took 2m10s.",
                "personal",
                "oh-my-boring",
                "s5",
            )

        self.assertTrue(ok)
        self.assertIn("event log write failed", stderr.getvalue())


class StripTrailingMetadataTests(unittest.TestCase):
    def test_strips_trailing_block(self):
        body = "## 결과\nfixed it.\n\ntags: [a, b]\ntools: [git]\nconcepts: [x]"
        self.assertEqual(note_node.strip_trailing_metadata(body), "## 결과\nfixed it.")

    def test_keeps_clean_body(self):
        body = "## 배경\nproblem\n\n## 결과\nsolved"
        self.assertEqual(note_node.strip_trailing_metadata(body), body.rstrip())

    def test_does_not_strip_midbody_mention(self):
        # A "tools:" line in the MIDDLE (not the trailing run) must be preserved.
        body = "intro\ntools: relevant here\nmore prose"
        self.assertEqual(note_node.strip_trailing_metadata(body), body.rstrip())


def _restore_env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def _read_last_event():
    with open(os.environ["BORING_EVENT_LOG"], encoding="utf-8") as f:
        return json.loads(f.readlines()[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
