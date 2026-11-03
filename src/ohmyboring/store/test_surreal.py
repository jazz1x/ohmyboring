"""파이썬 Surreal 읽기 문 시험 — 가짜 서버(httpx.MockTransport)로 와이어·설정·행 모양을 본다.

가짜 서버는 질의문 글자만 보고 미리 정한 행을 준다. 그래서 여기서 잡는 것은 "질의가 무엇을 묻는가"(봉인 행
제외, 조건, 정렬 키)와 "답을 어떻게 접는가"이고, 진짜 Surreal·pg 와 같은 답을 내는지는 test_surreal_live.py 가 본다.
"""

from __future__ import annotations

import json
import sys
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path[:0] = [
    str(Path(__file__).resolve().parents[2])
]  # 훅이 `python3 <파일>` 로 돌려도 ohmyboring 을 찾게

from ohmyboring.registers import pg as registers  # noqa: E402
from ohmyboring.result import Err, Ok  # noqa: E402
from ohmyboring.store import claims, surreal  # noqa: E402
from ohmyboring.sync import audit  # noqa: E402

SETTINGS = surreal.SurrealSettings("http://surreal.test", "boring", "boring", "root", "pw")


def _server(
    answer: Callable[[str], list[list[dict]]], seen: list[str] | None = None
) -> surreal.SurrealClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/signin":
            return httpx.Response(200, json={"token": "t0"})
        body = json.loads(request.content)
        sql = body["params"][0]
        if seen is not None:
            seen.append(sql)
        return httpx.Response(
            200, json={"result": [{"status": "OK", "result": rows} for rows in answer(sql)]}
        )

    return surreal.SurrealClient(SETTINGS, transport=httpx.MockTransport(handle))


class ParseBackend(unittest.TestCase):
    def test_non_surreal_values_read_pg(self) -> None:
        for cfg in ({}, {"vector": {}}, {"vector": {"backend": "pgvector"}}, {"vector": {"backend": "nope"}}):
            self.assertEqual(surreal.parse_backend(cfg, {}), Ok(surreal.OnPg()))

    def test_surreal_reads_url_env_over_file_and_password_from_named_env(self) -> None:
        cfg = {"vector": {"backend": "surreal", "surreal": {"url": "http://file", "password_env": "MY_PW"}}}
        match surreal.parse_backend(cfg, {"MY_PW": "s3", "BORING_SURREAL_URL": "http://env"}):
            case Ok(surreal.OnSurreal(settings)):
                self.assertEqual(
                    (settings.url, settings.password, settings.namespace), ("http://env", "s3", "boring")
                )
            case other:
                self.fail(other)

    def test_surreal_without_password_is_an_error_not_pg(self) -> None:
        match surreal.parse_backend({"vector": {"backend": "surreal"}}, {}):
            case Err(error):
                self.assertIn("BORING_SURREAL_PASSWORD", error.message)
            case other:
                self.fail(other)

    def test_bad_namespace_is_an_error(self) -> None:
        cfg = {"vector": {"backend": "surreal", "surreal": {"namespace": "a b"}}}
        self.assertIsInstance(surreal.parse_backend(cfg, {"BORING_SURREAL_PASSWORD": "x"}), Err)

    def test_settings_repr_hides_the_password(self) -> None:
        self.assertNotIn("pw", repr(SETTINGS))


class Literals(unittest.TestCase):
    def test_text_escapes_quote_backslash_and_control_chars(self) -> None:
        self.assertEqual(surreal.text('a"b\\c\n'), '"a\\"b\\\\c\\u000a"')

    def test_micros_to_time_is_utc(self) -> None:
        self.assertEqual(
            surreal.micros_to_time(1_700_000_000_123_456),
            datetime(2023, 11, 14, 22, 13, 20, 123456, tzinfo=UTC),
        )


class Wire(unittest.TestCase):
    def test_query_sends_ns_db_and_bearer_and_returns_rows_per_statement(self) -> None:
        captured: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if request.url.path == "/signin":
                return httpx.Response(200, json={"token": "t0"})
            return httpx.Response(200, json={"result": [{"status": "OK", "result": [{"n": 1}]}]})

        client = surreal.SurrealClient(SETTINGS, transport=httpx.MockTransport(handle))
        self.assertEqual(client.query("SELECT 1;"), Ok([[{"n": 1}]]))
        rpc = captured[-1]
        self.assertEqual(rpc.headers["Surreal-NS"], "boring")
        self.assertEqual(rpc.headers["Surreal-DB"], "boring")
        self.assertEqual(rpc.headers["Authorization"], "Bearer t0")

    def test_401_signs_in_once_more_and_resends(self) -> None:
        tokens: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/signin":
                return httpx.Response(200, json={"token": f"t{len(tokens)}"})
            tokens.append(request.headers["Authorization"])
            if len(tokens) == 1:
                return httpx.Response(401, text="expired")
            return httpx.Response(200, json={"result": [{"status": "OK", "result": []}]})

        client = surreal.SurrealClient(SETTINGS, transport=httpx.MockTransport(handle))
        self.assertEqual(client.query("SELECT 1;"), Ok([[]]))
        self.assertEqual(len(tokens), 2)

    def test_a_statement_that_is_not_ok_fails_the_whole_call(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/signin":
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(200, json={"result": [{"status": "ERR", "result": "parse error"}]})

        client = surreal.SurrealClient(SETTINGS, transport=httpx.MockTransport(handle))
        match client.query("x"):
            case Err(error):
                self.assertIn("parse error", error.message)
            case other:
                self.fail(other)

    def test_unreachable_server_is_an_err_value(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        client = surreal.SurrealClient(SETTINGS, transport=httpx.MockTransport(handle))
        self.assertIsInstance(client.query("x"), Err)


class ClaimReads(unittest.TestCase):
    def test_claim_source_asks_only_open_rows_and_joins_the_author(self) -> None:
        seen: list[str] = []

        def answer(sql: str) -> list[list[dict]]:
            if "FROM claim_row" in sql:
                return [[{"subject": "s", "value": "v", "source_path": "/a.md", "valid_from_us": 1_000_000}]]
            return [[{"source_path": "/a.md", "author": "owner"}]]

        rows = claims.claim_source_rows(_server(answer, seen), "s")
        self.assertIn("superseded_us = NONE", seen[0])
        self.assertEqual(rows, Ok([("/a.md", datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC), "v", "owner")]))

    def test_a_note_without_a_document_row_is_unknown(self) -> None:
        def answer(sql: str) -> list[list[dict]]:
            if "FROM claim_row" in sql:
                return [[{"subject": "s", "value": "v", "source_path": "/a.md", "valid_from_us": 0}]]
            return [[]]

        match claims.claim_source_rows(_server(answer), "s"):
            case Ok([(_, _, _, author)]):
                self.assertEqual(author, "unknown")
            case other:
                self.fail(other)

    def test_split_samples_counts_picks_longest_value_and_ranks_by_rows(self) -> None:
        rows = [
            {"predicate": "p1", "value": "ab"},
            {"predicate": "p1", "value": "abc"},
            {"predicate": "p1", "value": "abd"},
            {"predicate": "p2", "value": "x" * 200},
        ]
        got = claims.split_samples(_server(lambda sql: [rows]), ["v1", "v2"])
        self.assertEqual(got, Ok([("p1", 3, "abc"), ("p2", 1, "x" * 160)]))

    def test_rule_rows_keep_newest_first_inside_a_subject(self) -> None:
        rows = [
            {"subject": "b", "predicate": "rule", "value": "new", "source_path": "/2", "valid_from_us": 2},
            {"subject": "a", "predicate": "rule", "value": "x", "source_path": "/3", "valid_from_us": 1},
            {"subject": "b", "predicate": "rule", "value": "old", "source_path": "/1", "valid_from_us": 1},
        ]
        got = claims.rule_rows(_server(lambda sql: [rows]), "rule")
        self.assertEqual(
            got,
            Ok([("a", "rule", "x", "/3"), ("b", "rule", "new", "/2"), ("b", "rule", "old", "/1")]),
        )


class RegisterReads(unittest.TestCase):
    ROW = {
        "subject": "s", "predicate": "p", "value": "v", "kind": "decision", "confidence": "certain",
        "valid_from_us": 5_000_000, "project": "proj", "source_path": "/a.md",
    }  # fmt: skip

    def test_recent_rows_ask_open_claims_with_policy_and_report_the_total(self) -> None:
        seen: list[str] = []
        client = _server(lambda sql: [[self.ROW], [{"n": 7}]], seen)
        query = registers.RegisterQuery(1, "proj", ("decision",), ("company",))
        match registers.recent_register_rows(client, query):
            case Ok(found):
                self.assertEqual((len(found.rows), found.total_matching), (1, 7))
                self.assertEqual(found.rows[0].project, "proj")
            case other:
                self.fail(other)
        for needle in (
            "superseded_us = NONE",
            'project = "proj"',
            'kind IN ["decision"]',
            '!(origin IN ["company"])',
        ):
            self.assertIn(needle, seen[0])

    def test_stalled_keeps_the_oldest_row_of_each_note(self) -> None:
        a_old = {**self.ROW, "valid_from_us": 1}
        a_new = {**self.ROW, "valid_from_us": 2, "subject": "s2"}
        b = {**self.ROW, "valid_from_us": 3, "source_path": "/b.md"}
        client = _server(lambda sql: [[a_old, a_new, b]])
        match registers.stalled_register_rows(
            client, registers.RegisterQuery(10, None, None, (), older_than_days=3)
        ):
            case Ok(found):
                self.assertEqual((len(found.rows), found.total_matching), (2, 2))
                self.assertEqual(found.rows[0].subject, "s")
            case other:
                self.fail(other)

    def test_north_star_keeps_the_newest_row_per_predicate(self) -> None:
        newer = {**self.ROW, "predicate": "goal", "value": "new"}
        older = {**self.ROW, "predicate": "goal", "value": "old"}
        check = {**self.ROW, "predicate": "check", "value": "c"}
        client = _server(lambda sql: [[newer, older, check]])
        match registers.north_star_claims(client, "proj", ()):
            case Ok(rows):
                self.assertEqual([(r.predicate, r.value) for r in rows], [("goal", "new"), ("check", "c")])
            case other:
                self.fail(other)

    def test_recurrences_and_status_answer_an_err_not_pg(self) -> None:
        client = _server(lambda sql: [[]])
        self.assertIsInstance(registers.recurrence_rows(client, 30, None, 10), Err)
        self.assertIsInstance(registers.status_docs(client, "p"), Err)
        self.assertIsInstance(registers.status_claims(client, "p", [0.0]), Err)


class AuditReads(unittest.TestCase):
    def test_stats_fold_thirteen_statements_into_the_pg_shape(self) -> None:
        statements = [
            [
                {"source_path": "/a", "origin": "user", "n": 2},
                {"source_path": "/b", "origin": "company", "n": 1},
            ],
            [{"source_path": "/a", "kind": "wiki"}, {"source_path": "/b", "kind": "wiki"}],
            [{"n": 1}],
            [{"n": 2}],
            [{"n": 4}],
            [{"n": 1}],
            [{"kind": "tool", "n": 3}],
            [{"kind": "uses", "n": 5}],
            [{"n": 1}],
            [],
            [{"n": 2}],
            [{"n": 3}],
            [{"n": 6}],
        ]
        match audit.stats_surreal(_server(lambda sql: statements), allow_company=False):
            case Ok(stats):
                self.assertEqual((stats.total_chunks, stats.total_files), (3, 2))
                self.assertEqual(stats.by_origin, {"user": 2, "company": 1})
                self.assertEqual(stats.by_kind, {"wiki": 3})
                self.assertFalse(stats.clean)
                self.assertEqual(stats.nodes["tool"], 3)
                self.assertEqual(stats.nodes["project"], 0)
                self.assertEqual(stats.edges, {"uses": 5, "about": 0})
                self.assertEqual(
                    (stats.claims_anchored, stats.claims_unanchored, stats.claims_pre_anchor), (1, 0, 2)
                )
                self.assertEqual((stats.claims_label_only, stats.claims_stale), (3, 6))
            case other:
                self.fail(other)

    def test_a_chunk_without_a_document_row_is_an_err(self) -> None:
        statements = [[{"source_path": "/a", "origin": "user", "n": 1}], [], *[[] for _ in range(11)]]
        self.assertIsInstance(audit.stats_surreal(_server(lambda sql: statements), allow_company=True), Err)


if __name__ == "__main__":
    unittest.main()
