#!/usr/bin/env python3
"""Network-free tests for the stdlib pieces of the distillation core the host hooks use.

Run: python3 agents/shared/test_distill_core.py
"""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import distill_core

from ohmyboring.adapters.engine import Unreachable
from ohmyboring.result import Err, Ok


def _restore_env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


class RepoIdentityAcrossWorktrees(unittest.TestCase):
    """A task worktree is the same repo. Its folder name is not the repo's name."""

    def _repo(self, tmp):
        repo = pathlib.Path(tmp) / "parent-repo"
        repo.mkdir()

        def run(*a):
            return subprocess.run(["git", "-C", str(repo), *a], capture_output=True)

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
        automated, unreadable = distill_core.classify_automated_sessions(["a", "b"], texts.get)
        self.assertEqual(automated, {"a"})
        self.assertEqual(unreadable, set())

    def test_an_unreadable_session_stays_in_the_denominator(self):
        # The failure direction that shrinks a denominator is the one that flatters coverage:
        # every classifier failure would raise the ratio, which is exactly backwards.
        automated, unreadable = distill_core.classify_automated_sessions(["gone"], lambda _sid: None)
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
            {
                "source_path": "/vault/wiki/wiki-0007.md",
                "snippet": "the pool died because deadpool recycled a closed socket " * 3,
            },
            {
                "source_path": "/vault/wiki/wiki-0003.md",
                "snippet": "the older pool note said keep the socket warm " * 3,
            },
        ]
        return [uptake_core.injection_record("s1", "why did the pool die", hits, 3)]

    def test_used_contested_and_superseding_notes_are_posted_as_edges(self):
        transcript = (
            "[user] why did the pool die\n"
            "[assistant] per wiki-0007 instead of wiki-0003. Even so, wiki-0007 is outdated now.\n"
        )
        with (
            mock.patch.dict(os.environ, {"BORING_EVENT_SINK": "db"}),
            mock.patch("ohmyboring.adapters.engine.DrudgeClient") as client,
            mock.patch.object(distill_core.event_log, "append_event"),
        ):
            client.return_value.consumption.return_value = Ok({"used": 2, "contested": 1})
            distill_core.write_consumption_to_graph("s1", self._records(), transcript)
        (sid, when, marks), kwargs = client.return_value.consumption.call_args
        self.assertEqual(sid, "s1")
        self.assertEqual(marks.used, ["/vault/wiki/wiki-0007.md", "/vault/wiki/wiki-0003.md"])
        self.assertEqual(marks.contested, ["/vault/wiki/wiki-0007.md"])
        self.assertEqual(marks.supersedes, [["/vault/wiki/wiki-0007.md", "/vault/wiki/wiki-0003.md"]])
        self.assertEqual(marks.judge, "inferred")
        self.assertRegex(when, r"^\d{4}-\d{2}-\d{2}T")

    def test_verdict_proposed_events_fire_per_note_only_after_a_successful_write(self):
        # The agent's call is visible to the owner the next morning: one verdict_proposed per
        # used/contested note, only when the consumption write itself succeeded — the failure
        # arm is the control group and must leave zero proposals behind.
        transcript = (
            "[user] why did the pool die\n"
            "[assistant] per wiki-0007 instead of wiki-0003. Even so, wiki-0007 is outdated now.\n"
        )
        with (
            mock.patch.dict(os.environ, {"BORING_EVENT_SINK": "db"}),
            mock.patch("ohmyboring.adapters.engine.DrudgeClient") as client,
            mock.patch.object(distill_core.event_log, "append_event") as append_event,
        ):
            distill_core.write_consumption_to_graph("s1", self._records(), transcript)
        proposals = [c for c in append_event.call_args_list if c.args[1] == "verdict_proposed"]
        self.assertEqual(
            [(c.kwargs["note"], c.kwargs["kind"]) for c in proposals],
            [
                ("/vault/wiki/wiki-0007.md", "used"),
                ("/vault/wiki/wiki-0003.md", "used"),
                ("/vault/wiki/wiki-0007.md", "contested"),
            ],
        )
        for call in proposals:
            self.assertEqual((call.args[0], call.args[2]), ("distill", "ok"))
            self.assertEqual(call.kwargs["session_id"], "s1")
            self.assertEqual(call.kwargs["judge"], "inferred")
        # 이유 한 줄: 판정을 만든 문장이 사건에 그대로 실린다 — used 는 이름을 울린 문장,
        # contested 는 표지가 든 문장. 사건에 실리지 않는 변이는 이 단언이 잡는다.
        reasons = {(c.kwargs["note"], c.kwargs["kind"]): c.kwargs["reason"] for c in proposals}
        self.assertEqual(
            reasons[("/vault/wiki/wiki-0007.md", "used")],
            "per wiki-0007 instead of wiki-0003",
        )
        self.assertEqual(
            reasons[("/vault/wiki/wiki-0007.md", "contested")],
            "Even so, wiki-0007 is outdated now",
        )

        append_event.reset_mock()
        with (
            mock.patch.dict(os.environ, {"BORING_EVENT_SINK": "db"}),
            mock.patch("ohmyboring.adapters.engine.DrudgeClient") as client,
            mock.patch.object(distill_core.event_log, "append_event") as append_event,
        ):
            client.return_value.consumption.return_value = Err(Unreachable("down"))
            distill_core.write_consumption_to_graph("s1", self._records(), transcript)
        append_event.assert_not_called()

    def test_a_spooled_sink_never_writes_to_the_live_graph(self):
        transcript = "[user] why\n[assistant] per wiki-0007.\n"
        with (
            mock.patch.dict(os.environ, {"BORING_EVENT_SINK": "spool"}),
            mock.patch("ohmyboring.adapters.engine.DrudgeClient") as client,
        ):
            distill_core.write_consumption_to_graph("s1", self._records(), transcript)
        client.return_value.consumption.assert_not_called()

    def test_nothing_consumed_means_no_call_and_a_dead_engine_does_not_raise(self):
        with (
            mock.patch.dict(os.environ, {"BORING_EVENT_SINK": "db"}),
            mock.patch("ohmyboring.adapters.engine.DrudgeClient") as client,
        ):
            distill_core.write_consumption_to_graph("s1", self._records(), "[assistant] unrelated.\n")
            client.return_value.consumption.assert_not_called()
            client.return_value.consumption.return_value = Err(Unreachable("down"))
            distill_core.write_consumption_to_graph("s1", self._records(), "[assistant] per wiki-0007.\n")


class SessionEndIsRecorded(unittest.TestCase):
    """`distill_resolution` fires on every distillation including mid-session compactions (PRD §3),
    so nothing in the feed meant "a session ended" — which left §2's coverage clause without a
    denominator and "zero uptake rows" unable to say whether any session had finished at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_log = os.environ.get("BORING_EVENT_LOG")
        self.old_sink = os.environ.get("BORING_EVENT_SINK")
        self.old_ledger = os.environ.get("BORING_INJECTION_LEDGER")
        os.environ["BORING_EVENT_LOG"] = os.path.join(self.tmp.name, "events.ndjson")
        os.environ["BORING_EVENT_SINK"] = "spool"
        os.environ["BORING_INJECTION_LEDGER"] = os.path.join(self.tmp.name, "ledger.jsonl")

    def tearDown(self):
        _restore_env("BORING_EVENT_LOG", self.old_log)
        _restore_env("BORING_EVENT_SINK", self.old_sink)
        _restore_env("BORING_INJECTION_LEDGER", self.old_ledger)
        self.tmp.cleanup()

    def _events(self):
        with open(os.environ["BORING_EVENT_LOG"], encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_a_session_with_no_injections_still_says_it_ended(self):
        """The session that received nothing is exactly the one the old early return erased, and
        it belongs in the denominator: the channel could have reached it and did not."""
        distill_core.log_uptake_event("s-empty", "repo", "[assistant] hi\n", "claude-code")
        events = self._events()
        kinds = [e["event"] for e in events]
        self.assertIn("session_end", kinds)
        self.assertNotIn("injection_uptake", kinds, "nothing was injected, so nothing is scored")
        end = next(e for e in events if e["event"] == "session_end")
        self.assertEqual(end["session_id"], "s-empty")
        self.assertEqual(end["injected_prompts"], 0)

    def test_a_scored_session_says_it_ended_too(self):
        import uptake_core

        hits = [
            {
                "source_path": "/vault/wiki/wiki-0007.md",
                "snippet": "the pool died because deadpool recycled a closed socket " * 3,
            }
        ]
        uptake_core.append_record(uptake_core.injection_record("s-scored", "why", hits, 3))
        distill_core.log_uptake_event("s-scored", "repo", "[assistant] per wiki-0007.\n", "claude-code")
        kinds = [e["event"] for e in self._events()]
        self.assertIn("session_end", kinds)
        self.assertIn("injection_uptake", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
