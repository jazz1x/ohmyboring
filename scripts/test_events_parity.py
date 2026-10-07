#!/usr/bin/env python3
"""events-parity 의 순수 함수 시험 — 운영 DSN 단언·재생 묶음·정규화·대조·PASS 판정만. DB·엔진은 안 만진다."""

import importlib.util
import json
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[1] / "src")]
SPEC = importlib.util.spec_from_file_location("events_parity", HERE.with_name("events-parity.py"))
ep = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ep  # dataclass 가 모듈 이름으로 자기 이름공간을 찾는다
SPEC.loader.exec_module(ep)

PROD = "postgresql://boring:s3cret@127.0.0.1:5432/boring"
A = "postgresql://boring:s3cret@127.0.0.1:5433/parity_a"
B = "postgresql://boring:s3cret@127.0.0.1:5433/parity_b"
SK = ep.SK_ANT

#: SEED_SQL 칸 순서의 가짜 행 — id·observed_at 는 재생에서 빠진다.
ROW = (
    42,  # id
    "2026-10-08T01:02:03.12+00:00",  # observed_at
    1728345723000000123,  # time_unix_nano
    "WARN",  # severity_text
    13,  # severity_number
    "door",  # service_name
    "door",  # component
    "recall_shadow",  # event_name
    "ok",  # status
    "trace1",  # trace_id
    "span1",  # span_id
    "run-1",  # run_id
    "sess-1",  # session_id
    "distill",  # workflow
    "node-1",  # workflow_node
    "done",  # workflow_outcome
    '{"event.name": "recall_shadow"}',  # body
    '{"component": "door", "event": "recall_shadow"}',  # attributes
    '{"attributes": {"service.name": "door", "service.namespace": "oh-my-boring"}}',  # resource
)


class DsnGuardTest(unittest.TestCase):
    def test_scratch_pair_passes_and_the_line_names_both_without_a_password(self):
        ok, line = ep.assert_scratch(A, B, {"DOOR_PG_DSN": PROD})
        self.assertTrue(ok)
        self.assertIn("parity_a", line)
        self.assertIn("neither equals production", line)
        self.assertNotIn("s3cret", line)

    def test_production_dsn_in_either_slot_is_refused_even_with_another_password_or_host_alias(self):
        for prod_like in (
            PROD,
            "postgresql://other:pw@localhost:5432/boring",
            "postgresql://u@boring-postgres/boring",
        ):
            self.assertFalse(ep.assert_scratch(prod_like, B, {})[0], prod_like)
            self.assertFalse(ep.assert_scratch(A, prod_like, {})[0], prod_like)

    def test_env_named_production_is_refused_and_same_database_twice_is_refused(self):
        self.assertFalse(ep.assert_scratch(A, B, {"PG_DSN": A})[0])
        self.assertFalse(ep.assert_scratch(A, A, {})[0])


class ReplayBodyTest(unittest.TestCase):
    def test_a_stored_row_replays_as_an_otel_body_with_top_level_id_fields(self):
        body = ep.replay_body(ROW)
        self.assertEqual(body["component"], "door")
        self.assertEqual(body["event"], "recall_shadow")
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["run_id"], "run-1")
        self.assertEqual(body["session_id"], "sess-1")
        self.assertEqual(body["workflow"], "distill")
        self.assertEqual(body["workflow_node"], "node-1")
        self.assertEqual(body["workflow_outcome"], "done")
        self.assertEqual(body["otel"]["event_name"], "recall_shadow")
        self.assertEqual(body["otel"]["severity_text"], "WARN")
        self.assertEqual(body["otel"]["severity_number"], 13)
        self.assertEqual(body["otel"]["time_unix_nano"], 1728345723000000123)
        self.assertEqual(body["otel"]["trace_id"], "trace1")
        self.assertEqual(body["otel"]["span_id"], "span1")
        self.assertEqual(body["otel"]["body"], {"event.name": "recall_shadow"})
        self.assertEqual(body["otel"]["attributes"], {"component": "door", "event": "recall_shadow"})
        self.assertNotIn("service_name", body, "service_name 은 표에서 나오는 칸 — 본문엔 없다")

    def test_null_columns_are_dropped_and_json_columns_parse(self):
        row = list(ROW)
        row[2] = None  # time_unix_nano
        row[9] = None  # trace_id
        row[12] = None  # session_id
        body = ep.replay_body(tuple(row))
        self.assertNotIn("time_unix_nano", body["otel"])
        self.assertNotIn("trace_id", body["otel"])
        self.assertNotIn("session_id", body)
        self.assertEqual(body["otel"]["resource"]["attributes"]["service.name"], "door")


class NormalizeTest(unittest.TestCase):
    def test_strip_entry_drops_id_observed_at_and_otel_observed_timestamp(self):
        entry = {
            "id": 7,
            "observed_at": "2026-10-08T01:02:03+00:00",
            "otel": {"observed_timestamp": "2026-10-08T01:02:03+00:00", "time_unix_nano": 1},
            "event": "recall_shadow",
        }
        stripped = ep.strip_entry(entry)
        self.assertNotIn("id", stripped)
        self.assertNotIn("observed_at", stripped)
        self.assertNotIn("observed_timestamp", stripped["otel"])
        self.assertEqual(stripped["event"], "recall_shadow")
        self.assertEqual(stripped["otel"]["time_unix_nano"], 1)

    def test_normalize_get_strips_every_entry(self):
        body = json.dumps(
            {
                "entries": [
                    {"id": 1, "observed_at": "a", "otel": {"observed_timestamp": "a"}, "event": "x"},
                    {"id": 2, "observed_at": "b", "otel": {"observed_timestamp": "b"}, "event": "y"},
                ],
                "limit_applied": 50,
                "maybe_truncated": True,
            }
        ).encode()
        norm = ep.normalize_get(body)
        self.assertEqual([e["event"] for e in norm["entries"]], ["x", "y"])
        self.assertNotIn("id", norm["entries"][0])
        self.assertEqual((norm["limit_applied"], norm["maybe_truncated"]), (50, True))

    def test_normalize_mcp_success_and_error(self):
        ok = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "…"}],
                    "structuredContent": {"entries": [{"id": 3, "observed_at": "c", "event": "z"}]},
                    "isError": False,
                },
            }
        ).encode()
        norm = ep.normalize_mcp(ok)
        self.assertEqual(norm["id"], 1)
        self.assertEqual(norm["result"]["structuredContent"]["entries"], [{"event": "z"}])
        err = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "since_hours must be >= 0"}}
        ).encode()
        self.assertIsNone(ep.normalize_mcp(err), "오류 봉투는 정규화 대상이 아니다 — rpc_error 가 본다")
        self.assertEqual(ep.rpc_error(ep.Answer(200, err))["code"], -32602)


class AnswersMatchTest(unittest.TestCase):
    def test_post_success_is_byte_compared(self):
        engine = ep.Answer(200, b'{"accepted": 1}')
        self.assertTrue(ep.answers_match(engine, ep.Answer(200, b'{"accepted": 1}'), "post"))
        self.assertFalse(ep.answers_match(engine, ep.Answer(200, b'{"accepted": 2}'), "post"))

    def test_both_5xx_compare_status_and_error_key_only(self):
        engine = ep.Answer(500, b'{"error": "sqlx said disk full"}')
        python = ep.Answer(500, b'{"error": "psycopg said boom"}')
        self.assertTrue(ep.answers_match(engine, python, "post"))
        self.assertFalse(ep.answers_match(engine, ep.Answer(200, b'{"accepted": 1}'), "post"))

    def test_400s_are_byte_compared(self):
        engine = ep.Answer(400, b'{"error": "events batch too large: max 100"}')
        self.assertTrue(
            ep.answers_match(engine, ep.Answer(400, b'{"error": "events batch too large: max 100"}'), "post")
        )
        self.assertFalse(ep.answers_match(engine, ep.Answer(400, b'{"error": "batch too large"}'), "post"))

    def test_get_matches_after_excluded_fields_are_stripped(self):
        def body(observed: str, ident: int) -> bytes:
            return json.dumps(
                {
                    "entries": [
                        {
                            "id": ident,
                            "observed_at": observed,
                            "time_unix_nano": 5,
                            "severity_text": "INFO",
                            "severity_number": 9,
                            "service_name": "door",
                            "component": "door",
                            "event": "recall_shadow",
                            "status": "ok",
                            "trace_id": None,
                            "span_id": None,
                            "run_id": None,
                            "session_id": None,
                            "workflow": None,
                            "workflow_node": None,
                            "workflow_outcome": None,
                            "body": {"event.name": "recall_shadow", "status": "ok"},
                            "attributes": {"token": "‹REDACTED›", "list": ["x", SK]},
                            "resource": {"attributes": {"service.name": "door"}},
                            "otel": {"observed_timestamp": observed, "time_unix_nano": 5},
                        }
                    ],
                    "limit_applied": 50,
                    "maybe_truncated": False,
                }
            ).encode()

        self.assertTrue(
            ep.answers_match(
                ep.Answer(200, body("2026-10-08T01:00:00+00:00", 9)),
                ep.Answer(200, body("2026-10-08T02:00:00+00:00", 77)),
                "get",
            )
        )
        other = json.loads(body("a", 1))
        other["entries"][0]["event"] = "other"
        self.assertFalse(
            ep.answers_match(ep.Answer(200, body("a", 1)), ep.Answer(200, json.dumps(other).encode()), "get")
        )

    def test_mcp_minus_32603_compares_code_only_but_32602_compares_the_message(self):
        engine = ep.Answer(
            200,
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "events: sqlx down"}}
            ).encode(),
        )
        python = ep.Answer(
            200,
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "events: psycopg down"}}
            ).encode(),
        )
        self.assertTrue(ep.answers_match(engine, python, "mcp"))
        bad = ep.Answer(
            200,
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "since_hours is too large"}}
            ).encode(),
        )
        self.assertFalse(ep.answers_match(engine, bad, "mcp"))
        good = ep.Answer(
            200,
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "since_hours must be >= 0"}}
            ).encode(),
        )
        same = ep.Answer(
            200,
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "since_hours must be >= 0"}}
            ).encode(),
        )
        self.assertTrue(ep.answers_match(good, same, "mcp"))


class BatchTest(unittest.TestCase):
    def test_batch_covers_all_three_surfaces_and_the_fixed_shapes(self):
        batch = ep.build_batch([ROW])
        counts = ep.surface_counts(batch)
        self.assertEqual(counts, {"post": 6, "get": 10, "mcp": 6})
        labels = [r.label for r in batch]
        for want in (
            "101 batch (400)",
            "empty batch",
            "single object body",
            "non-array events value",
            "redaction probe",
            "get since_hours=-1 (400)",
            "mcp since_hours=-1 (-32602)",
        ):
            self.assertIn(want, labels)

    def test_midway_request_is_a_three_event_batch_with_a_boom_row(self):
        req = ep.midway_request()
        self.assertEqual(req.surface, "post")
        self.assertEqual(
            [e["component"] for e in req.body["events"]], ["parity-mid-a", ep.BOOM, "parity-mid-b"]
        )


class DecideTest(unittest.TestCase):
    def base(self) -> ep.Result:
        return ep.Result(counts={"post": 6, "get": 11, "mcp": 6}, answered={"post": 6, "get": 11, "mcp": 6})

    def test_a_full_green_run_passes(self):
        result = self.base()
        result.table_diffs = ([], [])
        result.midway = ep.Midway(500, 500, 1, 0)
        passed, problems = ep.decide(result)
        self.assertTrue(passed, problems)

    def test_any_zero_surface_refuses_pass(self):
        result = self.base()
        result.counts["mcp"] = 0
        result.table_diffs = ([], [])
        result.midway = ep.Midway(500, 500, 1, 0)
        passed, problems = ep.decide(result)
        self.assertFalse(passed)
        self.assertTrue(any("surface mcp" in p for p in problems))

    def test_response_diffs_and_table_diffs_refuse_pass(self):
        result = self.base()
        result.response_diffs.append(("get limit=2", ep.Answer(200, b"a"), ep.Answer(200, b"b")))
        result.table_diffs = ([("row",)], [])
        result.midway = ep.Midway(500, 500, 1, 0)
        passed, problems = ep.decide(result)
        self.assertFalse(passed)
        self.assertTrue(any("response differs" in p for p in problems))
        self.assertTrue(any("event_log differs" in p for p in problems))

    def test_midway_must_be_partial_on_the_engine_and_zero_on_python(self):
        result = self.base()
        result.table_diffs = ([], [])
        result.midway = ep.Midway(500, 500, 0, 0)
        self.assertFalse(ep.decide(result)[0], "엔진이 반쯤 쓰지 않았다 — 주사가 안 물었다")
        result.midway = ep.Midway(500, 200, 1, 0)
        self.assertFalse(ep.decide(result)[0], "파이썬이 실패 없이 지나갔다")
        result.midway = ep.Midway(500, 500, 1, 1)
        self.assertFalse(ep.decide(result)[0], "파이썬도 반쯤 썼다 — 트랜잭션이 아니다")


if __name__ == "__main__":
    unittest.main()
