#!/usr/bin/env python3
"""compact 시험 — 실제 Postgres(스크래치)에서만 본다. 가짜 연결은 VACUUM 의 트랜잭션 제약도,
TOAST 색인의 스키마 이름도 흉내 못 낸다.

Run: COMPACT_TEST_ADMIN_DSN=postgresql://u:p@host/postgres python3 src/ohmyboring/compact/test_compact.py
(관리 DSN 의 서버에 시험용 DB 를 만들어 data/schema/baseline.sql 을 얹고 끝나면 지운다. 서버는
allow_system_table_mods=on 이어야 한다 — TOAST 표에 잘못 남은 색인을 만들려면 필요하다.)

1. 90일 경계 — 89일·91일 행 중 91일만 지운다. 변이: interval 을 '89 days' 나 '92 days' 로.
3. _ccnew 정리는 regclass 이름으로 DROP 한다 — TOAST 색인은 pg_toast 스키마라 relname 으로는 못 찾는다.
   변이: SELECT 의 c.oid::regclass::text 를 c.relname 으로.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ohmyboring.compact import run  # noqa: E402
from ohmyboring.sync import schedule  # noqa: E402

_ADMIN_DSN = os.environ.get("COMPACT_TEST_ADMIN_DSN", "")
_DB = "compact_test"
_BASELINE = ROOT / "data" / "schema" / "baseline.sql"
_CCNEW = "SELECT count(*) FROM pg_class WHERE relname LIKE '%\\_ccnew%'"


def _baseline_sql() -> str:
    lines = _BASELINE.read_text().splitlines()
    return "\n".join(line for line in lines if not line.startswith("\\"))


@unittest.skipUnless(_ADMIN_DSN, "COMPACT_TEST_ADMIN_DSN 이 없다 — 실제 Postgres 가 있어야 의미가 있다")
class CompactOnRealPostgres(unittest.TestCase):
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
        self.cur = self.conn.cursor()
        self.addCleanup(self.conn.close)

    def test_prune_keeps_89_days_and_drops_91_days(self) -> None:
        self.cur.execute("DELETE FROM query_log")
        self.cur.execute(
            "INSERT INTO query_log (created_at, endpoint) VALUES"
            " (now() - interval '89 days', 'keep'), (now() - interval '91 days', 'drop')"
        )
        pruned = run.prune_query_log(self.cur)
        self.cur.execute("SELECT endpoint FROM query_log")
        self.assertEqual((1, [("keep",)]), (pruned, self.cur.fetchall()))

    def test_invalid_ccnew_indexes_are_dropped_including_the_toast_one(self) -> None:
        self.cur.execute("SELECT reltoastrelid::regclass::text FROM pg_class WHERE relname = 'claim'")
        toast = self.cur.fetchone()[0]
        self.cur.execute("CREATE INDEX claim_subject_ccnew1 ON claim (subject)")
        # TOAST 표에는 SQL 로 색인을 못 만든다 — 일반 색인을 만든 뒤 카탈로그에서 TOAST 표 소속·pg_toast 스키마로 옮긴다.
        toast_index = f"{toast.split('.')[1]}_index_ccnew1"
        self.cur.execute(f"CREATE INDEX {toast_index} ON claim (subject)")
        self.cur.execute(
            "UPDATE pg_index SET indrelid = %(toast)s::regclass WHERE indexrelid = %(idx)s::regclass",
            {"toast": toast, "idx": toast_index},
        )
        self.cur.execute(
            "UPDATE pg_class SET relnamespace = 'pg_toast'::regnamespace WHERE oid = %(idx)s::regclass",
            {"idx": toast_index},
        )
        self.cur.execute(
            "UPDATE pg_index SET indisvalid = false"
            " WHERE indexrelid IN (SELECT oid FROM pg_class WHERE relname LIKE '%\\_ccnew%')"
        )
        self.cur.execute(_CCNEW)
        self.assertEqual(2, self.cur.fetchone()[0], "시험 준비: 잘못 남은 색인 둘(일반·TOAST)이 있어야 한다")
        dropped = run.drop_invalid_indexes(self.cur, "claim")
        self.cur.execute(_CCNEW)
        self.assertEqual((2, 0), (dropped, self.cur.fetchone()[0]))

    def test_compact_waits_while_another_connection_holds_the_run_lock(self) -> None:
        deps = run.CompactDeps(
            connect=lambda: psycopg.connect(self.dsn, autocommit=True), append_event=lambda *a, **k: True
        )
        started, finished = threading.Event(), threading.Event()

        def compact_round() -> None:
            started.set()
            run.run_compact(deps)
            finished.set()

        with schedule.run_lock(lambda: psycopg.connect(self.dsn)):
            worker = threading.Thread(target=compact_round, daemon=True)
            worker.start()
            self.assertTrue(started.wait(5))
            self.assertFalse(finished.wait(1.5), "잠금을 쥔 동안 다른 연결의 compact 가 돌면 안 된다")
        self.assertTrue(finished.wait(30), "잠금이 풀리면 compact 가 이어서 돌아야 한다")
        worker.join(5)


if __name__ == "__main__":
    unittest.main()
