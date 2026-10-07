#!/usr/bin/env python3
"""사건 쓰기·읽기(E4-6a) 시험 — 요청 경계의 엔진 문구, 칸 대열, 진짜 pg 에 물어 본 쓰기·트랜잭션.

Run: BORING_TEST_DATABASE_URL=postgresql://… python3 src/ohmyboring/events/test_events.py
DB 시험은 변수가 없으면 걸러낸다(search/test_pg.py 와 같은 관례). 일회용 DB 의 별도 스키마
(omb_events_test)에 표를 만들고 끝에 떨군다 — public 의 표는 손 안 탄다.
Mutation targets: attributes 를 언제나 otel.attributes 로·가림이 키·목록을 건드리거나 걸름·
행마다 자동 커밋·읽기의 중첩 키 정렬 빼기 — 각각 아래 단언에서 사망 확인(구현 커밋 뒤 사본에서).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ohmyboring.events import parse as events_parse  # noqa: E402
from ohmyboring.events import pg as events_pg  # noqa: E402
from ohmyboring.events import run as events_run  # noqa: E402
from ohmyboring.result import Err, Ok  # noqa: E402
from ohmyboring.search.redact import redact_json_value  # noqa: E402

DSN = os.environ.get("BORING_TEST_DATABASE_URL")
SCHEMA = "omb_events_test"

SK_ANT = "sk-ant-abcdefghijklmnopqrstuvwxyz1234567890ABCDEF"


def rejected_message(parsed) -> str:
    match parsed:
        case Err(events_parse.Rejected(message, _)):
            return message
        case other:
            raise AssertionError(f"expected a rejection, got {other!r}")


class BatchTests(unittest.TestCase):
    """http.rs event_batch — events 칸이 배열이면 그 배열, 아니면 본문 통째가 한 사건."""

    def test_over_100_is_400_with_the_engine_wording(self):
        body = {"events": [{"event": "x", "i": i} for i in range(101)]}
        self.assertEqual(
            rejected_message(events_run.ingest(body, self.deps())), "events batch too large: max 100"
        )
        at_max = {"events": [{"event": "x"}] * 100}
        self.assertIsInstance(events_parse.event_batch(at_max), Ok)

    def test_empty_batch_accepts_zero(self):
        match events_parse.event_batch({"events": []}):
            case Ok(events):
                self.assertEqual(events, [])
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_empty_batch_never_touches_the_store_but_still_needs_one(self):
        """엔진은 사건 루프를 안 돌아 표를 안 만지지만, 저장소 확인 자체는 먼저다."""
        match events_run.ingest({"events": []}, events_run.Deps(None)):
            case Err(events_run.StoreOff()):
                pass
            case other:
                self.fail(f"expected StoreOff, got {other!r}")
        match events_run.ingest({"events": []}, self.deps()):
            case Ok(payload):
                self.assertEqual(payload, {"accepted": 0})
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_single_object_body_is_one_event(self):
        match events_parse.event_batch({"component": "door", "event": "recall_shadow"}):
            case Ok(events):
                self.assertEqual(events, [{"component": "door", "event": "recall_shadow"}])
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_non_array_events_value_means_whole_body_is_one_event(self):
        for body in ({"events": "x"}, {"events": 3}, {"events": {"a": 1}}, {"events": None}):
            match events_parse.event_batch(body):
                case Ok(events):
                    self.assertEqual(events, [body], body)
                case other:
                    self.fail(f"{body}: expected Ok, got {other!r}")
        for body in ([1, 2], "str", 7):
            match events_parse.event_batch(body):
                case Ok(events):
                    self.assertEqual(events, [body], body)
                case other:
                    self.fail(f"{body}: expected Ok, got {other!r}")

    def deps(self):
        class NeverConnect:
            def __call__(self):
                raise AssertionError("검증이 먼저다 — 연결을 열지 않는다")

        return events_run.Deps(NeverConnect())


class QueryParseTests(unittest.TestCase):
    """http.rs EventLogReq + validate_event_since_hours — 클램프·검사 순서와 문구."""

    def test_limit_defaults_and_clamps(self):
        match events_parse.parse_event_query(""):
            case Ok(q):
                self.assertEqual((q.limit, q.since_hours), (50, None))
            case other:
                self.fail(f"expected Ok, got {other!r}")
        for raw, want in (("limit=0", 1), ("limit=1", 1), ("limit=5000", 1000), ("limit=1000", 1000)):
            match events_parse.parse_event_query(raw):
                case Ok(q):
                    self.assertEqual(q.limit, want, raw)
                case other:
                    self.fail(f"{raw}: expected Ok, got {other!r}")

    def test_negative_since_hours_is_400_before_anything_else(self):
        self.assertEqual(
            rejected_message(events_parse.parse_event_query("since_hours=-1")),
            "since_hours must be >= 0",
        )
        match events_parse.parse_event_query("since_hours=0"):
            case Ok(q):
                self.assertEqual(q.since_hours, 0)
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_non_integer_query_fields_are_serde_400s(self):
        self.assertEqual(
            rejected_message(events_parse.parse_event_query("limit=abc")),
            "Failed to deserialize query string: limit: invalid digit found in string",
        )
        self.assertEqual(
            rejected_message(events_parse.parse_event_query("since_hours=1.5")),
            "Failed to deserialize query string: since_hours: invalid digit found in string",
        )

    def test_limit_range_is_i64_and_clamps(self):
        """serve.rs EventLogReq — limit 은 i64: 2^31 을 넘어도 받고(검증 통과) 클램프만 1000."""
        match events_parse.parse_event_query("limit=3000000000"):
            case Ok(q):
                self.assertEqual(q.limit, 1000)
            case other:
                self.fail(f"expected Ok, got {other!r}")
        match events_parse.parse_event_query("limit=%2B5"):
            case Ok(q):
                self.assertEqual(q.limit, 5, "Rust str::parse 는 선행 + 를 받는다")
            case other:
                self.fail(f"expected Ok, got {other!r}")
        self.assertEqual(
            rejected_message(events_parse.parse_event_query("limit=9223372036854775808")),
            "Failed to deserialize query string: limit: number too large to fit in target type",
        )

    def test_a_declared_field_twice_is_a_duplicate_field_400_and_unknown_fields_are_ignored(self):
        # 2026-10-08 사본 엔진(:7791) 실측 문장 그대로.
        for query, want in (
            ("limit=1&limit=2", "Failed to deserialize query string: duplicate field `limit`"),
            ("event=a&event=b", "Failed to deserialize query string: duplicate field `event`"),
            ("limit=abc&limit=2", "Failed to deserialize query string: limit: invalid digit found in string"),
            ("limit=-", "Failed to deserialize query string: limit: invalid digit found in string"),
            ("limit=%2B", "Failed to deserialize query string: limit: invalid digit found in string"),
        ):
            self.assertEqual(rejected_message(events_parse.parse_event_query(query)), want, query)
        self.assertIsInstance(events_parse.parse_event_query("foo=1&foo=2"), Ok)

    def test_serde_400_wording_is_rust_parse_int_display(self):
        """serde_urlencoded 는 str::parse 의 ParseIntError 문장을 그대로 낸다(de::Error::custom)."""
        for raw, want in (
            (
                "limit=",
                "Failed to deserialize query string: limit: cannot parse integer from empty string",
            ),
            (
                "limit=1_0",
                "Failed to deserialize query string: limit: invalid digit found in string",
            ),
            (
                "limit=%205",
                "Failed to deserialize query string: limit: invalid digit found in string",
            ),
            (
                "since_hours=99999999999",
                "Failed to deserialize query string: since_hours: number too large to fit in target type",
            ),
            (
                "since_hours=-99999999999",
                "Failed to deserialize query string: since_hours: number too small to fit in target type",
            ),
            (
                "since_hours=3000000000",
                "Failed to deserialize query string: since_hours: number too large to fit in target type",
            ),
        ):
            self.assertEqual(rejected_message(events_parse.parse_event_query(raw)), want, raw)

    def test_filters_map_event_to_event_name_and_keep_strings_untrimmed(self):
        match events_parse.parse_event_query(
            "component=door&event=recall_shadow&status=ok&run_id=r1&workflow=wf"
        ):
            case Ok(q):
                self.assertEqual(
                    (q.component, q.event_name, q.status, q.run_id, q.workflow),
                    ("door", "recall_shadow", "ok", "r1", "wf"),
                )
            case other:
                self.fail(f"expected Ok, got {other!r}")
        match events_parse.parse_event_query("component=%20"):
            case Ok(q):
                self.assertEqual(
                    q.component, " ", "빈 문자열 필터는 빈 문자열과 딱 맞는다(엔진이 트림 안 함)"
                )
            case other:
                self.fail(f"expected Ok, got {other!r}")


class McpArgsTests(unittest.TestCase):
    """mcp.rs mcp_events — 문자열 트림·빈 칸 무시, 비정수 limit 은 50, since_hours 규약."""

    def test_non_integer_limit_defaults_to_50_and_clamps(self):
        for raw in (None, "5", 5.5, True):
            args = {} if raw is None else {"limit": raw}
            match events_parse.parse_mcp_args(args):
                case Ok(parsed):
                    self.assertEqual(parsed.limit, 50, raw)
                case other:
                    self.fail(f"{raw}: expected Ok, got {other!r}")
        match events_parse.parse_mcp_args({"limit": 5000}):
            case Ok(parsed):
                self.assertEqual(parsed.limit, 1000)
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_strings_trimmed_and_empty_ignored(self):
        match events_parse.parse_mcp_args({"component": " door ", "event": "   ", "status": 5}):
            case Ok(parsed):
                self.assertEqual((parsed.component, parsed.event_name, parsed.status), ("door", None, None))
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_since_hours_rules(self):
        self.assertEqual(
            rejected_message(events_parse.parse_mcp_args({"since_hours": -1})), "since_hours must be >= 0"
        )
        self.assertEqual(
            rejected_message(events_parse.parse_mcp_args({"since_hours": 2**31})),
            "since_hours is too large",
        )
        for raw in ("1", 1.5, False):
            match events_parse.parse_mcp_args({"since_hours": raw}):
                case Ok(parsed):
                    self.assertIsNone(parsed.since_hours, raw)
                case other:
                    self.fail(f"{raw}: expected Ok, got {other!r}")
        match events_parse.parse_mcp_args({"since_hours": 24}):
            case Ok(parsed):
                self.assertEqual(parsed.since_hours, 24)
            case other:
                self.fail(f"expected Ok, got {other!r}")


class OrderTests(unittest.TestCase):
    """핸들러 순서 — POST 는 저장소가 먼저, GET 은 검증이 먼저, MCP 는 저장소가 먼저."""

    def test_ingest_checks_the_store_before_the_batch(self):
        match events_run.ingest({"events": [{"i": i} for i in range(101)]}, events_run.Deps(None)):
            case Err(events_run.StoreOff()):
                pass
            case other:
                self.fail(f"expected StoreOff, got {other!r}")

    def test_read_validates_the_query_before_the_store(self):
        match events_run.read("since_hours=-1", events_run.Deps(None)):
            case Err(events_parse.Rejected("since_hours must be >= 0", 400)):
                pass
            case other:
                self.fail(f"expected Rejected, got {other!r}")

    def test_mcp_checks_the_store_before_the_arguments(self):
        match events_run.mcp_read({"since_hours": -1}, events_run.Deps(None)):
            case Err(events_run.StoreOff()):
                pass
            case other:
                self.fail(f"expected StoreOff, got {other!r}")

    def test_store_off_is_the_engines_sentence(self):
        self.assertEqual(
            events_run.StoreOff().message,
            "BORING_VECTOR=off — this feature requires the vector backend (pgvector). "
            "Set BORING_VECTOR=on and start Postgres.",
        )


class EventNameTests(unittest.TestCase):
    """store.rs log_event 의 event_name — otel.event_name 이 문자열이면 빈 문자열까지 그대로."""

    def test_empty_otel_event_name_wins_over_the_event_field(self):
        self.assertEqual(events_pg._event_name({"event": "x"}, {"event_name": ""}), "")


class SeverityTableTests(unittest.TestCase):
    """store.rs 대체표 — status→문자, 문자→수."""

    def test_status_to_severity_text(self):
        for status, want in (
            ("failed", "ERROR"),
            ("Failure", "ERROR"),
            ("ERROR", "ERROR"),
            ("warn", "WARN"),
            ("Warning", "WARN"),
            ("debug", "DEBUG"),
            ("trace", "TRACE"),
            ("ok", "INFO"),
            ("", "INFO"),
            ("anything", "INFO"),
        ):
            self.assertEqual(events_pg.severity_text_for_status(status), want, status)

    def test_severity_text_to_number(self):
        for text, want in (
            ("TRACE", 1),
            ("debug", 5),
            ("WARN", 13),
            ("error", 17),
            ("FATAL", 21),
            ("INFO", 9),
            ("", 9),
            ("WEIRD", 9),
        ):
            self.assertEqual(events_pg.severity_number_for_text(text), want, text)


class RedactWalkerTests(unittest.TestCase):
    """store.rs redact_json_value — 문자열 값만 가리고, 키와 목록 뼈대는 손 안 댄다."""

    def test_redacts_string_values_only_recursing_into_lists_and_objects(self):
        value = {"token": SK_ANT, "items": [{"note": f"x {SK_ANT} y"}, 5, None], "n": 3}
        out = redact_json_value(value)
        self.assertEqual(out["token"], "‹REDACTED›")
        self.assertEqual(out["items"][0]["note"], "x ‹REDACTED› y")
        self.assertEqual(out["items"][1:], [5, None])
        self.assertEqual(out["n"], 3)
        self.assertEqual(list(out), ["token", "items", "n"], "키는 그대로")

    def test_keys_are_not_redacted(self):
        out = redact_json_value({SK_ANT: "plain", "api_key": "plain"})
        self.assertEqual(out, {SK_ANT: "plain", "api_key": "plain"})


class EntryBytesTests(unittest.TestCase):
    """serve.rs EventLogEntry 의 응답 바이트 — 구조체 칸 순서, 중첩 Value 는 키 정렬, chrono 시각."""

    ROW = events_pg.EventRow(
        id=7,
        observed_at=datetime(2026, 10, 8, 1, 2, 3, 120000, tzinfo=UTC),
        time_unix_nano=123,
        severity_text="INFO",
        severity_number=9,
        service_name="door",
        component="door",
        event_name="recall_shadow",
        status="ok",
        trace_id="t1",
        span_id="s1",
        run_id="r1",
        session_id="se1",
        workflow="wf",
        workflow_node="n1",
        workflow_outcome="done",
        body={"event.name": "recall_shadow", "status": "ok"},
        attributes={
            "component": "door",
            "event": "recall_shadow",
            "list": ["x", SK_ANT],
            "nested": {"b": 1, "a": 2},
        },
        resource={"attributes": {"service.name": "door", "service.namespace": "oh-my-boring"}},
    )

    EXPECTED = (
        '{"entries":[{"id":7,"observed_at":"2026-10-08T01:02:03.120+00:00","time_unix_nano":123,'
        '"severity_text":"INFO","severity_number":9,"service_name":"door","component":"door",'
        '"event":"recall_shadow","status":"ok","trace_id":"t1","span_id":"s1","run_id":"r1",'
        '"session_id":"se1","workflow":"wf","workflow_node":"n1","workflow_outcome":"done",'
        '"body":{"event.name":"recall_shadow","status":"ok"},'
        '"attributes":{"component":"door","event":"recall_shadow","list":["x","'
        + SK_ANT
        + '"],"nested":{"a":2,"b":1}},'
        '"resource":{"attributes":{"service.name":"door","service.namespace":"oh-my-boring"}},'
        '"otel":{"attributes":{"component":"door","event":"recall_shadow","list":["x","'
        + SK_ANT
        + '"],"nested":{"a":2,"b":1}},'
        '"body":{"event.name":"recall_shadow","status":"ok"},"event_name":"recall_shadow",'
        '"observed_timestamp":"2026-10-08T01:02:03.120+00:00",'
        '"resource":{"attributes":{"service.name":"door","service.namespace":"oh-my-boring"}},'
        '"severity_number":9,"severity_text":"INFO","span_id":"s1","time_unix_nano":123,'
        '"trace_id":"t1"}}],"limit_applied":50,"maybe_truncated":false}'
    )

    def test_entry_bytes_match_the_engine_struct_order_and_sorted_nested_keys(self):
        payload = events_pg.entries_payload([self.ROW], 50)
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(wire, self.EXPECTED)

    def test_rfc3339_fraction_rules_match_chrono(self):
        base = datetime(2026, 10, 8, 1, 2, 3, tzinfo=UTC)
        self.assertEqual(events_pg.rfc3339(base), "2026-10-08T01:02:03+00:00")
        self.assertEqual(events_pg.rfc3339(base.replace(microsecond=120000)), "2026-10-08T01:02:03.120+00:00")
        self.assertEqual(
            events_pg.rfc3339(base.replace(microsecond=123456)), "2026-10-08T01:02:03.123456+00:00"
        )
        kst = datetime(
            2026,
            10,
            8,
            10,
            2,
            3,
            tzinfo=__import__("datetime").timezone(__import__("datetime").timedelta(hours=9)),
        )
        self.assertEqual(events_pg.rfc3339(kst), "2026-10-08T01:02:03+00:00", "UTC 로 접는다")


def _skip_reason() -> str | None:
    if not DSN:
        return "BORING_TEST_DATABASE_URL unset — DB integration test skipped (disposable DB only)"
    return None


@unittest.skipIf(DSN is None, _skip_reason())
class EventsDbTests(unittest.TestCase):
    """별도 스키마에 고정 물 — 순수 파이썬 게이트에는 안 잡히는 SQL 결과(칸 대열·트랜잭션)를 본다."""

    OPTIONS = f"-c search_path={SCHEMA},public"

    @classmethod
    def setUpClass(cls):
        import psycopg

        cls.psycopg = psycopg
        cls.db = psycopg.connect(DSN, autocommit=True, options=cls.OPTIONS)
        with cls.db.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;")
            cur.execute(f"CREATE SCHEMA {SCHEMA};")
            cur.execute("SET search_path TO " + SCHEMA + ", public;")
            cur.execute(
                "CREATE TABLE event_log ("
                " id bigserial PRIMARY KEY,"
                " observed_at timestamptz NOT NULL DEFAULT now(),"
                " time_unix_nano bigint,"
                " severity_text text NOT NULL DEFAULT 'INFO',"
                " severity_number int NOT NULL DEFAULT 9,"
                " service_name text NOT NULL DEFAULT '',"
                " component text NOT NULL DEFAULT '',"
                " event_name text NOT NULL DEFAULT '',"
                " status text NOT NULL DEFAULT '',"
                " trace_id text,"
                " span_id text,"
                " run_id text,"
                " session_id text,"
                " workflow text,"
                " workflow_node text,"
                " workflow_outcome text,"
                " body jsonb NOT NULL DEFAULT '{}'::jsonb,"
                " attributes jsonb NOT NULL DEFAULT '{}'::jsonb,"
                " resource jsonb NOT NULL DEFAULT '{}'::jsonb);"
            )
            cur.execute(
                "CREATE FUNCTION fail_on_boom() RETURNS trigger LANGUAGE plpgsql AS $$"
                " BEGIN IF NEW.component = 'boom' THEN RAISE EXCEPTION 'boom'; END IF; RETURN NEW; END $$;"
            )
            cur.execute(
                "CREATE TRIGGER event_boom BEFORE INSERT ON event_log"
                " FOR EACH ROW EXECUTE FUNCTION fail_on_boom();"
            )

    @classmethod
    def tearDownClass(cls):
        with cls.db.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;")
        cls.db.close()

    def setUp(self):
        with self.db.cursor() as cur:
            cur.execute("TRUNCATE event_log;")

    def deps(self) -> events_run.Deps:
        return events_run.Deps(connect=lambda: self.psycopg.connect(DSN, options=self.OPTIONS))

    def q(self, sql: str, *params):
        with self.db.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def row(self, component: str) -> dict:
        import json

        rows = self.q(
            "SELECT component, event_name, status, severity_text, severity_number, service_name,"
            " time_unix_nano, trace_id, span_id, run_id, session_id, workflow, workflow_node,"
            " workflow_outcome, body::text, attributes::text, resource::text"
            " FROM event_log WHERE component = %s;",
            component,
        )
        self.assertEqual(len(rows), 1, component)
        keys = (
            "component event_name status severity_text severity_number service_name time_unix_nano"
            " trace_id span_id run_id session_id workflow workflow_node workflow_outcome"
            " body attributes resource"
        ).split()
        parsed = dict(zip(keys, rows[0]))
        for key in ("body", "attributes", "resource"):
            parsed[key] = json.loads(parsed[key])
        return parsed

    def test_legacy_columns_trim_and_severity_fallbacks(self):
        body = {
            "component": "door",
            "event": "  recall_shadow  ",
            "status": "warn",
            "run_id": " r1 ",
            "session_id": "   ",
            "workflow": "wf",
            "workflow_node": "n1",
            "workflow_outcome": "done",
            "extra": "kept",
        }
        self.assertEqual(events_run.ingest(body, self.deps()).value, {"accepted": 1})
        stored = self.row("door")
        self.assertEqual(stored["event_name"], "recall_shadow", "text_field 는 트림한다")
        self.assertEqual(stored["status"], "warn")
        self.assertEqual((stored["severity_text"], stored["severity_number"]), ("WARN", 13))
        self.assertEqual(stored["run_id"], "r1")
        self.assertIsNone(stored["session_id"], "공백만 있는 칸은 NULL")
        self.assertEqual(
            stored["attributes"],
            {**body, "event": "  recall_shadow  "},
            "otel 이 없으면 attributes 는 가린 본문 통째다",
        )
        self.assertEqual(stored["body"], {"event.name": "recall_shadow", "status": "warn"}, "body 기본값")
        self.assertEqual(
            stored["resource"],
            {"attributes": {"service.name": "door", "service.namespace": "oh-my-boring"}},
        )
        self.assertEqual(stored["service_name"], "door")

    def test_otel_overrides_and_time_unix_nano_null_when_absent(self):
        body = {
            "component": "c",
            "event": "e",
            "status": "failure",
            "run_id": "r1",
            "otel": {
                "event_name": "raw  name  ",
                "severity_text": "DEBUG",
                "severity_number": 5,
                "trace_id": "tt",
                "span_id": "ss",
                "body": {"custom": 1},
                "attributes": {"k": "v"},
                "resource": {"attributes": {"service.name": "svc"}},
                "run_id": "ignored-in-otel",
            },
        }
        self.assertEqual(events_run.ingest(body, self.deps()).value, {"accepted": 1})
        stored = self.row("c")
        self.assertEqual(stored["event_name"], "raw  name  ", "otel.event_name 은 트림 안 한다")
        self.assertEqual((stored["severity_text"], stored["severity_number"]), ("DEBUG", 5))
        self.assertEqual((stored["trace_id"], stored["span_id"]), ("tt", "ss"))
        self.assertEqual(stored["body"], {"custom": 1})
        self.assertEqual(stored["attributes"], {"k": "v"}, "otel.attributes 가 있으면 그것")
        self.assertEqual(stored["service_name"], "svc")
        self.assertEqual(stored["run_id"], "r1", "run_id 는 언제나 윗칸에서만 읽는다")
        self.assertIsNone(stored["time_unix_nano"], "otel.time_unix_nano 없으면 NULL")
        with_otel_time = {**body, "component": "c2", "otel": {**body["otel"], "time_unix_nano": 999}}
        events_run.ingest(with_otel_time, self.deps())
        self.assertEqual(self.row("c2")["time_unix_nano"], 999)

    def test_empty_otel_event_name_is_stored_empty(self):
        """store.rs 는 otel.event_name = "" 을 "" 그대로 둔다 — 대체는 칸 값이 없을 때만 튄다."""
        body = {"component": "c", "event": "x", "otel": {"event_name": ""}}
        self.assertEqual(events_run.ingest(body, self.deps()).value, {"accepted": 1})
        self.assertEqual(self.row("c")["event_name"], "")

    def test_severity_number_falls_back_when_otel_value_is_not_an_i32(self):
        base = {"component": "c", "event": "e", "status": "ok"}
        too_big = {**base, "component": "big", "otel": {"severity_number": 2**31}}
        events_run.ingest(too_big, self.deps())
        self.assertEqual((self.row("big")["severity_text"], self.row("big")["severity_number"]), ("INFO", 9))
        as_float = {**base, "component": "float", "otel": {"severity_number": 9.0, "severity_text": "WARN"}}
        events_run.ingest(as_float, self.deps())
        self.assertEqual(
            (self.row("float")["severity_text"], self.row("float")["severity_number"]), ("WARN", 13)
        )

    def test_redaction_lands_on_nested_list_values_and_leaves_keys(self):
        body = {
            "component": "door",
            "event": "remember_written",
            "items": ["x", SK_ANT],
            "token": SK_ANT,
            "note": f"key={SK_ANT}",
        }
        events_run.ingest(body, self.deps())
        stored = self.row("door")
        self.assertEqual(stored["attributes"]["items"], ["x", "‹REDACTED›"], "목록 속 문자염 값도 가림")
        self.assertEqual(stored["attributes"]["token"], "‹REDACTED›")
        self.assertIn("token", stored["attributes"], "키는 손 안 댄다")
        self.assertEqual(stored["attributes"]["note"], "key=‹REDACTED›")

    def test_a_failing_row_mid_batch_writes_nothing(self):
        batch = [
            {"component": "ok1", "event": "e"},
            {"component": "boom", "event": "e"},
            {"component": "ok2", "event": "e"},
        ]
        match events_run.ingest({"events": batch}, self.deps()):
            case Err(events_run.Failed(message)):
                self.assertIn("boom", message)
            case other:
                self.fail(f"expected Failed, got {other!r}")
        self.assertEqual(self.q("SELECT count(*) FROM event_log;")[0][0], 0, "한 요청 = 한 트랜잭션")
        ok = events_run.ingest({"events": [batch[0], batch[2]]}, self.deps())
        self.assertIsInstance(ok, Ok, "대조군: boom 이 없으면 같은 묶음이 쓰인다")
        self.assertEqual(self.q("SELECT count(*) FROM event_log;")[0][0], 2)

    def test_read_filters_limit_and_truncation(self):
        for i in range(3):
            events_run.ingest(
                {
                    "component": "door",
                    "event": "recall_shadow" if i == 0 else "remember_written",
                    "status": "ok",
                    "run_id": f"r{i}",
                },
                self.deps(),
            )
        payload = events_run.read("limit=2", self.deps()).value
        self.assertEqual(
            (payload["limit_applied"], len(payload["entries"]), payload["maybe_truncated"]), (2, 2, True)
        )
        payload = events_run.read("limit=5000", self.deps()).value
        self.assertEqual(
            (payload["limit_applied"], len(payload["entries"]), payload["maybe_truncated"]), (1000, 3, False)
        )
        match events_run.read("limit=3000000000", self.deps()):
            case Ok(payload):
                self.assertEqual(payload["limit_applied"], 1000, "i64 라 받고 1000 에 클램프")
            case other:
                self.fail(f"expected Ok, got {other!r}")
        payload = events_run.read("event=recall_shadow", self.deps()).value
        self.assertEqual([e["event"] for e in payload["entries"]], ["recall_shadow"])
        payload = events_run.read("component=door&run_id=r2", self.deps()).value
        self.assertEqual([e["run_id"] for e in payload["entries"]], ["r2"])
        match events_run.read("since_hours=-1", self.deps()):
            case Err(events_parse.Rejected("since_hours must be >= 0", 400)):
                pass
            case other:
                self.fail(f"expected Rejected, got {other!r}")
        mcp = events_run.mcp_read({"component": "door", "limit": 1}, self.deps()).value
        self.assertEqual(list(mcp), ["entries"])
        self.assertEqual(len(mcp["entries"]), 1)


if __name__ == "__main__":
    unittest.main()
