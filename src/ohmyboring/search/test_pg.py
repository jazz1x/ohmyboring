#!/usr/bin/env python3
"""pg.py 통합 시험 — 진짜 pgvector 에 물어 결과로 본다 (질의 문자열이 아니라).

Run: BORING_TEST_DATABASE_URL=postgresql://… python3 ohmyboring/search/test_pg.py
없으면 건颜色 — drudge DB 게이트 관례(BORING_TEST_DATABASE_URL) 그대로 따른다. 일회용 DB 를
쓴다: 별도 스키마(omb_search_test)에 고정 물을 만들고 끝에 떨군다 — public 의 표는 손 안 탄다.

Mutation targets: ranking 의 owner 필터를 빼는 변이(=tally 로 내리는 행 필터)는 여기서
owner 노트의 contested 카운트가 달라져 사망 확인.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ohmyboring.result import Err, Ok  # noqa: E402
from ohmyboring.search import pg  # noqa: E402

DSN = os.environ.get("BORING_TEST_DATABASE_URL")

DIM = 1024


class RelatedSqlShapeTests(unittest.TestCase):
    """SQL 모양 시험 — 살아있는 postgres 없이도 옮긴 규칙을 본다 (가중치 뒤집기·older-than
    빼기 변이는 이 시험에서 사망 — 통합 시험은 CI postgres 있을 때만 돈다)."""

    def test_weights_kinds_and_older_than_are_ported(self):
        sql = pg._RELATED_BY_SHARED_GROUND_SQL
        self.assertIn(
            "2 AS weight FROM edge e JOIN self_claims",
            sql,
            "공유 claim 의 가중치는 2 — store.rs:1218 (가중치↔종류 결합까지 본다)",
        )
        self.assertIn(
            "1 AS weight FROM edge e JOIN self_concepts",
            sql,
            "공유 concept 의 가중치는 1 — store.rs:1223",
        )
        self.assertEqual(sql.count("kind = 'claims'"), 2)
        self.assertEqual(sql.count("kind = 'about'"), 2)
        self.assertIn("dst LIKE 'concept:%%'", sql)
        self.assertIn(
            "od.updated_at < (SELECT updated_at FROM document WHERE source_path = %s)",
            sql,
            "후보는 self 보다 오래된 문서뿐 — store.rs:1241",
        )

    def test_final_order_is_shared_desc_then_source_path_asc(self):
        sql = pg._RELATED_BY_SHARED_GROUND_SQL
        self.assertIn("ORDER BY r.shared DESC, d.source_path ASC", sql)
        ranked_at = sql.index("GROUP BY s.doc_node ORDER BY shared DESC")
        self.assertLess(ranked_at, sql.index("LIMIT %s"), "LIMIT 전 doc_node ASC 는 ranked 단계")


def vec(first: float, second: float = 0.0) -> list[float]:
    out = [0.0] * DIM
    out[0] = first
    out[1] = second
    return out


def _skip_reason() -> str | None:
    if not DSN:
        return "BORING_TEST_DATABASE_URL unset — DB integration test skipped (disposable DB only)"
    return None


@unittest.skipIf(DSN is None, _skip_reason())
class PgIntegrationTests(unittest.TestCase):
    """별도 스키마에 고정 물 — 순수 Python 게이트에는 안 잡히는 SQL 결과를 본다."""

    @classmethod
    def setUpClass(cls):
        import psycopg

        cls.conn = psycopg.connect(DSN, autocommit=True)
        with cls.conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute("DROP SCHEMA IF EXISTS omb_search_test CASCADE;")
            cur.execute("CREATE SCHEMA omb_search_test;")
            cur.execute("SET search_path TO omb_search_test, public;")
            cur.execute(
                "CREATE TABLE document ("
                " source_path text PRIMARY KEY,"
                " author text NOT NULL DEFAULT 'unknown',"
                " project text NOT NULL DEFAULT '',"
                " updated_at timestamptz NOT NULL DEFAULT now(),"
                " sha text NOT NULL DEFAULT '',"
                " tags text[] NOT NULL DEFAULT '{}');"
            )
            cur.execute(
                "CREATE TABLE chunk ("
                " id text PRIMARY KEY,"
                " source_path text NOT NULL,"
                " content text NOT NULL DEFAULT '',"
                " origin text NOT NULL DEFAULT '',"
                " project text NOT NULL DEFAULT '',"
                " kind text NOT NULL DEFAULT '',"
                " chunk_idx integer NOT NULL DEFAULT 0,"
                " tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, content)) STORED,"
                " embedding vector(1024));"
            )
            cur.execute(
                "CREATE TABLE edge ("
                " src text NOT NULL, dst text NOT NULL, kind text NOT NULL, judge text,"
                " PRIMARY KEY (src, dst, kind));"
            )
            cur.execute(
                "CREATE TABLE node ("
                " id text PRIMARY KEY, kind text NOT NULL, label text NOT NULL DEFAULT '', outcome text);"
            )
            cur.execute(
                "CREATE TABLE claim ("
                " subject text NOT NULL, predicate text NOT NULL, value text NOT NULL,"
                " source_path text NOT NULL,"
                " valid_from timestamptz NOT NULL, superseded_at timestamptz,"
                " kind text NOT NULL DEFAULT 'fact', confidence text NOT NULL DEFAULT 'certain',"
                " said_by text);"
            )
            cur.execute(
                "CREATE TABLE query_log ("
                " id serial PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),"
                " endpoint text NOT NULL, query text NOT NULL DEFAULT '',"
                " hit_paths text[] NOT NULL DEFAULT '{}', hit_dists real[] NOT NULL DEFAULT '{}',"
                " hit_dist_kinds text[] NOT NULL DEFAULT '{}', sources text[] NOT NULL DEFAULT '{}',"
                " answer_snippet text NOT NULL DEFAULT '', latency_ms integer);"
            )
            cls._seed(cur)

    @classmethod
    def _seed(cls, cur) -> None:
        docs = [
            ("/own.md", "owner"),
            ("/other.md", "agent:x"),
            ("/plain.md", "unknown"),
            ("/old.md", "unknown"),
        ]
        cur.executemany(
            "INSERT INTO document (source_path, author, updated_at) VALUES (%s, %s, now());", docs
        )
        chunks = [
            ("/own.md#0", "/own.md", "훅 이중 등록 체크아웃 두 개", "personal", "omb"),
            ("/other.md#0", "/other.md", "훅 등록 증류 방법", "personal", "omb"),
            ("/plain.md#0", "/plain.md", "전혀 다른 이야기", "mirror", "other"),
        ]
        cur.executemany(
            "INSERT INTO chunk (id, source_path, content, origin, project, embedding)"
            " VALUES (%s, %s, %s, %s, %s, %s::vector);",
            [
                (c[0], c[1], c[2], c[3], c[4], pg._vector_literal(v))
                for c, v in zip(chunks, [vec(1.0), vec(0.9, 0.1), vec(0.1, 1.0)])
            ],
        )
        edges = [
            ("session:s1", "doc:/own.md", "contested", "agent:x"),  # ranking 에선 안 깎는다
            ("session:s2", "doc:/own.md", "contested", "owner"),  # owner 판정 — 깎는다
            ("session:s3", "doc:/own.md", "used", "agent:x"),  # used 는 누구든
            ("session:s4", "doc:/other.md", "contested", "agent:x"),
            ("doc:/new.md", "doc:/old.md", "supersedes", None),
            ("doc:/own.md", "claim:주제:판정", "claims", None),
            ("doc:/own.md", "claim:주제:다음", "claims", None),
            ("doc:/own.md", "claim:주제:위험", "claims", None),
            ("doc:/own.md", "claim:주제:옛것", "claims", None),
            ("doc:/own.md", "claim:주제:사실", "claims", None),
        ]
        cur.executemany("INSERT INTO edge (src, dst, kind, judge) VALUES (%s, %s, %s, %s);", edges)
        claims = [
            (
                "/own.md",
                "주제",
                "판정",
                "결정한 값",
                "2026-09-20T00:00:00+00:00",
                None,
                "decision",
                "certain",
                "owner",
            ),
            (
                "/own.md",
                "주제",
                "다음",
                "남은 작업 없음",
                "2026-09-21T00:00:00+00:00",
                None,
                "next",
                "certain",
                "owner",
            ),
            (
                "/own.md",
                "주제",
                "위험",
                "고장나는 지점",
                "2026-09-22T00:00:00+00:00",
                None,
                "risk",
                "certain",
                "agent:x",
            ),
            (
                "/own.md",
                "주제",
                "옛것",
                "지난 값",
                "2020-01-01T00:00:00+00:00",
                "2026-09-01T00:00:00+00:00",
                "decision",
                "certain",
                "owner",
            ),
            (
                "/own.md",
                "주제",
                "사실",
                "배경",
                "2026-09-23T00:00:00+00:00",
                None,
                "fact",
                "certain",
                "owner",
            ),
        ]
        cur.executemany(
            "INSERT INTO claim (source_path, subject, predicate, value, valid_from, superseded_at,"
            " kind, confidence, said_by)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);",
            claims,
        )

    @classmethod
    def tearDownClass(cls):
        with cls.conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS omb_search_test CASCADE;")
        cls.conn.close()

    def test_vector_search_cosine_and_filters(self):
        match pg.vector_search(self.conn, vec(1.0), 10, None, None):
            case Ok(hits):
                self.assertEqual([h.id for h in hits], ["/own.md#0", "/other.md#0", "/plain.md#0"])
                self.assertTrue(all(h.dist_kind == "vector_cosine" for h in hits))
                self.assertLess(hits[0].dist, hits[1].dist)
            case Err(e):
                self.fail(f"vector_search: {e}")
        match pg.vector_search(self.conn, vec(1.0), 10, "omb", None):
            case Ok(hits):
                self.assertEqual([h.source_path for h in hits], ["/own.md", "/other.md"])
            case Err(e):
                self.fail(f"project filter: {e}")
        match pg.vector_search(self.conn, vec(1.0), 10, None, 1):
            case Ok(hits):
                self.assertEqual(len(hits), 3)
            case Err(e):
                self.fail(f"since_hours filter: {e}")

    def test_text_search_ts_rank(self):
        match pg.text_search(self.conn, "훅 등록", 10, None, None):
            case Ok(hits):
                # 두 조각 모두 '훅'·'등록' 을 담지만, /other.md 가 토큰 밀도에서 앞선다 —
                # 어느 쪽이 1위든 ranking 신호는 ts_rank 값 자체다.
                self.assertEqual(hits[0].source_path, "/other.md")
                self.assertEqual({h.source_path for h in hits}, {"/own.md", "/other.md"})
                self.assertEqual(hits[0].dist_kind, "text_rank")
            case Err(e):
                self.fail(f"text_search: {e}")

    def test_ranking_feedback_owner_filter_end_to_end(self):
        """(b) pg 쪽 — owner 노트(/own.md)의 agent:x contested 는 ranking 에 안 잡힌다."""
        match pg.ranking_feedback_counts(self.conn, ["/own.md", "/other.md"]):
            case Ok(counts):
                own = counts["/own.md"]
                self.assertEqual((own.used, own.contested), (1, 1), "owner 판정 contested 하나만")
                self.assertEqual(counts["/other.md"].contested, 1)
            case Err(e):
                self.fail(f"ranking_feedback_counts: {e}")

    def test_consumption_counts_every_judge(self):
        match pg.consumption_counts(self.conn, ["/own.md"]):
            case Ok(counts):
                own = counts["/own.md"]
                self.assertEqual((own.used, own.contested), (1, 2), "표시는 judge 불문 전부")
            case Err(e):
                self.fail(f"consumption_counts: {e}")

    def test_said_by_owner_counts_current_only(self):
        match pg.said_by_owner_counts(self.conn, ["/own.md", "/other.md"]):
            case Ok(counts):
                self.assertEqual(counts, {"/own.md": 3}, "owner 가 말한 현재 행 셋 — 대첸 행(옛것)만 빠진다")
            case Err(e):
                self.fail(f"said_by_owner_counts: {e}")

    def test_superseded_by(self):
        match pg.superseded_by(self.conn, ["/old.md", "/own.md"]):
            case Ok(out):
                self.assertEqual(out, {"/old.md": ["/new.md"]})
            case Err(e):
                self.fail(f"superseded_by: {e}")

    def test_rank_facts(self):
        match pg.rank_facts(self.conn, ["/own.md", "/old.md", "/plain.md"]):
            case Ok(facts):
                self.assertTrue(facts["/own.md"].owner)
                self.assertFalse(facts["/plain.md"].owner)
                self.assertTrue(facts["/old.md"].superseded)
                self.assertIsNotNone(facts["/own.md"].updated_at)
            case Err(e):
                self.fail(f"rank_facts: {e}")

    def test_declared_claims_tier_cut_and_canonical_kind(self):
        match pg.declared_claims(self.conn, ["/own.md"], 10):
            case Ok(out):
                rows = out["/own.md"]
                self.assertEqual(
                    [r.predicate for r in rows.rows], ["판정", "다음", "위험"], "fact 는 빠지고 tier 순"
                )
                self.assertEqual(rows.total_matching, 3, "대첸 행·fact 를 뺀 전수")
                self.assertEqual(rows.rows[0].node_id, "claim:주제:판정")
                self.assertEqual(rows.rows[1].kind, "fact", "next 가 '남은 작업 없음' 을 담으면 fact")
                self.assertTrue(rows.rows[0].valid_from.startswith("20"), "RFC 3339")
            case Err(e):
                self.fail(f"declared_claims: {e}")
        match pg.declared_claims(self.conn, ["/own.md"], 1):
            case Ok(out):
                self.assertEqual(len(out["/own.md"].rows), 1)
                self.assertEqual(out["/own.md"].total_matching, 3, "자른 것은 전수로 밝힌다")
            case Err(e):
                self.fail(f"declared_claims per_doc: {e}")

    def test_record_handover_inserts_and_counts_unknown(self):
        match pg.record_handover(self.conn, "s-1", "2026-10-01T00:00:00+00:00", ["/own.md", "/ghost.md"]):
            case Ok(report):
                self.assertEqual((report.handed, report.unknown), (1, 1))
            case Err(e):
                self.fail(f"record_handover: {e}")
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM edge WHERE src = 'session:s-1' AND kind = 'handed';")
            self.assertEqual(cur.fetchone()[0], 1)
        # 멱등 — 같은 경로를 다시 넣어도 간선이 늘지 않는다.
        match pg.record_handover(self.conn, "s-1", "2026-10-01T01:00:00+00:00", ["/own.md"]):
            case Ok(report):
                self.assertEqual(report.handed, 1)
            case Err(e):
                self.fail(f"record_handover again: {e}")
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM edge WHERE src = 'session:s-1' AND kind = 'handed';")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT label FROM node WHERE id = 'session:s-1';")
            self.assertEqual(cur.fetchone()[0], "2026-10-01T01:00:00+00:00")

    def test_log_query_row_shape(self):
        logged = (("/own.md", 0.25, "vector_cosine"), ("/other.md", None, None))
        row = pg.QueryLogRow(
            endpoint="search",
            query="질의",
            logged_hits=logged,
            sources=[],
            answer_snippet="답 일부",
            latency_ms=12,
        )
        match pg.log_query(self.conn, row):
            case Ok(_):
                pass
            case Err(e):
                self.fail(f"log_query: {e}")
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT endpoint, query, hit_paths, hit_dists, hit_dist_kinds, sources,"
                " answer_snippet, latency_ms FROM query_log WHERE query = '질의';"
            )
            row = cur.fetchone()
        self.assertEqual(
            row,
            (
                "search",
                "질의",
                ["/own.md", "/other.md"],
                [0.25, None],
                ["vector_cosine", None],
                [],
                "답 일부",
                12,
            ),
        )

    def test_bad_input_is_err_not_raise(self):
        match pg.vector_search(self.conn, vec(1.0), 10, None, "not-an-int"):
            case Err(_):
                pass
            case Ok(_):
                self.fail("since_hours 가 문자면 psycopg 경계에서 Err 여야 한다")


@unittest.skipIf(DSN is None, _skip_reason())
class RelatedBySharedGroundTests(unittest.TestCase):
    """related_by_shared_ground 고정 물 — PgIntegrationTests 와 스키마를 갈라 vector/text
    시험의 행 수를 더럽히지 않는다."""

    @classmethod
    def setUpClass(cls):
        import psycopg

        cls.conn = psycopg.connect(DSN, autocommit=True)
        with cls.conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS omb_related_test CASCADE;")
            cur.execute("CREATE SCHEMA omb_related_test;")
            cur.execute("SET search_path TO omb_related_test, public;")
            cur.execute(
                "CREATE TABLE document ("
                " source_path text PRIMARY KEY,"
                " author text NOT NULL DEFAULT 'unknown',"
                " project text NOT NULL DEFAULT '',"
                " updated_at timestamptz NOT NULL DEFAULT now(),"
                " sha text NOT NULL DEFAULT '',"
                " tags text[] NOT NULL DEFAULT '{}');"
            )
            cur.execute(
                "CREATE TABLE chunk ("
                " id text PRIMARY KEY,"
                " source_path text NOT NULL,"
                " content text NOT NULL DEFAULT '',"
                " chunk_idx integer NOT NULL DEFAULT 0);"
            )
            cur.execute(
                "CREATE TABLE edge ("
                " src text NOT NULL, dst text NOT NULL, kind text NOT NULL, judge text,"
                " PRIMARY KEY (src, dst, kind));"
            )
            # self(09-20) 기준 — 더 오래된 것만 후보다.
            cur.executemany(
                "INSERT INTO document (source_path, updated_at, tags) VALUES (%s, %s, %s);",
                [
                    ("/rel-self.md", "2026-09-20T00:00:00+00:00", ["self"]),
                    ("/rel-both.md", "2026-09-19T00:00:00+00:00", ["heavy"]),
                    ("/rel-claim.md", "2026-09-18T00:00:00+00:00", []),
                    ("/rel-concept.md", "2026-09-17T00:00:00+00:00", []),
                    ("/rel-new.md", "2026-09-21T00:00:00+00:00", []),
                    ("/rel-a.md", "2026-09-16T00:00:00+00:00", []),
                    ("/rel-z.md", "2026-09-16T00:00:00+00:00", []),
                ],
            )
            cur.executemany(
                "INSERT INTO chunk (id, source_path, content, chunk_idx) VALUES (%s, %s, %s, %s);",
                [
                    ("/rel-self.md#0", "/rel-self.md", "자기 본문", 0),
                    ("/rel-both.md#0", "/rel-both.md", "첫째", 0),
                    ("/rel-both.md#1", "/rel-both.md", "둘째", 1),
                    ("/rel-claim.md#0", "/rel-claim.md", "클 공유", 0),
                    ("/rel-concept.md#0", "/rel-concept.md", "개 공유", 0),
                    ("/rel-new.md#0", "/rel-new.md", "새 노트", 0),
                    ("/rel-a.md#0", "/rel-a.md", "에이", 0),
                    ("/rel-z.md#0", "/rel-z.md", "지", 0),
                ],
            )
            cur.executemany(
                "INSERT INTO edge (src, dst, kind) VALUES (%s, %s, %s);",
                [
                    ("doc:/rel-self.md", "claim:주제:공통", "claims"),
                    ("doc:/rel-self.md", "concept:공통개념", "about"),
                    ("doc:/rel-self.md", "concept:둘째개념", "about"),
                    ("doc:/rel-both.md", "claim:주제:공통", "claims"),
                    ("doc:/rel-both.md", "concept:공통개념", "about"),
                    ("doc:/rel-claim.md", "claim:주제:공통", "claims"),
                    ("doc:/rel-concept.md", "concept:공통개념", "about"),
                    ("doc:/rel-new.md", "claim:주제:공통", "claims"),
                    ("doc:/rel-a.md", "concept:둘째개념", "about"),
                    ("doc:/rel-z.md", "concept:둘째개념", "about"),
                ],
            )

    @classmethod
    def tearDownClass(cls):
        with cls.conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS omb_related_test CASCADE;")
        cls.conn.close()

    def test_weights_older_only_and_final_order(self):
        match pg.related_by_shared_ground(self.conn, "/rel-self.md", 10):
            case Ok(docs):
                self.assertEqual(
                    [d.source_path for d in docs],
                    ["/rel-both.md", "/rel-claim.md", "/rel-a.md", "/rel-concept.md", "/rel-z.md"],
                    "shared DESC(claim 2 + concept 1 합산) 다음 source_path ASC — "
                    "/rel-new 는 self 보다 새 노트라 빠진다",
                )
            case Err(e):
                self.fail(f"related_by_shared_ground: {e}")

    def test_content_is_chunks_joined_by_chunk_idx_and_tags(self):
        match pg.related_by_shared_ground(self.conn, "/rel-self.md", 10):
            case Ok(docs):
                both = docs[0]
                self.assertEqual(both.content, "첫째\n둘째", "chunk 를 chunk_idx 순으로 \\n 으로 잇는다")
                self.assertEqual(both.tags, ["heavy"])
            case Err(e):
                self.fail(f"related_by_shared_ground: {e}")

    def test_limit_applies_in_ranked_before_final_order(self):
        match pg.related_by_shared_ground(self.conn, "/rel-self.md", 1):
            case Ok(docs):
                self.assertEqual([d.source_path for d in docs], ["/rel-both.md"])
            case Err(e):
                self.fail(f"related_by_shared_ground limit 1: {e}")
        match pg.related_by_shared_ground(self.conn, "/rel-self.md", 3):
            case Ok(docs):
                self.assertEqual(
                    [d.source_path for d in docs],
                    ["/rel-both.md", "/rel-claim.md", "/rel-a.md"],
                    "자르는 ranked(doc_node ASC) — 최종 source_path ASC 정렬은 자른 뒤에도 유지",
                )
            case Err(e):
                self.fail(f"related_by_shared_ground limit 3: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
