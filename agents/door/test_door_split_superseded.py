"""이름 합치기(/repairs/split-subjects)는 현재 claim(superseded_at is null)만 보고 지운다.

실제 Postgres 의 임시 DB 에서 돈다 (compact 시험과 같은 방식). DOOR_SPLIT_TEST_ADMIN_DSN 이
없으면 건너뛴다. 운영 DB(boring)에는 쓰지 않는다 — 임시 DB 만 만들고 지운다.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import psycopg
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

door = importlib.import_module("agents.door.door")

_ADMIN_DSN = os.environ.get("DOOR_SPLIT_TEST_ADMIN_DSN", "")
_DB = "door_split_superseded_test"
_BASELINE = ROOT / "data" / "schema" / "baseline.sql"


def _baseline_sql() -> str:
    lines = _BASELINE.read_text().splitlines()
    return "\n".join(line for line in lines if not line.startswith("\\"))


@unittest.skipUnless(_ADMIN_DSN, "DOOR_SPLIT_TEST_ADMIN_DSN 이 없다 — 실제 Postgres 가 있어야 의미가 있다")
class SplitSubjectsIgnoreSuperseded(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with psycopg.connect(_ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {_DB}")
            admin.execute(f"CREATE DATABASE {_DB}")
        cls.dsn = make_conninfo(_ADMIN_DSN, dbname=_DB)
        with psycopg.connect(cls.dsn, autocommit=True) as conn:
            conn.execute(_baseline_sql())

    @classmethod
    def tearDownClass(cls) -> None:
        with psycopg.connect(_ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {_DB}")

    def setUp(self) -> None:
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.conn.close)
        self.conn.execute("DELETE FROM claim")
        self.conn.execute("DELETE FROM document")
        env = mock.patch.dict(os.environ, {"DOOR_PG_DSN": self.dsn})
        env.start()
        self.addCleanup(env.stop)

    def _claim(self, subject: str, path: str, *, superseded: bool) -> None:
        self.conn.execute(
            "INSERT INTO claim (subject, predicate, value, source_path, valid_from, superseded_at)"
            " VALUES (%s, 'p', 'v', %s, now(), CASE WHEN %s THEN now() END)",
            (subject, path, superseded),
        )

    def _rows(self) -> list[tuple[str, bool]]:
        return self.conn.execute(
            "SELECT subject, superseded_at IS NOT NULL FROM claim ORDER BY subject"
        ).fetchall()

    def test_group_with_extra_spelling_only_in_superseded_rows_is_not_a_candidate(self) -> None:
        self._claim("Foo Bar", "n1.md", superseded=False)
        self._claim("foo_bar", "n1.md", superseded=True)
        self._claim("Live One", "n2.md", superseded=False)
        self._claim("live_one", "n2.md", superseded=False)

        subjects = [g["subject"] for g in door._group_split_subjects(door._fetch_split_subject_rows())]

        self.assertEqual(subjects, [door._canon_py("Live One")])

    def test_merge_of_mixed_group_leaves_superseded_row(self) -> None:
        self._claim("Baz Qux", "n3.md", superseded=False)
        self._claim("baz_qux", "n3.md", superseded=False)
        self.conn.execute(
            "INSERT INTO claim (subject, predicate, value, source_path, valid_from, superseded_at)"
            " VALUES ('Baz Qux', 'p', 'old', 'n3.md', now() - interval '1 day', now())"
        )
        self.conn.execute("INSERT INTO document (source_path, sha) VALUES ('n3.md', 'abc')")

        with mock.patch.object(door, "_reread_in_background"):
            result = door._merge_split_subject(door._canon_py("Baz Qux"), door.Standing.OWNER)

        self.assertIsNotNone(result)
        self.assertEqual(self._rows(), [("Baz Qux", True)])


if __name__ == "__main__":
    unittest.main()
