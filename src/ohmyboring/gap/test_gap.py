#!/usr/bin/env python3
"""gap 경계 파싱과 쓰기 SQL — 가짜 커서로 SQL·인자를 단언한다 (살아 있는 DB 없이)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import psycopg  # noqa: E402

from ohmyboring.gap import parse, pg  # noqa: E402
from ohmyboring.result import Err, Ok  # noqa: E402


def _args(**over) -> dict:
    base = {"session_id": "s-1", "query": "how do we deploy", "kind": "missing"}
    return {**base, **over}


def _rejected(arguments: dict) -> str:
    match parse.parse(arguments):
        case Err(rejected):
            return rejected.message
        case Ok(value):
            raise AssertionError(f"accepted: {value}")


class ParseTests(unittest.TestCase):
    def test_accepts_each_kind_and_defaults_handed(self):
        for kind in ("missing", "broken"):
            match parse.parse(_args(kind=kind)):
                case Ok(args):
                    self.assertEqual((args.kind.value, args.handed), (kind, ()))
                case Err(rejected):
                    self.fail(rejected.message)

    def test_stale_with_handed_is_accepted_and_deduped(self):
        match parse.parse(_args(kind="stale", handed=["/a.md", "/a.md", "/b.md"])):
            case Ok(args):
                self.assertEqual(args.handed, ("/a.md", "/b.md"))
            case Err(rejected):
                self.fail(rejected.message)

    def test_kind_outside_the_three_is_rejected(self):
        for bad in ("gone", "", None, ["missing"]):
            self.assertEqual(_rejected(_args(kind=bad)), "kind must be one of missing, stale, broken")

    def test_stale_without_handed_is_rejected(self):
        self.assertEqual(_rejected(_args(kind="stale")), "stale gap needs handed notes")
        self.assertEqual(_rejected(_args(kind="stale", handed=[])), "stale gap needs handed notes")

    def test_blank_session_or_query_is_rejected(self):
        for key in ("session_id", "query"):
            for bad in ("", "   ", None, 3):
                self.assertEqual(_rejected(_args(**{key: bad})), f"{key} is required")

    def test_handed_must_be_a_list_of_strings(self):
        for bad in ("/a.md", [1], {"a": 1}):
            self.assertEqual(_rejected(_args(handed=bad)), "handed must be a list of strings")


class _Cursor:
    def __init__(self, known: list[str]):
        self.known = known
        self.calls: list[tuple[str, tuple]] = []
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if sql.startswith("SELECT"):
            self._rows = [(p,) for p in self.known if p in params[0]]

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, known: list[str], fail: bool = False):
        self.cur = _Cursor(known)
        self.commits = 0
        self.fail = fail

    def cursor(self):
        if self.fail:
            raise psycopg.OperationalError("down")
        return self.cur

    def commit(self):
        self.commits += 1


def _record(conn, **over):
    match parse.parse(_args(**over)):
        case Ok(args):
            return pg.record_gap(conn, args, "2026-10-04T00:00:00+00:00")
        case Err(rejected):
            raise AssertionError(rejected.message)


class RecordGapTests(unittest.TestCase):
    def test_writes_node_edge_with_existing_columns_and_counts_unknown(self):
        conn = _Conn(known=["/own.md"])
        match _record(conn, kind="stale", handed=["/own.md", "/ghost.md"]):
            case Ok(report):
                gap = pg.gap_node_id("s-1", "how do we deploy")
                self.assertEqual(report.payload(), {"gap": gap, "kind": "stale", "handed": 1, "unknown": 1})
            case Err(e):
                self.fail(str(e))
        calls = conn.cur.calls
        self.assertEqual(conn.commits, 1)
        gap_node = calls[0]
        self.assertIn("INSERT INTO node (id, kind, label, outcome)", gap_node[0])
        self.assertIn("'gap'", gap_node[0])
        self.assertIn("outcome = EXCLUDED.outcome", gap_node[0])
        self.assertEqual(gap_node[1], (gap, "how do we deploy", "stale"))
        self.assertTrue(gap.startswith("gap:") and len(gap) == len("gap:") + 16)
        edges = [c for c in calls if c[0].startswith("INSERT INTO edge")]
        self.assertEqual(
            [(e[1][0], e[1][1], "'gap_handed'" in e[0]) for e in edges],
            [("session:s-1", gap, False), (gap, "doc:/own.md", True)],
        )
        self.assertIn("'gap'", edges[0][0])
        for sql, _ in calls:
            self.assertNotIn("DELETE", sql)

    def test_same_session_and_query_is_one_node_other_session_is_another(self):
        self.assertEqual(pg.gap_node_id("s-1", "q"), pg.gap_node_id("s-1", "q"))
        self.assertNotEqual(pg.gap_node_id("s-1", "q"), pg.gap_node_id("s-2", "q"))
        self.assertNotEqual(pg.gap_node_id("s-1", "q"), pg.gap_node_id("s-1", "q2"))

    def test_no_handed_makes_only_the_session_edge(self):
        conn = _Conn(known=[])
        match _record(conn):
            case Ok(report):
                self.assertEqual((report.handed, report.unknown), (0, 0))
            case Err(e):
                self.fail(str(e))
        self.assertEqual(len([c for c in conn.cur.calls if c[0].startswith("INSERT INTO edge")]), 1)

    def test_db_failure_is_an_err_value(self):
        match _record(_Conn(known=[], fail=True)):
            case Err(e):
                self.assertIn("down", e.detail)
            case Ok(_):
                self.fail("a dead connection must surface as Err")


if __name__ == "__main__":
    unittest.main()
