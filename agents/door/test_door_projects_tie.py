"""GET /projects?active_days 의 문서 수 동률은 project 사전순(엔진 `order by 2 desc, 1` 과 같다).

실제 Postgres 의 임시 DB 에서 돈다. DOOR_SPLIT_TEST_ADMIN_DSN 이 없으면 건너뛴다.
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
_DB = "door_projects_tie_test"
_BASELINE = ROOT / "data" / "schema" / "baseline.sql"


def _baseline_sql() -> str:
    lines = _BASELINE.read_text().splitlines()
    return "\n".join(line for line in lines if not line.startswith("\\"))


@unittest.skipUnless(_ADMIN_DSN, "DOOR_SPLIT_TEST_ADMIN_DSN 이 없다 — 실제 Postgres 가 있어야 의미가 있다")
class ActiveProjectsTieOrder(unittest.TestCase):
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

    def test_equal_counts_come_back_in_project_name_order(self) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            for i, project in enumerate(["zeta", "mid", "alpha"]):  # 이름 역순으로 넣는다
                for n in range(2):
                    conn.execute(
                        "INSERT INTO document (source_path, project) VALUES (%s, %s)",
                        (f"{project}-{i}-{n}.md", project),
                    )
        with mock.patch.dict(os.environ, {"DOOR_PG_DSN": self.dsn}):
            rows = door._fetch_active_projects(14)
        self.assertEqual(rows, [("alpha", 2), ("mid", 2), ("zeta", 2)])


if __name__ == "__main__":
    unittest.main()
