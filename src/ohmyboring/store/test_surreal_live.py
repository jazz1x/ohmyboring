"""같은 입력을 pg 길과 Surreal 길에 넣으면 같은 답이 나온다(C-SURALL2 Z3, 끝 조건 m) — 진짜 서버가 있을 때만.

환경 변수가 모두 있어야 돈다(없으면 skip — 이 환경에서는 돌리지 못했다):
  BORING_Z3_SURREAL_URL · BORING_Z3_SURREAL_PASSWORD  — 일회용 Surreal (네임스페이스 z3test_<pid> 를 만들고 끝에 지운다)
  BORING_Z3_PG_DSN                                      — 스키마가 깔린 pg. 시험 행은 한 트랜잭션 안에서만 넣고 rollback 한다.
같은 고정 입력을 양쪽에 넣고, 옮긴 가지마다 pg 의 기존 SQL 이 낸 행과 Surreal 길이 낸 행을 비교한다.
시험 행은 주어·술어·프로젝트·경로 접두를 `z3test` 로 잡아 기존 행과 섞이지 않는다.
(audit.stats_surreal 은 전 표 집계라 사본 밖 행과 섞여 여기서 대조하지 않는다 — 별도 표본 대조 필요.)"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]  # `agents.door` 와 `ohmyboring` 둘 다

from ohmyboring.registers import pg as registers  # noqa: E402
from ohmyboring.result import Ok  # noqa: E402
from ohmyboring.store import claims, surreal  # noqa: E402

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

_URL = os.environ.get("BORING_Z3_SURREAL_URL")
_PASSWORD = os.environ.get("BORING_Z3_SURREAL_PASSWORD")
_DSN = os.environ.get("BORING_Z3_PG_DSN")
_READY = bool(_URL and _PASSWORD and _DSN and psycopg)

PROJECT = "z3test-project"
NS = f"z3test_{os.getpid()}"
T0 = datetime(2026, 10, 1, tzinfo=UTC)

#: (subject, predicate, value, source_path, days after T0, sealed, kind, author, era)
CLAIMS = [
    ("z3test-s", "z3test-p", "open newest", "z3test/a.md", 5, False, "rule", "owner", "anchored"),
    ("z3test-s", "z3test-p", "open older", "z3test/b.md", 3, False, "rule", "agent", "unanchored"),
    ("z3test-s", "z3test-p", "sealed row", "z3test/c.md", 1, True, "rule", "agent", "pre-anchor"),
    ("z3test-s", "trigger", "words", "z3test/a.md", 5, False, "rule", "owner", "anchored"),
    ("Z3Test-S", "z3test-p", "split spelling", "z3test/d.md", 4, False, "fact", "agent", "pre-anchor"),
    ("z3test-t", "z3test-p", "ab", "z3test/d.md", 2, False, "decision", "agent", "pre-anchor"),
    ("north-star:" + PROJECT, "goal", "ship it", "z3test/a.md", 6, False, "decision", "owner", "anchored"),
]  # fmt: skip
DOCS = {c[3]: c[7] for c in CLAIMS}  # source_path -> author (a.md 는 owner, 나머지 agent)


def _us(days: int) -> int:
    return int((T0 + timedelta(days=days)).timestamp() * 1_000_000)


@unittest.skipUnless(
    _READY, "BORING_Z3_SURREAL_URL/PASSWORD + BORING_Z3_PG_DSN 필요 — 이 환경에서는 돌리지 못함"
)
class PgAndSurrealAgree(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        settings = surreal.SurrealSettings(_URL, NS, "z3", "root", _PASSWORD)
        cls.client = surreal.SurrealClient(settings)
        ddl = """
            DEFINE TABLE claim_row SCHEMAFULL;
            DEFINE FIELD cid ON claim_row TYPE string; DEFINE FIELD subject ON claim_row TYPE string;
            DEFINE FIELD predicate ON claim_row TYPE string; DEFINE FIELD value ON claim_row TYPE string;
            DEFINE FIELD source_path ON claim_row TYPE string; DEFINE FIELD valid_from_us ON claim_row TYPE int;
            DEFINE FIELD superseded_us ON claim_row TYPE option<int>; DEFINE FIELD kind ON claim_row TYPE string;
            DEFINE FIELD confidence ON claim_row TYPE string; DEFINE FIELD era ON claim_row TYPE string;
            DEFINE FIELD origin ON claim_row TYPE option<string>; DEFINE FIELD project ON claim_row TYPE option<string>;
            DEFINE TABLE document SCHEMAFULL;
            DEFINE FIELD cid ON document TYPE string; DEFINE FIELD source_path ON document TYPE string;
            DEFINE FIELD origin ON document TYPE string; DEFINE FIELD project ON document TYPE string;
            DEFINE FIELD kind ON document TYPE string; DEFINE FIELD author ON document TYPE string;
        """
        seeds = [ddl]
        for i, (s, p, v, path, day, sealed, kind, _a, era) in enumerate(CLAIMS):
            row = {
                "cid": f"c{i}", "subject": s, "predicate": p, "value": v, "source_path": path,
                "valid_from_us": _us(day), "kind": kind, "confidence": "certain", "era": era,
                "origin": "user", "project": PROJECT,
            }  # fmt: skip
            if sealed:
                row["superseded_us"] = _us(day + 1)
            seeds.append(f"CREATE claim_row CONTENT {json.dumps(row)};")
        for path, author in DOCS.items():
            doc = {
                "cid": path,
                "source_path": path,
                "origin": "user",
                "project": PROJECT,
                "kind": "wiki",
                "author": author,
            }
            seeds.append(f"CREATE document CONTENT {json.dumps(doc)};")
        assert isinstance(cls.client.query("".join(seeds)), Ok)

        cls.conn = psycopg.connect(_DSN)
        with cls.conn.cursor() as cur:
            for path, author in DOCS.items():
                cur.execute(
                    "INSERT INTO document (source_path, origin, project, kind, author) VALUES (%s,'user',%s,'wiki',%s)",
                    (path, PROJECT, author),
                )
            for s, p, v, path, day, sealed, kind, _a, era in CLAIMS:
                at = T0 + timedelta(days=day)
                cur.execute(
                    "INSERT INTO claim (subject, predicate, value, source_path, valid_from, superseded_at, kind, era)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (s, p, v, path, at, at + timedelta(days=1) if sealed else None, kind, era),
                )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.rollback()
        cls.conn.close()
        cls.client.query(f"REMOVE NAMESPACE IF EXISTS {NS};")

    def _pg(self, sql: str, params: tuple | dict) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def test_claim_source_rows_agree(self) -> None:
        from agents.door import claim_source

        expected = self._pg(claim_source.SQL, ("z3test-s",))
        got = claims.claim_source_rows(self.client, "z3test-s")
        self.assertEqual(got, Ok(expected))
        # 봉인 행(/c.md)은 양쪽 다 빠진다 — 끝 조건 z: 파이썬 읽기가 Surreal 의 봉인을 본다.
        self.assertNotIn("z3test/c.md", [r[0] for r in expected])

    def test_claim_source_list_rows_agree(self) -> None:
        from agents.door import claim_source

        expected = self._pg(claim_source.LIST_SQL, ("z3test-p",))
        self.assertEqual(claims.claim_source_list_rows(self.client, "z3test-p"), Ok(expected))

    def test_rule_groups_agree(self) -> None:
        from agents.door import rules

        pg_rows = [r for r in self._pg(rules.SQL, ("rule",)) if r[0].startswith("z3test")]
        match claims.rule_rows(self.client, "rule"):
            case Ok(rows):
                got = [r for r in rows if r[0].startswith("z3test")]
            case other:
                self.fail(other)
        self.assertEqual(rules.group_rules(got), rules.group_rules(pg_rows))

    def test_split_subject_rows_agree_on_the_seeded_subjects(self) -> None:
        pg_rows = sorted(
            r
            for r in self._pg("select subject, source_path from claim where superseded_at is null", ())
            if r[0].lower().startswith("z3test")
        )
        match claims.split_subject_rows(self.client):
            case Ok(rows):
                self.assertEqual(sorted(r for r in rows if r[0].lower().startswith("z3test")), pg_rows)
            case other:
                self.fail(other)

    def test_split_samples_agree(self) -> None:
        from agents.door import door

        variants = ["z3test-s", "Z3Test-S"]
        expected = self._pg(door._SPLIT_SAMPLES_SQL, (door._SAMPLE_VALUE_CHARS, variants, door._SAMPLE_LIMIT))
        self.assertEqual(claims.split_samples(self.client, variants), Ok([tuple(r) for r in expected]))

    def test_recent_register_rows_agree_for_the_seeded_project(self) -> None:
        query = registers.RegisterQuery(10, PROJECT, ("rule", "decision", "fact"), ())
        pg_side = registers.recent_register_rows(self.conn, query)
        surreal_side = registers.recent_register_rows(self.client, query)
        self.assertEqual(pg_side, surreal_side)

    def test_north_star_agrees(self) -> None:
        self.assertEqual(
            registers.north_star_claims(self.conn, PROJECT, ()),
            registers.north_star_claims(self.client, PROJECT, ()),
        )


if __name__ == "__main__":
    unittest.main()
