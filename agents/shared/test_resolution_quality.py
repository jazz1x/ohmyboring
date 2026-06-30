#!/usr/bin/env python3
"""Network-free tests for distillation resolution quality gates.

Run: python3 agents/shared/test_resolution_quality.py
"""
import unittest
from typing import Optional, get_type_hints

from resolution_quality import normalize_resolution, resolution_prompt_contract, verify_note_resolution
import resolution_quality


def claim(subject, predicate, value, kind="fact"):
    return {
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "kind": kind,
        "confidence": "certain",
    }


class ResolutionQualityTests(unittest.TestCase):
    def test_unknown_resolution_falls_back_to_standard(self):
        self.assertEqual(normalize_resolution("unknown"), "standard")

    def test_evidence_note_keeps_decisions_as_is_to_be_and_numbers(self):
        transcript = (
            "PR #159 CI checks: 8 passed. "
            "As-is: Hermes marked done without a witness. "
            "To-be: retry stays visible. eval-gate took 2m10s."
        )
        note = {
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
            "claims": [
                claim("ingest", "completion-state", "retry-visible", "decision"),
                claim("ci", "passed-checks", "8", "fact"),
                claim("eval-gate", "duration", "2m10s", "fact"),
                claim("resolution-gate", "next-step", "add verifier", "next"),
            ],
        }

        report = verify_note_resolution(note, transcript, "evidence")

        self.assertTrue(report.ok, report.missing)
        self.assertEqual(report.claim_count, 4)
        self.assertIn("pr#159", report.evidence_tokens_kept)
        self.assertIn("2m10s", report.evidence_tokens_kept)

    def test_evidence_note_rejects_shallow_summary(self):
        transcript = "PR #159 had 8 CI checks passing and eval-gate took 2m10s."
        note = {
            "title": "작업 정리",
            "body": "## Result\nEverything was checked and cleaned up.",
            "claims": [
                claim("work", "status", "done", "fact"),
            ],
        }

        report = verify_note_resolution(note, transcript, "evidence")

        self.assertFalse(report.ok)
        self.assertIn("section:as_is", report.missing)
        self.assertIn("section:to_be", report.missing)
        self.assertIn("claim-kind:decision", report.missing)
        self.assertIn("claims:min:4", report.missing)
        self.assertIn("evidence-tokens:min:2", report.missing)

    def test_evidence_requirement_does_not_disappear_without_transcript_tokens(self):
        note = {
            "title": "resolution gate",
            "body": "\n".join(
                [
                    "## Problem",
                    "A shallow distillation can lose concrete context.",
                    "## As-Is",
                    "The prior note only summarized the session.",
                    "## To-Be",
                    "The note should preserve concrete evidence.",
                    "## Decision",
                    "Use a repair-once verifier before remember.",
                    "## Evidence",
                    "No concrete token was preserved.",
                    "## Result",
                    "The verifier reports the gap.",
                    "## Next",
                    "Keep strict mode opt-in.",
                ]
            ),
            "claims": [
                claim("resolution-gate", "mode", "repair-once", "decision"),
                claim("distillation", "quality", "specific", "fact"),
                claim("strict-mode", "default", "off", "fact"),
                claim("readiness", "next-step", "surface gap", "next"),
            ],
        }

        report = verify_note_resolution(note, "no numeric or ticket evidence here", "evidence")

        self.assertFalse(report.ok)
        self.assertEqual(report.evidence_tokens_seen, ())
        self.assertIn("evidence-tokens:min:2", report.missing)

    def test_evidence_requirement_is_capped_by_seen_tokens(self):
        transcript = "Only PR #165 is concrete evidence in this short session."
        note = {
            "title": "short evidence session",
            "body": "\n".join(
                [
                    "## Problem",
                    "A short session still needs a structured note.",
                    "## As-Is",
                    "The transcript has only one concrete token.",
                    "## To-Be",
                    "The verifier should require that token, not invented extras.",
                    "## Decision",
                    "Keep the evidence note if the single token is preserved.",
                    "## Evidence",
                    "PR #165 is the only concrete token in the transcript.",
                    "## Result",
                    "The note remains specific without fabricating a second token.",
                    "## Next",
                    "No follow-up.",
                ]
            ),
            "claims": [
                claim("evidence-gate", "policy", "preserve available tokens", "decision"),
                claim("short-session", "evidence", "PR #165", "fact"),
                claim("verifier", "required-token-count", "1", "fact"),
                claim("follow-up", "next-step", "none", "next"),
            ],
        }

        report = verify_note_resolution(note, transcript, "evidence")

        self.assertTrue(report.ok, report.missing)
        self.assertEqual(report.evidence_tokens_seen, ("pr#165",))
        self.assertEqual(report.evidence_tokens_kept, ("pr#165",))

    def test_public_annotations_are_python39_type_hint_safe(self):
        hints = get_type_hints(resolution_quality.normalize_resolution)

        self.assertEqual(hints["resolution"], Optional[str])

    def test_forensic_requires_cause_timeline_regression_and_next_claim(self):
        transcript = (
            "At 09:17 worker was scheduled. At 09:37 next run was set. "
            "Root cause was stale retry handling. Regression fixture PR #160."
        )
        note = {
            "title": "forensic ingest recovery PR #160",
            "body": "\n".join(
                [
                    "## Problem",
                    "Retry state could disappear from the workflow.",
                    "## As-Is",
                    "A retry marker could be treated as terminal.",
                    "## To-Be",
                    "A stale retry marker re-enters the queue.",
                    "## Timeline",
                    "At 09:17 worker was scheduled; 09:37 was the next run.",
                    "## Root Cause",
                    "Fresh retry and terminal skip were conflated.",
                    "## Decision",
                    "Keep retry as backoff state.",
                    "## Evidence",
                    "Regression fixture covers PR #160.",
                    "## Result",
                    "The retry path is testable.",
                    "## Regression",
                    "Fixture asserts stale retry requeue.",
                    "## Next",
                    "Expose quality events in readiness.",
                ]
            ),
            "claims": [
                claim("retry-marker", "state", "backoff", "decision"),
                claim("worker", "scheduled-at", "09:17", "fact"),
                claim("worker", "next-run-at", "09:37", "fact"),
                claim("ingest", "risk", "terminal retry skip", "risk"),
                claim("fixture", "covers", "PR #160", "fact"),
                claim("readiness", "next-step", "surface quality events", "next"),
            ],
        }

        report = verify_note_resolution(note, transcript, "forensic")

        self.assertTrue(report.ok, report.missing)
        self.assertGreaterEqual(report.claim_count, 6)
        self.assertIn("pr#160", report.evidence_tokens_kept)

    def test_evidence_accepts_japanese_section_headers(self):
        transcript = "PR #159 の CI は 8 件通過。eval-gate は 2m10s。"
        note = {
            "title": "omb: 取り込み完了判定 PR #159",
            "body": "\n".join(
                [
                    "## 背景 / 問題",
                    "Hermes が witness なしで成功扱いする可能性があった。",
                    "## 現状",
                    "以前は一定回数後に done 扱いしていた。",
                    "## 目標",
                    "retry を見える状態で残す。",
                    "## 決定",
                    "false done ではなく retry backoff を使う。",
                    "## 根拠",
                    "PR #159 の CI 8 件と eval-gate 2m10s を確認した。",
                    "## 結果 / 解決",
                    "CLEAN 状態になった。",
                    "## 残件",
                    "解像度 verifier を readiness に出す。",
                ]
            ),
            "claims": [
                claim("ingest", "completion-state", "retry-visible", "decision"),
                claim("ci", "passed-checks", "8", "fact"),
                claim("eval-gate", "duration", "2m10s", "fact"),
                claim("readiness", "next-step", "surface verifier state", "next"),
            ],
        }

        report = verify_note_resolution(note, transcript, "evidence")

        self.assertTrue(report.ok, report.missing)

    def test_invalid_claim_kind_is_reported(self):
        note = {
            "title": "standard note",
            "body": "## Problem\nx\n## Decision\ny\n## Result\nz",
            "claims": [
                claim("x", "y", "z", "verdict"),
                claim("x", "result", "complete", "fact"),
            ],
        }

        report = verify_note_resolution(note, "", "standard")

        self.assertFalse(report.ok)
        self.assertIn("claim-kind-invalid:verdict", report.missing)

    def test_prompt_contract_spells_out_required_claim_kinds(self):
        contract = resolution_prompt_contract("evidence")

        self.assertIn("Required claim kinds: decision, fact", contract)
        self.assertIn("Required claim kinds are hard gates", contract)
        self.assertIn('"kind":"decision"', contract)


if __name__ == "__main__":
    unittest.main(verbosity=2)
