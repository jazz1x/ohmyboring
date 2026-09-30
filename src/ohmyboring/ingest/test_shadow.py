#!/usr/bin/env python3
"""shadow.py — 순수 함수와 「운영 표 쓰기 0」 게이트.

Run: python3 src/ohmyboring/ingest/test_shadow.py   (no pytest dependency)

쓰기 게이트: shadow.py 소스에 금지 문(insert/update/delete/create/alter/drop/truncate/grant/revoke)
이 하나라도 들어오면 빨강 — 대조 코드는 읽기만 해야 한다(§운영 표 쓰기 0, 시험으로 막음).
고정 변이: 금지 문 하나를 소스에 박는 변이·SQL 상수를 읽기 아닌 것으로 바꾸는 변이 사망 확인.
읽기 전용 변이: _connect 의 SET 문을 뺀 사본에서 ConnectTests 가 빨강 — 가짜 연결이 낸 문장
목록의 첫 자리가 SET 이라는 단언이 잡는다(psycopg 는 sys.modules 에 가짜를 꽂아 경계를 탄다 —
호스트에 psycopg 가 없어도 시험은 돈다).
"""

from __future__ import annotations

import re
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ohmyboring.ingest import shadow  # noqa: E402

_FORBIDDEN = re.compile(r"\b(insert|update|delete|create|alter|drop|truncate|grant|revoke)\b", re.IGNORECASE)


class WriteGateTests(unittest.TestCase):
    def test_shadow_source_has_no_write_statement(self):
        source = Path(shadow.__file__).read_text(encoding="utf-8")
        hits = _FORBIDDEN.findall(source)
        self.assertEqual(hits, [], f"shadow.py 에 운영 표 쓰기 문이 들어왔다: {hits}")

    def test_every_sql_constant_is_a_select(self):
        for name, value in vars(shadow).items():
            if name.startswith("SQL_"):
                self.assertIsInstance(value, str, name)
                self.assertTrue(value.lstrip().upper().startswith("SELECT"), f"{name}: {value}")


class PureFunctionTests(unittest.TestCase):
    def test_cosine_identical_and_orthogonal(self):
        self.assertAlmostEqual(shadow.cosine([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(shadow.cosine([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertAlmostEqual(shadow.cosine([1.0, 0.0], [-1.0, 0.0]), -1.0)
        self.assertEqual(shadow.cosine([0.0, 0.0], [1.0, 0.0]), 0.0, "0 벡터는 0")

    def test_parse_vector_pg_text(self):
        self.assertEqual(shadow.parse_vector("[1,2.5,-3]"), [1.0, 2.5, -3.0])
        self.assertEqual(shadow.parse_vector("[]"), [])

    def test_stride_sample_even_and_bounded(self):
        self.assertEqual(shadow.stride_sample(10, 3), [0, 4, 9])
        picked = shadow.stride_sample(100, 20)
        self.assertEqual(len(picked), 20)
        self.assertEqual(picked, sorted(picked))
        self.assertTrue(all(0 <= i < 100 for i in picked))
        self.assertEqual(shadow.stride_sample(5, 20), [0, 1, 2, 3, 4])
        self.assertEqual(shadow.stride_sample(0, 20), [])

    def test_chunk_diff_count_and_content(self):
        self.assertEqual(shadow.chunk_diff([(0, "a"), (1, "b")], ["a", "b"]), (True, True))
        self.assertEqual(shadow.chunk_diff([(0, "a")], ["a", "b"]), (False, True))
        self.assertEqual(shadow.chunk_diff([(0, "a"), (1, "X")], ["a", "b"]), (True, False))
        self.assertEqual(shadow.chunk_diff([(0, "a"), (2, "b")], ["a", "", "b"]), (False, True))

    def test_chunk_diff_extra_db_chunk(self):
        self.assertEqual(shadow.chunk_diff([(0, "a"), (1, "b"), (2, "c")], ["a", "b"]), (False, False))


class _FakeConnection:
    """psycopg 연결 대역 — 실행한 문장을 적기만 하고 닫는다."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.autocommit = False
        self.statements: list[str] = []

    def execute(self, sql: str) -> None:
        self.statements.append(sql)

    def close(self) -> None:
        pass


class ConnectTests(unittest.TestCase):
    def test_connect_sets_read_only_first(self):
        made: list[_FakeConnection] = []

        def fake_connect(dsn: str) -> _FakeConnection:
            conn = _FakeConnection(dsn)
            made.append(conn)
            return conn

        fake_module = types.ModuleType("psycopg")
        fake_module.connect = fake_connect
        previous = sys.modules.get("psycopg")
        sys.modules["psycopg"] = fake_module
        try:
            conn = shadow._connect("postgresql://example/db")
        finally:
            if previous is None:
                del sys.modules["psycopg"]
            else:
                sys.modules["psycopg"] = previous
        self.assertEqual([c.dsn for c in made], ["postgresql://example/db"])
        self.assertTrue(conn.autocommit, "연결 열자마자 자동 커밋이 걸린다")
        self.assertEqual(
            conn.statements,
            ["SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"],
            "첫 문장은 읽기 전용 고정이어야 한다 — SET 을 빼는 변이가 여기서 빨갛게 끝난다",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
