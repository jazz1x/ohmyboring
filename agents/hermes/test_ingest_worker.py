import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Load the module under test (ingest-worker.py) under a Python-valid name.
_ingest_worker_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ingest-worker.py")
spec = importlib.util.spec_from_file_location("ingest_worker", _ingest_worker_path)
ingest_worker = importlib.util.module_from_spec(spec)
sys.modules["ingest_worker"] = ingest_worker
spec.loader.exec_module(ingest_worker)
Ok, Err = ingest_worker.Ok, ingest_worker.Err


class IngestWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ingest_worker.MARK_DIR = self.tmp.name
        ingest_worker.markers.set_mark_dir(self.tmp.name)

        self._orig_event_log = os.environ.get("BORING_EVENT_LOG")
        self._orig_event_sink = os.environ.get("BORING_EVENT_SINK")
        self._orig_boring_config = os.environ.get("BORING_CONFIG")
        os.environ["BORING_EVENT_LOG"] = str(Path(self.tmp.name) / "events.ndjson")
        os.environ["BORING_EVENT_SINK"] = "spool"
        # Pin the real config (root boring.json — a symlink to the live one) so classify()
        # is deterministic regardless of the machine's ~/oh-my-boring.
        repo_root = Path(__file__).resolve().parent.parent.parent
        os.environ["BORING_CONFIG"] = str(repo_root / "boring.json")
        self.addCleanup(self._restore_env)

        # Default scan root lives under a .claude path so the agent axis maps to claude-code.
        self.sessions_root = str(Path(self.tmp.name) / ".claude" / "projects")

    def _restore_env(self):
        for key, orig in (
            ("BORING_EVENT_LOG", self._orig_event_log),
            ("BORING_EVENT_SINK", self._orig_event_sink),
            ("BORING_CONFIG", self._orig_boring_config),
        ):
            if orig is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = orig

    # ── scan/OFFER path ─────────────────────────────────────────────────────

    def _write_session(self, sid, source_dir=None, text=None, sub="proj"):
        """Write one claude-json session file and return its path."""
        root = Path(source_dir or self.sessions_root)
        d = root / sub
        d.mkdir(parents=True, exist_ok=True)
        body = text if text is not None else "y" * 600
        payload = {"cwd": self.tmp.name, "message": {"role": "user", "content": body}}
        p = d / f"{sid}.jsonl"
        p.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return str(p)

    def _run_main(self, source_dirs=None):
        """Run a full tick with the scan pointed at source_dirs, engine + LLM faked.

        Returns (stdout, distill_run mock). The real queue/markers/event log run against the
        temp MARK_DIR — nothing touches the live queue.
        """
        out = io.StringIO()
        dirs = source_dirs or [self.sessions_root]
        with (
            mock.patch.object(ingest_worker, "_source_dirs", return_value=dirs),
            mock.patch.object(ingest_worker, "MIN_KB", 0),
            mock.patch.object(ingest_worker, "STABLE_AGE_S", 0),
            mock.patch.object(ingest_worker, "_reachable", return_value=Ok(None)),
            mock.patch.object(ingest_worker.distill_run, "distill_and_remember", return_value=True) as run,
            mock.patch.object(ingest_worker.sys, "stdout", out),
        ):
            ingest_worker.main([])
        return out.getvalue(), run

    def _events(self):
        lines = Path(os.environ["BORING_EVENT_LOG"]).read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def _offer_events(self, sid):
        return [e for e in self._events() if e["event"] == "ingest_offer" and e.get("session_id") == sid]

    def test_scan_enqueues_eligible_session_and_drains_same_tick(self):
        self._write_session("s1")
        stdout, run = self._run_main()
        # stdout stays empty — the cron injects it into the agent prompt; an offer
        # must never print a memory-ingest instruction again.
        self.assertEqual(stdout, "")
        # the engine ran the session this same tick and the queue is settled
        self.assertTrue(ingest_worker.markers.is_done("s1"))
        self.assertFalse(ingest_worker.distill_queue.is_queued("s1"))
        run.assert_called_once()
        text, origin, repo, sid = run.call_args.args
        self.assertEqual((sid, origin, repo), ("s1", "personal", ""))
        self.assertIn("y" * 600, text)  # the clamped transcript is what the engine receives
        # the offer is recorded as queued with the same fields the old pending offer carried
        queued = self._offer_events("s1")
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["status"], "queued")
        self.assertEqual(queued[0]["agent"], "claude-code")
        self.assertEqual(queued[0]["origin"], "personal")
        self.assertEqual(queued[0]["repo"], "")
        self.assertGreaterEqual(queued[0]["source_chars"], 500)
        self.assertEqual(queued[0]["emitted_chars"], len(text))
        self.assertFalse(queued[0]["clamped"])
        self.assertEqual(queued[0]["workflow_node"], "transcript_prepared")
        # the processing result rides the existing ingest_queue event
        drained = [e for e in self._events() if e["event"] == "ingest_queue" and e.get("session_id") == "s1"]
        self.assertEqual(drained[-1]["status"], "ok")

    def test_already_done_session_is_not_reoffered(self):
        self._write_session("s-done")
        ingest_worker.markers.mark_done("s-done")
        stdout, run = self._run_main()
        self.assertEqual(stdout, "")
        run.assert_not_called()
        self.assertFalse(ingest_worker.distill_queue.is_queued("s-done"))
        self.assertEqual(self._offer_events("s-done"), [])

    def test_already_queued_session_is_not_reoffered(self):
        self._write_session("s-q")
        dq = ingest_worker.distill_queue
        dq.enqueue(dq.QueueItem("s-q", "claude-code", "personal", "repo", "preset-text"))
        # Age the pending marker past PENDING_TTL — enqueue() rewrote it fresh, and a fresh
        # pending alone would make _eligible reject. Only a correct is_queued() check blocks
        # the re-offer, so deleting that check must turn this test red.
        pending = Path(self.tmp.name) / "s-q.pending"
        stale = time.time() - ingest_worker.PENDING_TTL - 1
        os.utime(pending, (stale, stale))
        stdout, run = self._run_main()
        # the drain ran the pre-queued item; the scan did not overwrite it
        run.assert_called_once_with("preset-text", "personal", "repo", "s-q")
        self.assertEqual(self._offer_events("s-q"), [])

    def test_scan_tops_up_queue_to_one_ticks_worth(self):
        # The queue already holds 2 of this tick's budget of 3 → the scan may add exactly 1.
        # Without the cap the scan would enqueue all three candidates, and the drain (3 per
        # tick) would leave the rest waiting past PENDING_TTL.
        for i in range(2):
            self._queue(f"pre-{i}")
        for sid in ("cap-a", "cap-b", "cap-c"):
            self._write_session(sid)
        with mock.patch.object(ingest_worker, "QUEUE_PER_TICK", 3):
            stdout, run = self._run_main()
        queued = [e for e in self._events() if e["event"] == "ingest_offer" and e.get("status") == "queued"]
        self.assertEqual([e["session_id"] for e in queued], ["cap-a"])  # oldest first
        self.assertEqual(run.call_count, 3)  # 2 pre-queued + 1 offered, all drained this tick
        for sid in ("cap-b", "cap-c"):
            self.assertFalse(ingest_worker.markers.is_done(sid))
            self.assertFalse(ingest_worker.distill_queue.is_queued(sid))
            self.assertEqual(self._offer_events(sid), [])

    def test_dead_session_does_not_eat_a_tick_slot(self):
        # A dead session sits in the window ahead of an eligible one and QUEUE_PER_TICK=1:
        # enqueue() would refuse the dead session anyway, but the offer loop would still count
        # it against this tick's budget and log a false "queued" — the eligible session behind
        # it must not starve.
        dead_path = self._write_session("s-dead")
        live_path = self._write_session("s-live")
        older = time.time() - 200
        newer = time.time() - 100
        os.utime(dead_path, (older, older))
        os.utime(live_path, (newer, newer))
        dead_marker = Path(self.tmp.name) / "s-dead.dead"
        dead_marker.write_text("0")
        with mock.patch.object(ingest_worker, "QUEUE_PER_TICK", 1):
            stdout, run = self._run_main()
        self.assertEqual(stdout, "")
        # the tick's single slot went to the eligible session: queued and drained this tick
        run.assert_called_once()
        self.assertEqual(run.call_args.args[3], "s-live")
        self.assertTrue(ingest_worker.markers.is_done("s-live"))
        self.assertFalse(ingest_worker.distill_queue.is_queued("s-live"))
        self.assertEqual(len(self._offer_events("s-live")), 1)
        # the dead session got no offer, no queue entry, and its marker is untouched
        self.assertEqual(self._offer_events("s-dead"), [])
        self.assertFalse(ingest_worker.distill_queue.is_queued("s-dead"))
        self.assertTrue(ingest_worker.markers.is_dead("s-dead"))
        self.assertEqual(dead_marker.read_text(encoding="utf-8"), "0")

    def test_offer_ok_reports_real_numbers_when_queue_is_full(self):
        # The queue is full this tick (3 of 3) and one session is eligible — nothing gets
        # offered, but the tick-summary event must still carry the real numbers.
        for i in range(3):
            self._queue(f"full-{i}")
        self._write_session("s-waiting")
        with mock.patch.object(ingest_worker, "QUEUE_PER_TICK", 3):
            stdout, run = self._run_main()
        self.assertEqual(stdout, "")
        ok = [e for e in self._events() if e["event"] == "ingest_offer" and e["status"] == "ok"]
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]["offered"], 0)
        self.assertEqual(ok[0]["eligible"], 1)
        self.assertEqual(ok[0]["room"], 0)
        # the full queue still drained; the waiting session waits for a later tick
        self.assertEqual(run.call_count, 3)
        self.assertFalse(ingest_worker.markers.is_done("s-waiting"))
        self.assertFalse(ingest_worker.distill_queue.is_queued("s-waiting"))
        self.assertEqual(self._offer_events("s-waiting"), [])

    def test_tick_order_is_offer_then_drain(self):
        self._write_session("s-ord")
        stdout, run = self._run_main()
        self.assertFalse(ingest_worker.distill_queue.is_queued("s-ord"))  # drained same tick
        events = self._events()
        offer_idx = next(
            i for i, e in enumerate(events) if e["event"] == "ingest_offer" and e.get("session_id") == "s-ord"
        )
        drain_idx = next(
            i for i, e in enumerate(events) if e["event"] == "ingest_queue" and e.get("session_id") == "s-ord"
        )
        self.assertLess(offer_idx, drain_idx)
        run.assert_called_once()

    def test_too_short_session_is_marked_done_and_not_enqueued(self):
        self._write_session("s-short", text="hi")
        stdout, run = self._run_main()
        self.assertEqual(stdout, "")
        self.assertTrue(ingest_worker.markers.is_done("s-short"))
        self.assertFalse(ingest_worker.distill_queue.is_queued("s-short"))
        run.assert_not_called()
        skipped = self._offer_events("s-short")
        self.assertEqual(skipped[-1]["status"], "skipped")
        self.assertEqual(skipped[-1]["reason"], "too_short")

    def test_scan_maps_agent_name_from_source_dir(self):
        codex_root = str(Path(self.tmp.name) / ".codex" / "sessions")
        self._write_session("s-codex", source_dir=codex_root)
        stdout, run = self._run_main(source_dirs=[codex_root])
        self.assertEqual(stdout, "")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[3], "s-codex")
        self.assertEqual(self._offer_events("s-codex")[-1]["agent"], "codex")
        drained = [
            e for e in self._events() if e["event"] == "ingest_queue" and e.get("session_id") == "s-codex"
        ]
        self.assertEqual(drained[-1]["agent"], "codex")

    def test_scan_records_clamp_when_transcript_is_cut(self):
        self._write_session("s-big", text="z" * 6000)
        stdout, run = self._run_main()
        queued = self._offer_events("s-big")
        self.assertTrue(queued[-1]["clamped"])
        # clamp_text snaps cuts to newline boundaries, so the emitted size is CLAMP plus
        # at most one kept line — well under the unclamped source, and byte-identical to
        # what the engine received.
        self.assertLess(queued[-1]["emitted_chars"], 6000)
        self.assertLessEqual(queued[-1]["emitted_chars"], ingest_worker.CLAMP + 600)
        self.assertEqual(len(run.call_args.args[0]), queued[-1]["emitted_chars"])

    # ── eligibility ─────────────────────────────────────────────────────────

    def test_fresh_retry_marker_is_not_reoffered(self):
        retry = Path(self.tmp.name) / "s-retry.retry"
        retry.write_text("0")
        session = Path(self.tmp.name) / "s-retry.jsonl"
        session.write_text("{}\n")

        self.assertFalse(ingest_worker._eligible(str(session)))

    def test_stale_retry_marker_is_reoffered(self):
        retry = Path(self.tmp.name) / "s-retry.retry"
        retry.write_text("0")
        stale = time.time() - ingest_worker.RETRY_TTL - 1
        os.utime(retry, (stale, stale))
        session = Path(self.tmp.name) / "s-retry.jsonl"
        session.write_text("{}\n")
        stable = time.time() - ingest_worker.STABLE_AGE_S - 1
        os.utime(session, (stable, stable))

        self.assertTrue(ingest_worker._eligible(str(session)))

    def test_unknown_worker_projection_raises(self):
        with self.assertRaises(ValueError):
            ingest_worker._log_worker_event("unknown", "ok")

    # ── drain path ──────────────────────────────────────────────────────────

    def _done_exists(self, sid):
        return (Path(self.tmp.name) / f"{sid}.ts").exists()

    def _retry_exists(self, sid):
        return (Path(self.tmp.name) / f"{sid}.retry").exists()

    def _last_event(self):
        event_path = Path(os.environ["BORING_EVENT_LOG"])
        return json.loads(event_path.read_text(encoding="utf-8").splitlines()[-1])

    def _queue(self, sid):
        distill_queue = ingest_worker.distill_queue
        distill_queue.enqueue(distill_queue.QueueItem(sid, "claude-code", "personal", "repo", "text"))

    def _drain_with(self, result=None, reachable=None):
        reachable = reachable or Ok(None)
        with (
            mock.patch.object(ingest_worker, "_reachable", return_value=reachable),
            mock.patch.object(ingest_worker.distill_run, "distill_and_remember", return_value=result) as run,
        ):
            ingest_worker._drain_queue()
        return run

    def test_a_raising_item_costs_one_try_and_does_not_stop_the_next(self):
        self._queue("q-a-boom")
        self._queue("q-b-good")

        def graph(text, origin, repo, sid):
            if sid == "q-a-boom":
                raise TypeError("expected string or bytes-like object, got 'int'")
            return True

        with (
            mock.patch.object(ingest_worker, "_reachable", return_value=Ok(None)),
            mock.patch.object(ingest_worker.distill_run, "distill_and_remember", side_effect=graph),
        ):
            ingest_worker._drain_queue()

        self.assertEqual(ingest_worker.markers.retry_count("q-a-boom"), 1)
        self.assertTrue(self._done_exists("q-b-good"))
        failed = [e for e in self._events() if e.get("session_id") == "q-a-boom"][-1]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("TypeError", failed["reason"])

    def test_a_session_that_keeps_failing_is_dead_on_the_fifth_try_and_is_not_requeued(self):
        for _ in range(7):
            self._queue("q-loop")
            self._drain_with(False)
        self.assertTrue(ingest_worker.markers.is_dead("q-loop"))
        self.assertFalse(ingest_worker.distill_queue.is_queued("q-loop"))
        runs = [e for e in self._events() if e.get("session_id") == "q-loop"]
        self.assertEqual([e["status"] for e in runs], ["failed"] * 4 + ["dead"])

    def test_an_already_done_session_is_not_distilled_again(self):
        self._queue("q-done")
        ingest_worker.markers.mark_done("q-done")
        run = self._drain_with(True)
        run.assert_not_called()
        self.assertFalse(ingest_worker.distill_queue.is_queued("q-done"))
        self.assertEqual(self._last_event()["status"], "skipped")
        self.assertEqual(self._last_event()["reason"], "already_done")

    def test_an_unreachable_backend_defers_without_spending_a_try(self):
        self._queue("q-down")
        ingest_worker.markers.mark_retry("q-down", reason="earlier")
        run = self._drain_with(True, reachable=Err("llm down"))
        run.assert_not_called()
        self.assertEqual(ingest_worker.markers.retry_count("q-down"), 1)
        self.assertTrue(ingest_worker.distill_queue.is_queued("q-down"))
        self.assertEqual(self._last_event()["status"], "deferred")
        self.assertIn("llm down", self._last_event()["reason"])

    def test_drain_only_prints_nothing_to_stdout(self):
        self._queue("q-now")
        out = io.StringIO()
        with (
            mock.patch.object(ingest_worker, "_reachable", return_value=Ok(None)),
            mock.patch.object(ingest_worker.distill_run, "distill_and_remember", return_value=True),
            mock.patch.object(ingest_worker.sys, "stdout", out),
        ):
            ingest_worker.main(["--drain-only"])
        self.assertEqual(out.getvalue(), "")
        self.assertTrue(self._done_exists("q-now"))

    def test_queue_success_marks_done_and_removes_file(self):
        self._queue("q-ok")
        run = self._drain_with(True)
        run.assert_called_once_with("text", "personal", "repo", "q-ok")
        self.assertTrue(self._done_exists("q-ok"))
        self.assertFalse(ingest_worker.distill_queue.is_queued("q-ok"))
        self.assertEqual(self._last_event()["status"], "ok")
        self.assertEqual(self._last_event()["event"], "ingest_queue")

    def test_queue_failure_marks_retry_and_keeps_file(self):
        self._queue("q-fail")
        self._drain_with(False)
        self.assertTrue(self._retry_exists("q-fail"))
        self.assertTrue(ingest_worker.distill_queue.is_queued("q-fail"))
        self.assertEqual(self._last_event()["status"], "failed")

    def test_queue_dead_removes_file(self):
        self._queue("q-dead")
        with mock.patch.dict(os.environ, {"MARKER_RETRY_MAX_ATTEMPTS": "1"}):
            self._drain_with(False)
        self.assertTrue(ingest_worker.markers.is_dead("q-dead"))
        self.assertFalse(ingest_worker.distill_queue.is_queued("q-dead"))
        self.assertEqual(self._last_event()["status"], "dead")


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

        item = ingest_worker.distill_queue.QueueItem("s-backstop", "claude-code", "personal", "repo", text)
        with (
            mock.patch.object(ingest_worker.llm, "call_llm", fake_llm),
            mock.patch.object(ingest_worker.sys, "stderr", stderr),
        ):
            ingest_worker._distill(item)
        return seen.get("prompt", ""), stderr.getvalue()

    def test_backstop_sits_above_every_caller_default(self):
        # The previous backstop equalled the caller defaults, so raising a caller's clamp was
        # silently undone here. A guard at the same value as the knob it guards is a hidden knob.
        transcript = ingest_worker.transcript
        for accessor in (
            transcript.claude_distill_clamp,
            transcript.codex_distill_clamp,
            transcript.kimi_distill_clamp,
        ):
            self.assertGreater(ingest_worker.BACKSTOP_CLAMP, accessor())

    def test_over_backstop_is_cut_and_announced(self):
        text = "\n".join(f"line {i} " + "x" * 60 for i in range(4000))
        self.assertGreater(len(text), ingest_worker.BACKSTOP_CLAMP)
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
        body = prompt[prompt.find("y" * 200) :]
        self.assertNotIn("y" * 201, body)


if __name__ == "__main__":
    unittest.main()
