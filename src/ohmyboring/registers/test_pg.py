"""레지스터 읽기 동치 시험 — 일회용 Postgres 에 고정 물을 심고, 엔진 SQL(store.rs 정본,
이 파일에 따로 박아 둠)과 파이썬 질의(registers/pg.py)가 같은 행을 내는지 본다.

필터 인자별로 시험을 갖는다 — 어떤 필터를 빼는 변이든 그 인자의 시험이 red 가 된다.
Run: BORING_TEST_DATABASE_URL=postgresql://… python3 ohmyboring/registers/test_pg.py
없으면 건다 — drudge DB 게이트 관례(BORING_TEST_DATABASE_URL) 그대로. 별도 스키마
(omb_registers_test)에 물을 만들고 끝에 떨군다 — public 의 표는 손 안 탄다.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import psycopg  # noqa: E402

from ohmyboring.registers import pg as registers_pg  # noqa: E402

DSN = os.environ.get("BORING_TEST_DATABASE_URL")
SCHEMA = "omb_registers_test"
FROZEN_NOW = "2026-10-04 17:00:00+00"

#: ── 엔진 SQL 정본 (drudge/src/store.rs — 이 시험의 오라클. $n 을 %s 로만 옮겼다) ──
ORACLE_RECENT_REGISTER_ROWS = (
    "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence, c.valid_from, d.project,"
    " COUNT(*) OVER () AS total"
    " FROM claim c"
    " JOIN document d ON d.source_path = c.source_path"
    " WHERE c.superseded_at IS NULL"
    " AND (%s::text IS NULL OR d.project = %s)"
    " AND (%s::text[] IS NULL OR c.kind = ANY(%s))"
    " AND NOT (d.origin = ANY(%s))"
    " AND d.source_path !~ %s"
    " ORDER BY (CASE WHEN length(c.value) < %s THEN 1 ELSE 0 END)"
    " + (CASE WHEN c.predicate ~* %s THEN 1 ELSE 0 END),"
    " c.valid_from DESC"
    " LIMIT %s"
)

ORACLE_STALLED_REGISTER_ROWS = (
    "WITH ranked AS ("
    " SELECT c.subject, c.predicate, c.value, c.kind, c.confidence, c.valid_from, d.project,"
    " ROW_NUMBER() OVER (PARTITION BY c.source_path ORDER BY c.valid_from ASC) AS per_doc"
    " FROM claim c"
    " JOIN document d ON d.source_path = c.source_path"
    " WHERE c.superseded_at IS NULL"
    " AND c.valid_from < (NOW() - INTERVAL '1 day' * (%s::bigint))"
    " AND c.valid_from >= (NOW() - INTERVAL '1 day' * (%s::bigint))"
    " AND (%s::text IS NULL OR d.project = %s)"
    " AND (%s::text[] IS NULL OR c.kind = ANY(%s))"
    " AND NOT (d.origin = ANY(%s))"
    " AND d.source_path !~ %s"
    " )"
    " SELECT subject, predicate, value, kind, confidence, valid_from, project,"
    " COUNT(*) OVER () AS total"
    " FROM ranked"
    " WHERE per_doc = 1"
    " ORDER BY valid_from ASC"
    " LIMIT %s"
)

ORACLE_RECURRENCES = (
    "SELECT a.source_path, a.subject, a.predicate, a.value, a.kind, a.valid_from,"
    " b.source_path, b.subject, b.predicate, b.value, b.kind, b.valid_from,"
    " (a.embedding <=> b.embedding)::float4 AS distance,"
    " (a.valid_from::date - b.valid_from::date)::bigint AS days_apart,"
    " (a.predicate ~* %s) AS label_only"
    " FROM claim a"
    " JOIN claim b ON b.source_path <> a.source_path"
    " JOIN document da ON da.source_path = a.source_path"
    " JOIN document db ON db.source_path = b.source_path"
    " WHERE a.superseded_at IS NULL"
    " AND b.superseded_at IS NULL"
    " AND a.kind IN ('risk', 'blocked')"
    " AND b.kind IN ('risk', 'blocked')"
    " AND a.embedding IS NOT NULL"
    " AND b.embedding IS NOT NULL"
    " AND length(a.value) >= %s"
    " AND length(b.value) >= %s"
    " AND a.valid_from >= (NOW() - INTERVAL '1 day' * (%s::bigint))"
    " AND (a.valid_from::date - b.valid_from::date) >= (%s::bigint)"
    " AND (a.embedding <=> b.embedding) <= %s::real"
    " AND (%s::text IS NULL OR da.project = %s)"
    " AND (%s::text IS NULL OR db.project = %s)"
    " ORDER BY distance ASC, a.valid_from DESC"
)

ORACLE_RECENT_CLAIMS = (
    "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence FROM claim c"
    " JOIN document d ON d.source_path = c.source_path"
    " WHERE c.superseded_at IS NULL"
    " AND (%s::text IS NULL OR d.project = %s)"
    " AND (%s::text[] IS NULL OR c.kind = ANY(%s))"
    " AND NOT (d.origin = ANY(%s))"
    " AND d.source_path !~ %s"
    " ORDER BY (CASE WHEN length(c.value) < %s THEN 1 ELSE 0 END)"
    " + (CASE WHEN c.predicate ~* %s THEN 1 ELSE 0 END),"
    " c.valid_from DESC"
    " LIMIT %s"
)

ORACLE_RECENT_DOCS = (
    "SELECT d.source_path, d.project, d.tags,"
    " string_agg(c.content, E'\\n' ORDER BY c.chunk_idx) AS content"
    " FROM document d"
    " JOIN chunk c ON c.source_path = d.source_path"
    " WHERE NOT (d.origin = ANY(%s))"
    " AND d.updated_at >= now() - make_interval(hours => %s)"
    " AND (%s::text IS NULL OR d.project = %s)"
    " AND d.source_path !~ %s"
    " GROUP BY d.source_path, d.project, d.tags, d.updated_at"
    " ORDER BY d.updated_at DESC"
    " LIMIT %s"
)

ORACLE_CURRENT_CLAIMS = (
    "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence"
    " FROM claim c"
    " JOIN document d ON d.source_path = c.source_path"
    " WHERE c.superseded_at IS NULL AND c.embedding IS NOT NULL"
    " AND c.stale_at IS NULL"
    " AND NOT (d.origin = ANY(%s))"
    " AND (%s::text IS NULL OR d.project = %s)"
    " AND d.source_path !~ %s"
    " ORDER BY c.embedding <=> %s::vector"
    " LIMIT %s"
)


def _skip_reason() -> str:
    return "BORING_TEST_DATABASE_URL unset — skipping disposable-DB parity tests"


def _norm_params(limit: int, project: str | None, kinds, exclude_origins) -> tuple:
    # 오라클 SQL 본문의 %s 순서에 맞춘다 (LIMIT 은 끝 — 엔진의 $1 과 자리가 다르다).
    return (
        project,
        project,
        kinds,
        kinds,
        list(exclude_origins),
        registers_pg.NOT_USER_MEMORY_RE,
        registers_pg.INFORMATIVE_VALUE_CHARS,
        registers_pg.TAUTOLOGICAL_PREDICATES,
        limit,
    )


def _row_tuples(rows: registers_pg.RegisterRows) -> list[tuple]:
    return [
        (r.subject, r.predicate, r.value, r.kind, r.confidence, r.valid_from, r.project) for r in rows.rows
    ]


@unittest.skipIf(DSN is None, _skip_reason())
class RegisterParityTests(unittest.TestCase):
    """엔진 SQL 오라클과 파이썬 질의의 동치 — 필터 인자별 하나씩."""

    @classmethod
    def setUpClass(cls):
        cls.conn = psycopg.connect(DSN, autocommit=True)
        with cls.conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;")
            cur.execute(f"CREATE SCHEMA {SCHEMA};")
            # 고정값 나이(「4 days 6 hours」)와 질의 창이 같은 now() 를 봐야 날짜 차가 실행 시각에 안 흔들린다.
            # 실시계면 UTC 06시 전에 rec-h 의 날짜가 하루 넘어가 co 와 apart 3 이 됐다(2026-10-05 CI).
            cur.execute(
                f"CREATE FUNCTION {SCHEMA}.now() RETURNS timestamptz LANGUAGE sql IMMUTABLE"
                f" AS $$ SELECT timestamptz '{FROZEN_NOW}' $$;"
            )
            cur.execute(f"SET search_path TO {SCHEMA}, pg_catalog, public;")
            cur.execute(
                "CREATE TABLE document ("
                " source_path text PRIMARY KEY,"
                " project text NOT NULL DEFAULT '',"
                " origin text NOT NULL DEFAULT 'personal',"
                " updated_at timestamptz NOT NULL DEFAULT now(),"
                " tags text[] NOT NULL DEFAULT '{}'"
                " );"
            )
            cur.execute(
                "CREATE TABLE claim ("
                " id bigserial PRIMARY KEY,"
                " source_path text NOT NULL REFERENCES document(source_path),"
                " subject text NOT NULL,"
                " predicate text NOT NULL,"
                " value text NOT NULL DEFAULT '',"
                " kind text NOT NULL DEFAULT '',"
                " confidence text NOT NULL DEFAULT '',"
                " valid_from timestamptz NOT NULL DEFAULT now(),"
                " superseded_at timestamptz,"
                " stale_at timestamptz,"
                " embedding vector(4)"
                " );"
            )
            cur.execute(
                "CREATE TABLE chunk ("
                " source_path text NOT NULL REFERENCES document(source_path),"
                " chunk_idx int NOT NULL,"
                " content text NOT NULL,"
                " PRIMARY KEY (source_path, chunk_idx)"
                " );"
            )
            cls._seed(cur)

    @classmethod
    def tearDownClass(cls):
        with cls.conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;")
        cls.conn.close()

    @classmethod
    def _seed(cls, cur) -> None:
        docs = [
            ("wiki/wiki-0001.md", "omb", "personal", "1 day", None),
            ("wiki/wiki-0002.md", "omb", "company", "2 days", None),
            ("wiki/wiki-0003.md", "other", "personal", "3 days", None),
            ("wiki/wiki-0004.md", "", "personal", "4 days", None),
            ("eval/eval-2026-09-01.md", "omb", "personal", "1 day", None),
            ("wiki/daily-brief-2026-09-30.md", "omb", "personal", "1 day", None),
            ("wiki/wiki-0007.md", "omb", "personal", "40 days", None),
        ]
        for path, project, origin, age, _ in docs:
            cur.execute(
                "INSERT INTO document (source_path, project, origin, updated_at)"
                " VALUES (%s, %s, %s, now() - %s::interval)",
                (path, project, origin, age),
            )
        claims = [
            # (doc, subject, predicate, value, kind, confidence, age, superseded, embedding)
            (
                "wiki/wiki-0001.md",
                "subj-dec",
                "decided",
                "migrated the register reads to python code",
                "decision",
                "certain",
                "2 days",
                False,
                None,
            ),
            (
                "wiki/wiki-0001.md",
                "subj-risk",
                "incident",
                "door register shadow mismatch on first run",
                "risk",
                "likely",
                "3 days 6 hours",
                False,
                "[0,1,0,0]",
            ),
            ("wiki/wiki-0001.md", "subj-short", "incident", "short", "risk", "", "4 days", False, None),
            (
                "wiki/wiki-0001.md",
                "subj-next",
                "next step",
                "wire the morning card to the new register path today",
                "next",
                "certain",
                "1 day",
                False,
                None,
            ),
            (
                "wiki/wiki-0001.md",
                "subj-deny",
                "next action",
                "없음",
                "next",
                "certain",
                "1 day",
                False,
                None,
            ),
            (
                "wiki/wiki-0001.md",
                "subj-fact",
                "states",
                "the door binds the loopback interface only",
                "fact",
                "certain",
                "5 days",
                False,
                None,
            ),
            (
                "wiki/wiki-0001.md",
                "subj-term",
                "defines",
                "문(door) 이란 엔진 앞의 프록시 하나다",
                "term",
                "certain",
                "6 days",
                False,
                None,
            ),
            (
                "wiki/wiki-0001.md",
                "subj-block",
                "blocked by",
                "engine container refuses to start on stale compose file",
                "blocked",
                "certain",
                "7 days",
                False,
                None,
            ),
            (
                "wiki/wiki-0001.md",
                "subj-sup",
                "decided",
                "superseded decision that must not show up",
                "decision",
                "certain",
                "8 days",
                True,
                "[1,0,0,0]",
            ),
            (
                "wiki/wiki-0001.md",
                "subj-empty",
                "states",
                "an empty kind row normalizes to fact",
                "",
                "likely",
                "2 days",
                False,
                None,
            ),
            (
                "wiki/wiki-0001.md",
                "subj-stall-new",
                "next step",
                "fresh work that is not stalled yet at all",
                "next",
                "certain",
                "36 hours",
                False,
                None,
            ),
            (
                "wiki/wiki-0001.md",
                "subj-stall-in",
                "next step",
                "stalled inside the seven day window",
                "next",
                "certain",
                "10 days",
                False,
                None,
            ),
            (
                "wiki/wiki-0001.md",
                "subj-stall-old",
                "next step",
                "stalled beyond the thirty day horizon",
                "next",
                "certain",
                "40 days",
                False,
                None,
            ),
            (
                "wiki/wiki-0002.md",
                "subj-co",
                "incident",
                "company origin claim excluded by policy",
                "risk",
                "likely",
                "2 days",
                False,
                "[1,0,0,0]",
            ),
            (
                "wiki/wiki-0002.md",
                "subj-co-stall",
                "next step",
                "company stalled item hidden by policy",
                "next",
                "certain",
                "12 days",
                False,
                None,
            ),
            (
                "wiki/wiki-0003.md",
                "subj-oth",
                "decided",
                "other project decision stays out of the omb filter",
                "decision",
                "certain",
                "2 days 6 hours",
                False,
                None,
            ),
            (
                "wiki/wiki-0003.md",
                "subj-oth-doc",
                "next step",
                "oldest claim of the other doc wins",
                "next",
                "certain",
                "12 days",
                False,
                None,
            ),
            (
                "wiki/wiki-0003.md",
                "subj-oth-doc",
                "next step",
                "second claim of the same doc collapses",
                "next",
                "certain",
                "9 days",
                False,
                None,
            ),
            (
                "wiki/wiki-0003.md",
                "subj-rec-a",
                "incident",
                "the deploy pipeline breaks every other week",
                "risk",
                "likely",
                "5 days 6 hours",
                False,
                "[1,0,0,0]",
            ),
            (
                "wiki/wiki-0004.md",
                "subj-rec-b",
                "incident",
                "the deploy pipeline breaks every other week",
                "risk",
                "likely",
                "20 days",
                False,
                "[1,0,0,0]",
            ),
            (
                "wiki/wiki-0004.md",
                "subj-rec-c",
                "blocked by",
                "ci keeps failing on the same flaky test suite",
                "blocked",
                "likely",
                "25 days",
                False,
                "[0,1,0,0]",
            ),
            (
                "wiki/wiki-0004.md",
                "subj-rec-d",
                "incident",
                "short label",
                "risk",
                "likely",
                "10 days",
                False,
                "[1,0,0,0]",
            ),
            (
                "wiki/wiki-0004.md",
                "subj-rec-e",
                "incident",
                "a newer recurrence without its embedding",
                "risk",
                "likely",
                "4 days",
                False,
                None,
            ),
            (
                "wiki/wiki-0004.md",
                "subj-rec-f",
                "incident",
                "a recurrence older than the days window",
                "risk",
                "likely",
                "45 days",
                False,
                "[1,0,0,0]",
            ),
            (
                "wiki/wiki-0001.md",
                "subj-rec-h",
                "incident",
                "a near clone of the weekly pipeline breakage",
                "risk",
                "likely",
                "4 days 6 hours",
                False,
                "[1,0,0,0]",
            ),
            (
                "wiki/wiki-0003.md",
                "subj-rec-i",
                "incident",
                "short label",
                "risk",
                "likely",
                "8 days",
                False,
                "[1,0,0,0]",
            ),
        ]
        for doc, subject, predicate, value, kind, confidence, age, superseded, embedding in claims:
            cur.execute(
                "INSERT INTO claim (source_path, subject, predicate, value, kind, confidence,"
                " valid_from, superseded_at, embedding)"
                " VALUES (%s, %s, %s, %s, %s, %s, now() - %s::interval, %s, %s::vector)",
                (
                    doc,
                    subject,
                    predicate,
                    value,
                    kind,
                    confidence,
                    age,
                    "now()" if superseded else None,
                    embedding,
                ),
            )
        chunks = [
            ("wiki/wiki-0001.md", 0, "first chunk of wiki-0001"),
            ("wiki/wiki-0001.md", 1, "second chunk of wiki-0001"),
            ("wiki/wiki-0002.md", 0, "company doc chunk"),
            ("wiki/wiki-0003.md", 0, "other project chunk"),
            ("wiki/wiki-0007.md", 0, "stale doc chunk"),
            ("eval/eval-2026-09-01.md", 0, "eval chunk"),
            ("wiki/daily-brief-2026-09-30.md", 0, "brief chunk"),
        ]
        for path, idx, content in chunks:
            cur.execute(
                "INSERT INTO chunk (source_path, chunk_idx, content) VALUES (%s, %s, %s)",
                (path, idx, content),
            )

    # ── decisions ────────────────────────────────────────────────────────────
    def _oracle_register(self, limit, project, kinds, exclude_origins):
        with self.conn.cursor() as cur:
            cur.execute(
                ORACLE_RECENT_REGISTER_ROWS,
                _norm_params(limit, project, list(kinds) if kinds is not None else None, exclude_origins),
            )
            rows = cur.fetchall()
        total = rows[0][7] if rows else 0
        return [row[:7] for row in rows], total

    def _python_register(self, limit=50, project=None, kinds=registers_pg.DECISION_KINDS, exclude_origins=()):
        query = registers_pg.RegisterQuery(limit, project, kinds, tuple(exclude_origins))
        match registers_pg.recent_register_rows(self.conn, query):
            case registers_pg.Ok(rows):
                return rows
            case other:
                self.fail(f"python query failed: {other!r}")

    def test_decisions_no_filter_matches_oracle(self):
        oracle_rows, oracle_total = self._oracle_register(50, None, ["decision"], ())
        rows = self._python_register()
        self.assertEqual(_row_tuples(rows), oracle_rows)
        self.assertEqual(rows.total_matching, oracle_total)
        subjects = {row[0] for row in oracle_rows}
        self.assertEqual(subjects, {"subj-dec", "subj-oth"}, "필터 없음 — omb 와 other 둘 다")

    def test_decisions_project_filter_matches_oracle(self):
        oracle_rows, _ = self._oracle_register(50, "omb", ["decision"], ())
        rows = self._python_register(project="omb")
        self.assertEqual(_row_tuples(rows), oracle_rows)
        self.assertEqual({row[0] for row in oracle_rows}, {"subj-dec"})

    def test_decisions_limit_cut_keeps_total(self):
        oracle_rows, oracle_total = self._oracle_register(50, None, ["decision"], ())
        self.assertGreater(oracle_total, 1, "시험 물이 total 강제를 만들 만큼 커야 한다")
        rows = self._python_register(limit=1)
        oracle_limited, _ = self._oracle_register(1, None, ["decision"], ())
        self.assertEqual(_row_tuples(rows), oracle_limited)
        self.assertEqual(len(rows.rows), 1)
        self.assertEqual(rows.total_matching, oracle_total)

    def test_decisions_policy_excludes_company_origin(self):
        _, oracle_total = self._oracle_register(50, None, ["decision"], ("company",))
        rows = self._python_register(exclude_origins=("company",))
        self.assertEqual(rows.total_matching, oracle_total)
        self.assertNotIn("subj-co", {r.subject for r in rows.rows})

    def test_decisions_excludes_memory_paths_and_superseded(self):
        _, total = self._oracle_register(50, "omb", ["decision"], ())
        rows = self._python_register(project="omb")
        subjects = {r.subject for r in rows.rows}
        self.assertNotIn("subj-sup", subjects, "superseded_at 행은 빠진다")
        self.assertEqual(total, 1)

    def test_decisions_answer_and_sources_render(self):
        match registers_pg.decision_register(self.conn, None, (), 50):
            case registers_pg.Ok(out):
                pass
            case other:
                self.fail(f"decision_register failed: {other!r}")
        oracle_rows, oracle_total = self._oracle_register(50, None, ["decision"], ())
        expected = registers_pg.render_register(
            tuple(registers_pg.RegisterRow(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in oracle_rows),
            len(oracle_rows) < oracle_total,
            oracle_total,
        )
        self.assertEqual(out.answer, expected)
        self.assertEqual(out.sources, sorted({row[0] for row in oracle_rows}))
        # 렌더 계약 칸을 문자열 그대로 고정 — render_register 자체를 변이핧여도 잡히게
        # (기대값을 같은 함수로 만들면 불리언 모양 같은 변이가 살아남는다).
        self.assertEqual(
            out.answer,
            "Showing 2 of 2 matching claims (limit_applied=false).\n"
            "* subj-dec — decided: migrated the register reads to python code (kind=decision, confidence=certain)\n"
            "* subj-oth — decided: other project decision stays out of the omb filter (kind=decision, confidence=certain)",
        )
        self.assertEqual(out.sources, ["subj-dec", "subj-oth"])

    # ── risks / next_actions — 종류 필터 ─────────────────────────────────────
    def test_risks_kinds_filter_matches_oracle(self):
        oracle_rows, _ = self._oracle_register(50, None, ["risk", "assumption", "blocked"], ())
        match registers_pg.risk_register(self.conn, None, (), 50):
            case registers_pg.Ok(out):
                pass
            case other:
                self.fail(f"risk_register failed: {other!r}")
        self.assertEqual(_row_tuples(registers_pg.RegisterRows(out.items, out.total_matching)), oracle_rows)
        subjects = {row[0] for row in oracle_rows}
        self.assertIn("subj-risk", subjects)
        self.assertIn("subj-block", subjects)
        self.assertIn("subj-rec-a", subjects)
        self.assertNotIn("subj-next", subjects, "next 는 risks 에 없다")
        self.assertNotIn("subj-dec", subjects)

    def test_next_actions_kinds_filter_matches_oracle(self):
        oracle_rows, _ = self._oracle_register(50, None, ["next", "blocked"], ())
        match registers_pg.next_action_register(self.conn, None, (), 50):
            case registers_pg.Ok(out):
                pass
            case other:
                self.fail(f"next_action_register failed: {other!r}")
        self.assertEqual(_row_tuples(registers_pg.RegisterRows(out.items, out.total_matching)), oracle_rows)
        subjects = {row[0] for row in oracle_rows}
        self.assertIn("subj-next", subjects)
        self.assertIn("subj-deny", subjects)
        self.assertNotIn("subj-dec", subjects)

    # ── stalled ──────────────────────────────────────────────────────────────
    def _oracle_stalled(self, limit, project, older_than_days, exclude_origins=()):
        with self.conn.cursor() as cur:
            cur.execute(
                ORACLE_STALLED_REGISTER_ROWS,
                (
                    older_than_days,
                    registers_pg.STALE_HORIZON_DAYS,
                    project,
                    project,
                    ["next", "blocked"],
                    ["next", "blocked"],
                    list(exclude_origins),
                    registers_pg.NOT_USER_MEMORY_RE,
                    limit,
                ),
            )
            rows = cur.fetchall()
        total = rows[0][7] if rows else 0
        return [row[:7] for row in rows], total

    def _python_stalled(self, limit=50, project=None, older_than_days=7, exclude_origins=()):
        query = registers_pg.RegisterQuery(
            limit, project, registers_pg.NEXT_ACTION_KINDS, tuple(exclude_origins), older_than_days
        )
        match registers_pg.stalled_register_rows(self.conn, query):
            case registers_pg.Ok(rows):
                return rows
            case other:
                self.fail(f"python stalled query failed: {other!r}")

    def test_stalled_window_matches_oracle(self):
        oracle_rows, oracle_total = self._oracle_stalled(50, None, 7)
        rows = self._python_stalled()
        self.assertEqual(_row_tuples(rows), oracle_rows)
        self.assertEqual(rows.total_matching, oracle_total)
        subjects = {row[0] for row in oracle_rows}
        self.assertIn("subj-stall-in", subjects)
        self.assertIn("subj-oth-doc", subjects)
        self.assertNotIn("subj-stall-new", subjects, "어린 것은 빠진다")
        self.assertNotIn("subj-stall-old", subjects, "지평(30d) 밖은 빠진다")

    def test_stalled_per_doc_collapses_to_oldest(self):
        oracle_rows, _ = self._oracle_stalled(50, None, 7)
        rows = self._python_stalled()
        self.assertEqual(_row_tuples(rows), oracle_rows)
        oth_rows = [row for row in oracle_rows if row[0] == "subj-oth-doc"]
        self.assertEqual(len(oth_rows), 1, "문서당 한 행 — 가장 오래된 claim")

    def test_stalled_project_filter_matches_oracle(self):
        oracle_rows, _ = self._oracle_stalled(50, "omb", 7, ("company",))
        rows = self._python_stalled(project="omb", exclude_origins=("company",))
        self.assertEqual(_row_tuples(rows), oracle_rows)
        self.assertNotIn("subj-oth-doc", {row[0] for row in oracle_rows})

    def test_stalled_limit_matches_oracle(self):
        oracle_rows, oracle_total = self._oracle_stalled(1, None, 7)
        rows = self._python_stalled(limit=1)
        self.assertEqual(_row_tuples(rows), oracle_rows)
        self.assertEqual(rows.total_matching, oracle_total)
        self.assertGreater(oracle_total, 1)

    # ── recurrences ──────────────────────────────────────────────────────────
    def _oracle_recurrences(self, days, project):
        with self.conn.cursor() as cur:
            cur.execute(
                ORACLE_RECURRENCES,
                (
                    registers_pg.TAUTOLOGICAL_PREDICATES,
                    registers_pg.INFORMATIVE_VALUE_CHARS,
                    registers_pg.INFORMATIVE_VALUE_CHARS,
                    days,
                    registers_pg.RECURRENCE_MIN_DAYS_APART,
                    registers_pg.RECURRENCE_MAX_DISTANCE,
                    project,
                    project,
                    project,
                    project,
                ),
            )
            return cur.fetchall()

    def _python_recurrences(self, days=30, project=None, limit=10):
        match registers_pg.recurrence_rows(self.conn, days, project, limit):
            case registers_pg.Ok(rows):
                return rows
            case other:
                self.fail(f"python recurrences failed: {other!r}")

    @staticmethod
    def _pair_multiset(rows) -> list[tuple]:
        return sorted(
            (group.newer.subject, older.subject, round(group.distance, 6))
            for group in rows
            for older in group.older
        )

    @staticmethod
    def _oracle_pair_multiset(oracle) -> list[tuple]:
        return sorted((row[1], row[7], round(float(row[12]), 6)) for row in oracle)

    def test_recurrences_pairs_match_oracle(self):
        oracle = self._oracle_recurrences(30, None)
        rows = self._python_recurrences()
        self.assertEqual(
            {group.newer.subject for group in rows},
            {"subj-co", "subj-rec-a", "subj-rec-h", "subj-risk"},
            "같은 임베딩끼리 cross-doc 짝 — co·rec-a·rec-h([1,0,0,0]) 와 subj-risk→rec-c([0,1,0,0])",
        )
        self.assertEqual(self._pair_multiset(rows), self._oracle_pair_multiset(oracle))
        for group in rows:
            self.assertEqual(
                [older.valid_from for older in group.older],
                sorted(older.valid_from for older in group.older),
                "older 는 오래된 순",
            )
            self.assertTrue(group.label_only, "predicate 'incident' 은 동어 반복 — label_only")
            oracle_aparts = {row[13] for row in oracle if row[1] == group.newer.subject}
            self.assertIn(group.days_apart, oracle_aparts, "그룹의 days_apart 는 그 newer 의 실제 짝 값")
            oracle_olders = {row[7] for row in oracle if row[1] == group.newer.subject}
            self.assertEqual({older.subject for older in group.older}, oracle_olders)

    def test_recurrences_days_window_filter(self):
        rows = self._python_recurrences(days=3)
        oracle = self._oracle_recurrences(3, None)
        self.assertEqual(
            {group.newer.subject for group in rows},
            {"subj-co"},
            "창이 사흘로 좁으면 이틀 된 co 만 남는다 — 엿 본 rec-a·rec-h·subj-risk 는 밖",
        )
        self.assertEqual(self._pair_multiset(rows), self._oracle_pair_multiset(oracle))
        co = rows[0]
        self.assertEqual(
            {older.subject for older in co.older},
            {"subj-rec-a", "subj-rec-b", "subj-rec-f"},
            "date 차이 세는 co→rec-a 도 apart=3 으로 걸러둔다",
        )

    def test_recurrences_days_window_week_keeps_two(self):
        rows = self._python_recurrences(days=4)
        self.assertEqual({group.newer.subject for group in rows}, {"subj-co", "subj-risk"})

    def test_recurrences_project_filter(self):
        rows = self._python_recurrences(project="omb")
        oracle = self._oracle_recurrences(30, "omb")
        self.assertEqual(rows, [], "b 쪽(빈 프로젝트)이 필터에 걸려 omb 는 짝이 없다")
        self.assertEqual(oracle, [])

    def test_recurrences_limit_counts_newers(self):
        limited = self._python_recurrences(limit=1)
        self.assertEqual(len(limited), 1)
        full = self._python_recurrences()
        self.assertIn(limited[0].newer.subject, {g.newer.subject for g in full})

    # ── context ──────────────────────────────────────────────────────────────
    def _oracle_claims(self, limit, project, kinds, exclude_origins=()):
        with self.conn.cursor() as cur:
            cur.execute(
                ORACLE_RECENT_CLAIMS,
                _norm_params(limit, project, list(kinds), exclude_origins),
            )
            return cur.fetchall()

    def test_context_sections_match_oracle(self):
        match registers_pg.context_payload(self.conn, None, (), 5, "ko"):
            case registers_pg.Ok(card):
                pass
            case other:
                self.fail(f"context_payload failed: {other!r}")
        expected = {
            "decisions": ["decision"],
            "risks": ["risk", "assumption", "blocked"],
            "facts": ["fact"],
            "glossary": ["term"],
            "next_actions": ["next", "blocked"],
        }
        for section, kinds in expected.items():
            oracle_rows = self._oracle_claims(5, None, kinds, ())
            got = card[section]
            self.assertEqual(len(got), len(oracle_rows), f"{section} 행 수")
            for item, row in zip(got, oracle_rows, strict=True):
                self.assertEqual(item["subject"], row[0])
                self.assertEqual(item["predicate"], row[1])
                self.assertEqual(item["value"], row[2])
                self.assertEqual(item["kind"], registers_pg._canonical_kind(row[3], row[2]))
                self.assertEqual(item["confidence"], registers_pg._canonical_confidence(row[4]))
        self.assertEqual(card["language"], "ko")

    def test_context_normalizes_empty_and_denial_kinds(self):
        match registers_pg.context_payload(self.conn, None, (), 50, "ko"):
            case registers_pg.Ok(card):
                pass
            case other:
                self.fail(f"context_payload failed: {other!r}")
        next_subjects = {item["subject"]: item for item in card["next_actions"]}
        self.assertEqual(next_subjects["subj-deny"]["kind"], "fact", "작업 부정 값은 fact 로 정규화")
        fact_subjects = {item["subject"] for item in card["facts"]}
        self.assertIn("subj-fact", fact_subjects)
        self.assertNotIn("subj-empty", fact_subjects, "빈 kind 는 SQL 필터(원시 칸)에 안 걸러 fact 가 아니다")

    def test_context_max_items_matches_oracle(self):
        match registers_pg.context_payload(self.conn, None, (), 2, "ko"):
            case registers_pg.Ok(card):
                pass
            case other:
                self.fail(f"context_payload failed: {other!r}")
        for section, kinds in {
            "decisions": ["decision"],
            "risks": ["risk", "assumption", "blocked"],
            "facts": ["fact"],
            "glossary": ["term"],
            "next_actions": ["next", "blocked"],
        }.items():
            oracle_rows = self._oracle_claims(2, None, kinds, ())
            self.assertEqual(len(card[section]), min(2, len(oracle_rows)))

    def test_context_exclude_origins_matches_oracle(self):
        match registers_pg.context_payload(self.conn, None, ("company",), 5, "ko"):
            case registers_pg.Ok(card):
                pass
            case other:
                self.fail(f"context_payload failed: {other!r}")
        risks = {item["subject"] for item in card["risks"]}
        self.assertNotIn("subj-co", risks)
        oracle_rows = self._oracle_claims(5, None, ["risk", "assumption", "blocked"], ("company",))
        self.assertNotIn("subj-co", {row[0] for row in oracle_rows})
        self.assertEqual(len(card["risks"]), len(oracle_rows))

    # ── status ───────────────────────────────────────────────────────────────
    def test_status_docs_match_oracle(self):
        with self.conn.cursor() as cur:
            cur.execute(
                ORACLE_RECENT_DOCS,
                (
                    [],
                    registers_pg.STATUS_SINCE_HOURS,
                    "omb",
                    "omb",
                    registers_pg.NOT_USER_MEMORY_RE,
                    registers_pg.STATUS_DOC_LIMIT,
                ),
            )
            oracle = cur.fetchall()
        match registers_pg.status_docs(self.conn, "omb"):
            case registers_pg.Ok(docs):
                pass
            case other:
                self.fail(f"status_docs failed: {other!r}")
        got = [(d.source_path, d.project, d.tags, d.content) for d in docs]
        self.assertEqual(got, oracle)
        paths = {row[0] for row in oracle}
        self.assertIn("wiki/wiki-0001.md", paths)
        self.assertIn("wiki/wiki-0002.md", paths, "status 는 exclude_origins=[] — company 도 본다")
        self.assertNotIn("wiki/wiki-0007.md", paths, "720h 밖 문서는 빠진다")
        self.assertFalse(any("eval-" in p or "daily-brief-" in p for p in paths))
        wiki1 = next(row for row in oracle if row[0] == "wiki/wiki-0001.md")
        self.assertEqual(wiki1[3], "first chunk of wiki-0001\nsecond chunk of wiki-0001")

    def test_status_claims_match_oracle(self):
        vec = [1.0, 0.0, 0.0, 0.0]
        with self.conn.cursor() as cur:
            cur.execute(
                ORACLE_CURRENT_CLAIMS,
                (
                    [],
                    "omb",
                    "omb",
                    registers_pg.NOT_USER_MEMORY_RE,
                    registers_pg._vector_literal(vec),
                    registers_pg.STATUS_CLAIM_LIMIT,
                ),
            )
            oracle = cur.fetchall()
        match registers_pg.status_claims(self.conn, "omb", vec):
            case registers_pg.Ok(claims):
                pass
            case other:
                self.fail(f"status_claims failed: {other!r}")
        got = [(c.subject, c.predicate, c.value, c.kind, c.confidence) for c in claims]
        self.assertEqual(got, oracle)
        subjects = [row[0] for row in oracle]
        self.assertIn("subj-co", subjects, "company claim 이 먼저(거리 0)")
        self.assertIn("subj-risk", subjects)
        self.assertNotIn("subj-sup", subjects, "superseded 는 빠진다")
        self.assertNotIn("subj-oth", subjects, "project 필터")

    def test_status_payload_empty_path_message(self):
        payload = registers_pg.status_payload(registers_pg.StatusMaterial((), ()), "ghost", "ko")
        self.assertEqual(
            payload, {"answer": "No recent records or claims found for project 'ghost'.", "sources": []}
        )

    def test_status_payload_digest_lists_docs_and_claims(self):
        doc = registers_pg.StatusDoc("wiki/wiki-0001.md", "omb", [], "body line")
        claim = registers_pg.RegisterRow(
            "subj", "pred", "value with enough characters", "decision", "certain"
        )
        payload = registers_pg.status_payload(registers_pg.StatusMaterial((doc,), (claim,)), "omb", "ko")
        self.assertEqual(payload["sources"], ["wiki/wiki-0001.md"])
        self.assertIn("wiki/wiki-0001.md", payload["answer"])
        self.assertIn("body line", payload["answer"])
        self.assertIn("- [decision|certain] subj pred value with enough characters", payload["answer"])


if __name__ == "__main__":
    unittest.main()
