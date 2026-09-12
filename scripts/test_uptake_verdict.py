#!/usr/bin/env python3
"""Tests for uptake-verdict.py's machine-readable mode.

Run: python3 scripts/test_uptake_verdict.py

`--json` exists so every consumer reads the verdict's own numbers instead of recomputing them.
That only holds if the channel stays clean and the three outcomes stay distinguishable.
"""
import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
os.environ["BORING_EVENT_SINK"] = "spool"
_spec = importlib.util.spec_from_file_location("uptake_verdict", HERE / "uptake-verdict.py")
uv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uv)


def _event(session, used, total, when="2026-09-13T00:00:00+00:00"):
    return {
        "event": "injection_uptake",
        "observed_at": when,
        "session_id": session,
        "attributes": {
            "agent": "claude-code",
            "used_prompts": used,
            "total_prompts": total,
            "used_control_prompts": 0,
            "used_hits": 0,
            "total_hits": total,
            "used_controls": 0,
            "total_controls": total,
        },
    }


class JsonMode(unittest.TestCase):
    def _run(self, rows, argv=("--json",)):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(uv, "fetch_events", return_value=rows):
            with redirect_stdout(out), redirect_stderr(err):
                code = uv.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_stdout_carries_only_the_payload(self):
        """Advisory prose on stdout is how a machine-readable mode stops being one.

        The unreported-sessions line prints on every run that has aged-out sessions, and it went
        to stdout before this — so the first `--json` consumer would have parsed a Korean sentence.
        """
        rows = [
            _event("a", 0, 10),
            {"event": "injection_unreported", "observed_at": "2026-09-13T00:00:00+00:00",
             "attributes": {"aged_sessions": 2, "aged_rows": 9}},
        ]
        code, out, err = self._run(rows)
        self.assertEqual(code, 0)
        payload = json.loads(out)  # must parse with nothing stripped
        self.assertEqual(payload["unreported"], {"sessions": 2, "prompts": 9})
        self.assertIn("측정 안 된 주입", err, "the human line must still be printed, on stderr")

    def test_an_unreachable_engine_is_not_an_empty_one(self):
        """Exit 2 and `reachable: false`, never a zero count.

        A caller that cannot tell these apart reads absence as a measurement — the failure this
        repo has now found in itself three times in one day.
        """
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(uv, "fetch_events", return_value=None):
            with redirect_stdout(out), redirect_stderr(err):
                code = uv.main(["--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue()), {"reachable": False})

    def test_the_floors_come_from_the_contract_not_from_here(self):
        """The sample floors are pre-registered in verdict_core; this script may not restate them."""
        code, out, _ = self._run([_event("a", 0, 10)])
        payload = json.loads(out)
        self.assertEqual(payload["floors"]["sessions"], uv.verdict_core.MIN_SESSIONS)
        self.assertEqual(payload["floors"]["prompts"], uv.verdict_core.MIN_INJECTED_PROMPTS)

    def test_pre_and_post_repair_stay_separate(self):
        """§8 D9: the window opens after the last repair, so a pre-repair row never reaches it."""
        rows = [
            _event("old", 5, 50, when="2026-09-11T00:00:00+00:00"),
            _event("new", 0, 10, when="2026-09-13T00:00:00+00:00"),
        ]
        _, out, _ = self._run(rows)
        payload = json.loads(out)
        self.assertEqual(payload["pre_repair"], {})
        self.assertEqual(payload["post_repair"]["claude-code"]["sessions"], 1)
        self.assertEqual(payload["post_repair"]["claude-code"]["used_prompts"], 0)


class Midpoint(unittest.TestCase):
    """PRD §2's one-time progress gate, exercised THROUGH THE CLI.

    An earlier version of these called `uv._midpoint()` directly and passed while the command
    returned something else entirely: `main()` hit its "no sample" early return first, so
    MIDPOINT_UNREADABLE was unreachable and the out-of-window silence was broken — before 09-08 an
    empty bucket exited 1 and doctor would have warned daily about a gate that is not due. The
    function was right and the wiring was wrong, which is the only place this gate can be wrong.
    """

    def _cli(self, rows, today, argv=("--midpoint",), url_ok=True):
        err = io.StringIO()
        fetch = (lambda *a, **k: rows) if url_ok else (lambda *a, **k: None)
        with mock.patch.object(uv, "fetch_events", fetch):
            with mock.patch.dict(os.environ, {"BORING_TODAY": today}):
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    code = uv.main(list(argv))
        return code, err.getvalue()

    def _events(self, per_session):
        return [_event(f"s{i}", 0, 10) for i in range(per_session)]

    def test_it_is_silent_outside_its_own_window(self):
        """Before the midpoint it is not due; after the close it is spent — and an empty sample
        must not turn either into noise."""
        thin = self._events(1)
        self.assertEqual(self._cli(thin, "2026-09-13")[0], uv.MIDPOINT_NOT_DUE)
        self.assertEqual(self._cli(thin, "2026-09-27")[0], uv.MIDPOINT_NOT_DUE)
        self.assertEqual(self._cli([], "2026-09-13")[0], uv.MIDPOINT_NOT_DUE)
        self.assertEqual(self._cli(thin, "2026-09-19")[0], uv.MIDPOINT_SHORT)

    def test_nothing_to_read_is_not_a_shortfall(self):
        """No events at all is a failure of observation, not of sample — and it must survive the
        trip through `main()`, which is where it did not."""
        code, out = self._cli([], "2026-09-19")
        self.assertEqual(code, uv.MIDPOINT_UNREADABLE, out)
        self.assertIn("관측 불가", out)

    def test_an_unreachable_engine_never_reads_as_a_shortfall(self):
        code, _ = self._cli([], "2026-09-19", url_ok=False)
        self.assertEqual(code, 2)

    def test_the_floor_is_per_adapter(self):
        """Summing adapters would clear a gate neither of them clears."""
        rows = [_event("a", 0, 10) for _ in range(8)]
        rows += [dict(_event("k", 0, 10), attributes={**_event("k", 0, 10)["attributes"],
                                                      "agent": "kimi"}) for _ in range(3)]
        code, out = self._cli(rows, "2026-09-19")
        self.assertEqual(code, uv.MIDPOINT_SHORT, out)
        self.assertIn("claude-code", out)
        self.assertIn("kimi", out)

    def test_json_and_midpoint_refuse_each_other(self):
        """One answers with a payload, the other with an exit code; dropping either is silent."""
        code, out = self._cli(self._events(1), "2026-09-19", argv=("--json", "--midpoint"))
        self.assertEqual(code, 2)
        self.assertIn("함께 못 쓴다", out)

    def test_not_due_and_met_are_different_exit_codes(self):
        """A shell told them apart by reading Korean prose, so a rephrase could swap them.

        Both mean "no action", which is exactly why they were collapsed — and why collapsing them
        made the message text load-bearing.
        """
        met = [_event(f"s{i}", 0, 10) for i in range(uv.verdict_core.MIDPOINT_MIN_SCORED)]
        self.assertEqual(self._cli(met, "2026-09-19")[0], uv.MIDPOINT_OK)
        self.assertEqual(self._cli(met, "2026-09-13")[0], uv.MIDPOINT_NOT_DUE)
        self.assertNotEqual(uv.MIDPOINT_OK, uv.MIDPOINT_NOT_DUE)

    def test_the_gate_cannot_be_narrowed_to_one_adapter(self):
        """`--agent` would hide another adapter's shortfall — the sum the per-adapter rule forbids,
        arriving through a flag instead of arithmetic."""
        code, out = self._cli(
            self._events(1), "2026-09-19", argv=("--midpoint", "--agent", "claude-code")
        )
        self.assertEqual(code, 2)
        self.assertIn("좁힐 수 없다", out)

    def test_the_window_runs_on_the_owners_calendar_not_utc(self):
        """The briefing fires at 08:00 KST, which is 23:00 UTC the day before.

        Measured against a UTC date, the 09-08 briefing says nothing and the 09-09 one carries the
        warning — handing back a day of a window that is short enough to be watched daily.
        """
        from datetime import datetime, timedelta, timezone as tz

        V = uv.verdict_core
        self.assertEqual(V.WINDOW_TZ.utcoffset(None), timedelta(hours=9))
        run = datetime(2026, 9, 19, 8, 0, tzinfo=V.WINDOW_TZ)
        self.assertEqual(run.astimezone(tz.utc).date().isoformat(), "2026-09-18")
        self.assertEqual(run.date().isoformat(), V.MIDPOINT, "the gate must be due on that run")

    def test_the_unset_path_reads_the_window_zone_not_utc(self):
        """`BORING_TODAY` is a back door, and every other test walks through it.

        That left `datetime.now(WINDOW_TZ)` — the wiring this whole change is about — untouched by
        the suite: swapping it for `timezone.utc` passed everything. The same shape as the defect
        the architect found last round, where the function was right and only the wiring was
        wrong, so it is asserted here without the override.
        """
        from datetime import datetime, timedelta, timezone as tz

        V = uv.verdict_core
        # 23:30 UTC is already the next day in the window's zone. If `window_today` reads UTC it
        # answers with yesterday, which is exactly the day the 08:00 KST briefing would lose.
        late = datetime(2026, 9, 18, 23, 30, tzinfo=tz.utc)

        class _Frozen(datetime):
            @classmethod
            def now(cls, tzinfo=None):
                return late.astimezone(tzinfo) if tzinfo else late

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BORING_TODAY", None)
            with mock.patch.object(V, "datetime", _Frozen):
                self.assertEqual(V.window_today(), "2026-09-19")
                self.assertEqual(late.date().isoformat(), "2026-09-18", "UTC would say yesterday")

    def test_a_malformed_override_falls_back_loudly(self):
        """A back door that turns the measurement off by accident is worse than no back door.

        Measured before this guard: `2026-9-8` — the zero-padding people actually type — plus
        `internal` and `20260908` each compare below MIDPOINT under the lexical comparison, so the
        gate went quiet with no error anywhere. Falls back to the real clock rather than raising,
        because this runs inside the morning briefing: a wrong variable should cost a warning, not
        the day's report.
        """
        V = uv.verdict_core
        real = V.window_today.__wrapped__ if hasattr(V.window_today, "__wrapped__") else None
        for bad in ("2026-9-8", "internal", "20260908", "2026-09-08T00:00:00"):
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"BORING_TODAY": bad}):
                with redirect_stderr(err):
                    got = V.window_today()
            self.assertNotEqual(got, bad, f"{bad!r} must not be taken as a date")
            self.assertIn("YYYY-MM-DD", err.getvalue(), bad)

        with mock.patch.dict(os.environ, {"BORING_TODAY": "2026-09-19"}):
            self.assertEqual(V.window_today(), "2026-09-19", "a well-formed override still works")

    def test_the_dates_are_not_spelled_here(self):
        """The gate reads `verdict_core`, which the PRD-transcription test covers."""
        source = (HERE / "uptake-verdict.py").read_text(encoding="utf-8")
        self.assertNotIn("2026-09-19", source, "the midpoint date is owned by verdict_core")
        self.assertNotIn("2026-09-26", source, "the window close is owned by verdict_core")


class WindowScoping(unittest.TestCase):
    """The window is the population; asking for all of history puts the server's cap between
    the two, and the coverage denominator arrives already truncated."""

    def test_hours_since_covers_the_window_and_never_undercuts_it(self):
        from datetime import datetime

        tz = uv.verdict_core.WINDOW_TZ
        now = datetime(2026, 9, 12, 16, 30, tzinfo=tz)
        hours = uv.hours_since("2026-09-12", now=now)
        self.assertGreaterEqual(hours, 17, "must cover every hour since the window opened")
        self.assertLessEqual(hours, 19, "and not ask for days more than it needs")
        same = uv.hours_since("2026-09-12", now=datetime(2026, 9, 12, 0, 0, tzinfo=tz))
        self.assertGreaterEqual(same, 1, "a window that just opened still asks for a whole hour")
        self.assertIsNone(uv.hours_since("not-a-date"), "an unparsable date scopes nothing")

    def test_main_scopes_its_own_fetch_to_the_window(self):
        """The wiring, not the function. `fetch_events` taking a scope proves nothing if `main`
        calls it without one — that shape has shipped here before (a helper built and its caller
        left unchanged), and the cost is the coverage denominator arriving truncated again."""
        seen = {}

        def _fetch(url, limit, since_hours=None):
            seen["since_hours"] = since_hours
            return []

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(uv, "fetch_events", _fetch):
            with redirect_stdout(out), redirect_stderr(err):
                uv.main([])
        self.assertIsNotNone(seen["since_hours"], "main must scope the fetch to the window")
        self.assertGreaterEqual(seen["since_hours"], 1)

    def test_the_request_carries_the_window_scope(self):
        seen = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"entries": [], "maybe_truncated": False}).encode()

        def _urlopen(url, timeout=0):
            seen.setdefault("urls", []).append(url)
            return _Resp()

        with mock.patch.object(uv.urllib.request, "urlopen", _urlopen):
            uv.fetch_events("http://engine", 5000, since_hours=42)
        self.assertTrue(seen["urls"], "the fetch must have asked for something")
        for url in seen["urls"]:
            self.assertIn("since_hours=42", url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
