#!/usr/bin/env python3
"""Network-free tests for shared distillation core behavior.

Run: python3 agents/shared/test_distill_core.py
"""
import io
import json
import subprocess
import pathlib
import os
import tempfile
import unittest
from unittest import mock

import distill_core
import transcript


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
        prompt = distill_core._build_prompt("transcript", "personal", "repo", resolution="forensic")

        self.assertIn("RESOLUTION CONTRACT: forensic", prompt)
        self.assertIn("timeline", prompt)
        self.assertIn("root_cause", prompt)

    def test_evidence_prompt_uses_verifier_section_headings(self):
        prompt = distill_core._build_prompt(
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
        prompt = distill_core._build_prompt(
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

        with mock.patch.object(distill_core.sys, "stderr", stderr):
            level = distill_core._distill_resolution()

        self.assertEqual(level, "evidence")
        self.assertIn("invalid BORING_DISTILL_RESOLUTION", stderr.getvalue())

    def test_repair_prompt_treats_previous_json_as_non_evidence(self):
        report = distill_core.verify_note_resolution(SHALLOW_NOTE, "PR #159 took 2m10s", "evidence")

        prompt = distill_core._build_repair_prompt(
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
        with mock.patch.object(distill_core, "_call_llm", return_value={"skip": True}), \
             mock.patch.object(distill_core, "_call_remember") as remember, \
             mock.patch.object(distill_core.sys, "stderr", stderr):
            ok = distill_core.distill_and_remember(
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
        with mock.patch.object(distill_core, "_call_llm", side_effect=[SHALLOW_NOTE, RICH_NOTE]) as llm, \
             mock.patch.object(
                 distill_core,
                 "_call_remember",
                 return_value=distill_core.RememberOutcome(True, "remembered"),
             ) as remember, \
             mock.patch.object(distill_core.sys, "stderr", stderr):
            ok = distill_core.distill_and_remember(
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
        with mock.patch.object(distill_core, "_call_llm", side_effect=[SHALLOW_NOTE, SHALLOW_NOTE]), \
             mock.patch.object(
                 distill_core,
                 "_call_remember",
                 return_value=distill_core.RememberOutcome(True, "remembered"),
             ) as remember, \
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
        self.assertIn("resolution repair failed", stderr.getvalue())
        event = _read_last_event()
        self.assertEqual(event["verifier_status"], "failed")
        self.assertEqual(event["remember_status"], "not_called")
        self.assertEqual(event["workflow_node"], "resolution_repaired")
        self.assertEqual(event["workflow_outcome"], "fail")

    def test_resolution_pass_calls_remember_and_logs_event(self):
        stderr = io.StringIO()
        with mock.patch.object(distill_core, "_call_llm", return_value=RICH_NOTE), \
             mock.patch.object(
                 distill_core,
                 "_call_remember",
                 return_value=distill_core.RememberOutcome(True, "duplicate"),
             ) as remember, \
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

        note = distill_core._prepare_note(parsed)

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
                {"subject": "olympus", "predicate": "report-count", "value": "0개", "kind": "fact", "confidence": "certain"},
                {"subject": "olympus", "predicate": "date", "value": "2026-06-18", "kind": "fact", "confidence": "certain"},
                {"subject": "olympus", "predicate": "target", "value": "hermes-rs", "kind": "fact", "confidence": "certain"},
                {"subject": "olympus", "predicate": "next-step", "value": "추가 분석", "kind": "next", "confidence": "certain"},
            ],
        }

        fixed = distill_core._ensure_required_claim_kinds(note, "evidence", "olympus")
        report = distill_core.verify_note_resolution(
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
                {"subject": "evidence", "predicate": "policy", "value": "derive excerpt", "kind": "decision", "confidence": "certain"},
                {"subject": "verifier", "predicate": "state", "value": "strict", "kind": "fact", "confidence": "certain"},
                {"subject": "readiness", "predicate": "status", "value": "checked", "kind": "fact", "confidence": "certain"},
                {"subject": "follow-up", "predicate": "next-step", "value": "none", "kind": "next", "confidence": "certain"},
            ],
        }

        fixed = distill_core._ensure_required_evidence_tokens(note, transcript, "evidence")
        report = distill_core.verify_note_resolution(
            {"title": fixed["title"], "body": fixed["body"], "claims": fixed["claims"]},
            transcript,
            "evidence",
        )

        self.assertTrue(report.ok, report.missing)
        self.assertIn("PR #165", fixed["body"])
        self.assertIn("42", fixed["body"])

    def test_remember_failure_logs_failed_status(self):
        with mock.patch.object(distill_core, "_call_llm", return_value=RICH_NOTE), \
             mock.patch.object(
                 distill_core,
                 "_call_remember",
                 return_value=distill_core.RememberOutcome(False, "failed"),
             ):
            ok = distill_core.distill_and_remember(
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
        with mock.patch.object(distill_core, "_call_llm", return_value=RICH_NOTE), \
             mock.patch.object(
                 distill_core,
                 "_call_remember",
                 return_value=distill_core.RememberOutcome(True, "remembered"),
             ), \
             mock.patch.object(distill_core.event_log, "append_event", side_effect=OSError("denied")), \
             mock.patch.object(distill_core.sys, "stderr", stderr):
            ok = distill_core.distill_and_remember(
                "PR #159 had 8 CI checks passing and eval-gate took 2m10s.",
                "personal",
                "oh-my-boring",
                "s5",
            )

        self.assertTrue(ok)
        self.assertIn("event log write failed", stderr.getvalue())


def _restore_env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


class BackstopClampTests(unittest.TestCase):
    """The backstop is the last-resort bound for a caller that forgot to clamp.

    It used to truncate silently with its own head/tail split, so raising a caller's clamp
    above it looked like it worked while the extra text was cut back here under a different
    rule. Both properties are pinned: it must announce itself, and it must use the shared
    truncation algorithm rather than a second one that disagrees.
    """

    def _run(self, text):
        stderr = io.StringIO()
        seen = {}

        def fake_llm(prompt):
            seen["prompt"] = prompt
            return None  # stop right after the clamp; nothing downstream is under test

        with mock.patch.object(distill_core, "_call_llm", fake_llm), \
             mock.patch.object(distill_core.sys, "stderr", stderr):
            distill_core.distill_and_remember(text, "personal", "repo")
        return seen.get("prompt", ""), stderr.getvalue()

    def test_backstop_sits_above_every_caller_default(self):
        # The previous backstop equalled the caller defaults, so raising a caller's clamp was
        # silently undone here. A guard at the same value as the knob it guards is a hidden knob.
        for accessor in (transcript.claude_distill_clamp, transcript.codex_distill_clamp,
                         transcript.kimi_distill_clamp):
            self.assertGreater(distill_core.BACKSTOP_CLAMP, accessor())

    def test_over_backstop_is_cut_and_announced(self):
        text = "\n".join(f"line {i} " + "x" * 60 for i in range(4000))
        self.assertGreater(len(text), distill_core.BACKSTOP_CLAMP)
        _prompt, err = self._run(text)
        self.assertIn("backstop", err)
        self.assertIn("unclamped", err)

    def test_under_backstop_is_untouched_and_silent(self):
        text = "short enough\n" * 10
        _prompt, err = self._run(text)
        self.assertNotIn("backstop", err)

    def test_backstop_uses_the_shared_truncator(self):
        # clamp_text snaps to newline boundaries; the old inline split did not. If the backstop
        # ever goes back to slicing by index this lands mid-line and the assertion fails.
        text = "\n".join("y" * 200 for _ in range(1200))
        prompt, _err = self._run(text)
        body = prompt[prompt.find("y" * 200):]
        self.assertNotIn("y" * 201, body)


class RepoIdentityAcrossWorktrees(unittest.TestCase):
    """A task worktree is the same repo. Its folder name is not the repo's name."""

    def _repo(self, tmp):
        repo = pathlib.Path(tmp) / "parent-repo"
        repo.mkdir()
        run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True)
        run("init", "-q")
        run("config", "user.email", "t@example.invalid")
        run("config", "user.name", "t")
        run("config", "commit.gpgsign", "false")
        (repo / "f").write_text("x", encoding="utf-8")
        run("add", "-A")
        run("commit", "-qm", "init")
        return repo

    def test_a_worktree_resolves_to_the_repo_even_without_a_remote(self):
        # Worktrees share the config, so a readable remote already collapses them. This is the
        # case that used to leak: a task worktree whose remote was never set or whose parent is
        # gone fell back to its own folder name, and that name is `<repo>-<task>`.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            wt = pathlib.Path(tmp) / "parent-repo-t5-parts"
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt), "HEAD"],
                capture_output=True,
            )
            self.assertTrue(wt.is_dir(), "worktree was not created")

            self.assertEqual(distill_core.repo_slug(str(wt)), "parent-repo")
            self.assertEqual(distill_core.repo_slug(str(repo)), "parent-repo")

    def test_a_directory_that_is_not_a_repo_names_no_project(self):
        """Naming the project after the folder invented one — 18 notes under `다시한번`.

        A phantom project takes a line in the briefing and a row in the graph, and nothing
        later can tell it from a real one.
        """
        with tempfile.TemporaryDirectory() as tmp:
            plain = pathlib.Path(tmp) / "다시한번"
            plain.mkdir()
            self.assertEqual(distill_core.repo_slug(str(plain)), "")

    def test_no_cwd_names_no_project(self):
        self.assertEqual(distill_core.repo_slug(""), "")


def _read_last_event():
    with open(os.environ["BORING_EVENT_LOG"], encoding="utf-8") as f:
        return json.loads(f.readlines()[-1])


class AutomatedRunsAreNotMemory(unittest.TestCase):
    """A scripted review ends like a session and distils like one.

    Measured on 2026-09-06: of the 34 sessions distilled the previous day, 31 opened with the
    security-review tool's own prompt, and 143 of the 183 notes written inside the measurement
    window (78%) came from those runs. The corpus holds how problems got solved; a tool that says
    the same sentence every night has no such story to tell, and feeding it back in dilutes every
    recall made against the corpus.
    """

    def test_the_tools_own_openings_are_not_memory(self):
        for opening in (
            "Review this change for security vulnerabilities.\n\nChanged files:",
            "You previously flagged these candidate vulnerabilities:\n\n[",
        ):
            self.assertTrue(
                distill_core.is_automated_run(f"[user] {opening}\n[assistant] ok"),
                opening,
            )

    def test_a_person_is_never_mistaken_for_the_tool(self):
        # Both stages of the same tool were observed; matching only the first let one run in the
        # sample through, so both are named.
        human = "[user] 회수게이트도 다 끊겼나본데요\n[assistant] 확인합니다"
        self.assertFalse(distill_core.is_automated_run(human))

        # Quoting the tool mid-session is a person doing real work, not the tool running itself.
        quoting = (
            "[user] 오늘 리뷰 왜 이래요\n"
            "[assistant] 확인\n"
            "[user] Review this change for security vulnerabilities. 라고 떴는데 이게 맞나요"
        )
        self.assertFalse(distill_core.is_automated_run(quoting))

    def test_a_transcript_with_no_user_turn_still_distils(self):
        # Absence of evidence is not evidence: an unparseable or assistant-only transcript is not
        # a reason to throw a session away.
        self.assertFalse(distill_core.is_automated_run("[assistant] 혼자 말함"))
        self.assertFalse(distill_core.is_automated_run(""))


class RetroactiveAutomatedLabelTests(unittest.TestCase):
    """`is_automated_run` shipped on 2026-09-06. The six days before it carry no label at all."""

    AUTOMATED = "[user] Review this change for security vulnerabilities.\n[assistant] ok"
    HUMAN = "[user] 커버리지가 왜 이래요\n[assistant] 봅니다"

    def test_the_label_is_read_back_off_the_transcript(self):
        texts = {"a": self.AUTOMATED, "b": self.HUMAN}
        automated, unreadable = distill_core.classify_automated_sessions(
            ["a", "b"], texts.get
        )
        self.assertEqual(automated, {"a"})
        self.assertEqual(unreadable, set())

    def test_an_unreadable_session_stays_in_the_denominator(self):
        # The failure direction that shrinks a denominator is the one that flatters coverage:
        # every classifier failure would raise the ratio, which is exactly backwards.
        automated, unreadable = distill_core.classify_automated_sessions(
            ["gone"], lambda _sid: None
        )
        self.assertEqual(automated, set())
        self.assertEqual(unreadable, {"gone"}, "unreadable is reported, not silently dropped")

    def test_a_reader_that_raises_is_unreadable_not_automated(self):
        def boom(_sid):
            raise OSError("disk")

        automated, unreadable = distill_core.classify_automated_sessions(["x"], boom)
        self.assertEqual(automated, set())
        self.assertEqual(unreadable, {"x"})

    def test_the_transcript_index_maps_session_id_to_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "proj").mkdir()
            wanted = root / "proj" / "abc123.jsonl"
            wanted.write_text("{}\n", encoding="utf-8")
            (root / "proj" / "notes.md").write_text("x", encoding="utf-8")
            index = distill_core.transcript_index(source_dirs=[str(root)])
        self.assertEqual(index.get("abc123"), wanted)
        self.assertNotIn("notes", index, "only transcripts, not every file beside them")

    def test_a_missing_root_is_skipped_rather_than_raising(self):
        self.assertEqual(distill_core.transcript_index(source_dirs=["/nope/not/here"]), {})


class ConsumptionReachesTheGraph(unittest.TestCase):
    """The scorer's verdict on each note has to leave the session, or the note learns nothing."""

    def _records(self):
        import uptake_core

        hits = [
            {"source_path": "/vault/wiki/wiki-0007.md", "snippet": "the pool died because deadpool recycled a closed socket " * 3},
            {"source_path": "/vault/wiki/wiki-0003.md", "snippet": "the older pool note said keep the socket warm " * 3},
        ]
        return [uptake_core.injection_record("s1", "why did the pool die", hits, 3)]

    def test_used_contested_and_superseding_notes_are_posted_as_edges(self):
        transcript = (
            "[user] why did the pool die\n"
            "[assistant] per wiki-0007 instead of wiki-0003. Even so, wiki-0007 is outdated now.\n"
        )
        with mock.patch.dict(os.environ, {"BORING_EVENT_SINK": "db"}), \
             mock.patch("drudge_client.DrudgeClient") as client:
            distill_core.write_consumption_to_graph("s1", self._records(), transcript)
        (sid, when, used, contested), kwargs = client.return_value.consumption.call_args
        self.assertEqual(sid, "s1")
        self.assertEqual(used, ["/vault/wiki/wiki-0007.md", "/vault/wiki/wiki-0003.md"])
        self.assertEqual(contested, ["/vault/wiki/wiki-0007.md"])
        self.assertEqual(kwargs["supersedes"], [["/vault/wiki/wiki-0007.md", "/vault/wiki/wiki-0003.md"]])
        self.assertRegex(when, r"^\d{4}-\d{2}-\d{2}T")

    def test_a_spooled_sink_never_writes_to_the_live_graph(self):
        transcript = "[user] why\n[assistant] per wiki-0007.\n"
        with mock.patch.dict(os.environ, {"BORING_EVENT_SINK": "spool"}), \
             mock.patch("drudge_client.DrudgeClient") as client:
            distill_core.write_consumption_to_graph("s1", self._records(), transcript)
        client.return_value.consumption.assert_not_called()

    def test_nothing_consumed_means_no_call_and_a_dead_engine_does_not_raise(self):
        with mock.patch.dict(os.environ, {"BORING_EVENT_SINK": "db"}), \
             mock.patch("drudge_client.DrudgeClient") as client:
            distill_core.write_consumption_to_graph("s1", self._records(), "[assistant] unrelated.\n")
            client.return_value.consumption.assert_not_called()
            client.return_value.consumption.side_effect = OSError("down")
            distill_core.write_consumption_to_graph("s1", self._records(), "[assistant] per wiki-0007.\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
