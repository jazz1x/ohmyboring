"""read_shadow 대조기 시험 — DB 없이 가짜 query 갈래만 둔다.

칸별 대조·갈래 분류(가/나/다)·거절 대조·레이스 재확인·MCP 봉투 파싱을 고정 답으로 본다.
Run: python3 ohmyboring/registers/test_shadow.py (repo root 의 src 륔 PYTHONPATH 로)

"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from ohmyboring.registers import pg as registers_pg
from ohmyboring.registers import shadow as registers_shadow
from ohmyboring.result import Err, Ok

EVENT_NAME = registers_shadow.EVENT_NAME


def _row(
    subject: str, predicate: str = "pred", value: str = "a value with enough characters"
) -> registers_pg.RegisterRow:
    return registers_pg.RegisterRow(
        subject=subject,
        predicate=predicate,
        value=value,
        kind="decision",
        confidence="certain",
        valid_from=datetime(2026, 9, 30, 1, 23, 45, 123456, tzinfo=UTC),
        project="omb",
    )


def _register_out(*rows: registers_pg.RegisterRow, empty: str = registers_pg.EMPTY_DECISIONS) -> dict:
    out = registers_pg._register_out(registers_pg.RegisterRows(tuple(rows), len(rows)), empty)
    return out.http_payload()


def _request(
    engine_payload: dict,
    python_result,
    **kwargs,
) -> registers_shadow.ShadowRequest:
    surface = kwargs.get("surface", "decisions")
    transport = kwargs.get("transport", "http")
    status = kwargs.get("status", 200)
    rerun_result = kwargs.get("rerun_result")
    arguments = kwargs.get("arguments", {"project": "omb"})
    if transport == "mcp":
        engine_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(engine_payload, ensure_ascii=False)}],
                    "structuredContent": engine_payload,
                    "isError": False,
                },
            }
        ).encode()
    else:
        engine_body = json.dumps(engine_payload, ensure_ascii=False).encode()
    return registers_shadow.ShadowRequest(
        surface=surface,
        transport=transport,
        arguments=arguments,
        engine_status=status,
        engine_body=engine_body,
        query=lambda: python_result,
        rerun=(lambda: rerun_result) if rerun_result is not None else None,
    )


class RegisterCompareTests(unittest.TestCase):
    def test_identical_http_register_is_ok(self):
        payload = _register_out(_row("subj"))
        event = registers_shadow.run_shadow(_request(payload, Ok(payload)))
        self.assertEqual(event.status, "ok")
        self.assertIsNone(event.reason)
        self.assertEqual(event.engine_rows, 1)
        self.assertEqual(event.python_rows, 1)

    def test_missing_row_is_python_defect(self):
        engine = _register_out(_row("subj"), _row("other"))
        python = _register_out(_row("subj"))
        event = registers_shadow.run_shadow(_request(engine, Ok(python)))
        self.assertEqual(event.status, "mismatch")
        self.assertGreaterEqual(event.python_defect, 1)
        self.assertIn("(가)", event.reason)

    def test_tie_order_is_engine_diff(self):
        rows = (_row("a"), _row("b"))
        engine = _register_out(*rows)
        python = _register_out(*reversed(rows))
        event = registers_shadow.run_shadow(_request(engine, Ok(python)))
        self.assertEqual(event.status, "mismatch")
        self.assertGreaterEqual(event.engine_diff, 1)
        self.assertIn("tie-plan (나)", event.reason)
        self.assertEqual(event.python_defect, 0)

    def test_race_rerun_match_stays_ok_with_unknown(self):
        engine = _register_out(_row("subj"))
        first = _register_out(empty=registers_pg.EMPTY_DECISIONS)
        event = registers_shadow.run_shadow(_request(engine, Ok(first), rerun_result=Ok(engine)))
        self.assertEqual(event.status, "ok")
        self.assertGreaterEqual(event.unknown, 1)
        self.assertIn("race (다)", event.reason)

    def test_race_rerun_still_diverging_is_python_defect(self):
        engine = _register_out(_row("subj"))
        first = _register_out(empty=registers_pg.EMPTY_DECISIONS)
        second = _register_out(_row("different"))
        event = registers_shadow.run_shadow(_request(engine, Ok(first), rerun_result=Ok(second)))
        self.assertEqual(event.status, "mismatch")
        self.assertGreaterEqual(event.python_defect, 1)

    def test_mcp_items_compared_field_by_field(self):
        row = _row("subj")
        out = registers_pg._register_out(registers_pg.RegisterRows((row,), 1), registers_pg.EMPTY_RISKS)
        engine = out.mcp_payload()
        python = dict(engine)
        python["items"] = [dict(engine["items"][0], confidence="unknown")]
        event = registers_shadow.run_shadow(_request(engine, Ok(python), surface="risks", transport="mcp"))
        self.assertEqual(event.status, "mismatch")
        self.assertGreaterEqual(event.python_defect, 1)

    def test_engine_rejection_matching_python_rejection_is_ok(self):
        message = "limit must be an integer in 1..=50"
        event = registers_shadow.run_shadow(
            _request({"error": message}, Err(registers_shadow.PyFailure(message, rejection=True)), status=400)
        )
        self.assertEqual(event.status, "ok")
        self.assertIn("both rejected", event.reason)

    def test_engine_rejection_python_answer_is_python_defect(self):
        event = registers_shadow.run_shadow(
            _request(
                {"error": "limit must be an integer in 1..=50"},
                Ok(_register_out(_row("subj"))),
                status=400,
            )
        )
        self.assertEqual(event.status, "mismatch")
        self.assertEqual(event.python_defect, 1)

    def test_engine_unreadable_is_unknown_error(self):
        event = registers_shadow.run_shadow(
            _request(
                {"answer": "x"},
                Ok(_register_out(_row("subj"))),
                status=500,
            )
        )
        self.assertEqual(event.status, "error")
        self.assertEqual(event.unknown, 1)
        self.assertIn("engine unreadable (다)", event.reason)

    def test_python_failure_is_unknown_error(self):
        event = registers_shadow.run_shadow(
            _request(
                _register_out(_row("subj")),
                Err(registers_shadow.PyFailure("pg connect: boom", rejection=False)),
            )
        )
        self.assertEqual(event.status, "error")
        self.assertIn("python-error (다)", event.reason)

    def test_python_rejection_of_engine_answer_is_python_defect(self):
        event = registers_shadow.run_shadow(
            _request(
                _register_out(_row("subj")),
                Err(registers_shadow.PyFailure("limit must be an integer in 1..=50", rejection=True)),
            )
        )
        self.assertEqual(event.status, "mismatch")
        self.assertEqual(event.python_defect, 1)


class ContextCompareTests(unittest.TestCase):
    def _card(self, subject: str) -> dict:
        return {
            "decisions": [_row(subject).as_context_item()],
            "risks": [],
            "facts": [],
            "glossary": [],
            "next_actions": [],
            "language": "ko",
        }

    def test_identical_card_is_ok(self):
        card = self._card("subj")
        event = registers_shadow.run_shadow(_request(card, Ok(card), surface="context"))
        self.assertEqual(event.status, "ok")

    def test_section_row_diff_is_python_defect(self):
        engine = self._card("subj")
        python = self._card("different")
        event = registers_shadow.run_shadow(_request(engine, Ok(python), surface="context"))
        self.assertEqual(event.status, "mismatch")
        self.assertIn("decisions rows", event.reason)
        self.assertGreaterEqual(event.python_defect, 1)

    def test_language_diff_is_python_defect(self):
        engine = self._card("subj")
        python = dict(engine)
        python["language"] = "en"
        event = registers_shadow.run_shadow(_request(engine, Ok(python), surface="context"))
        self.assertEqual(event.status, "mismatch")
        self.assertIn("field language", event.reason)


class RecurrencesCompareTests(unittest.TestCase):
    def _payload(self, newer_subject: str, distance: float = 0.1) -> dict:
        newer = registers_pg.ClaimRef(
            source_path="wiki/wiki-0001.md",
            subject=newer_subject,
            predicate="incident",
            value="the deploy pipeline breaks every other week",
            kind="risk",
            valid_from=datetime(2026, 9, 20, tzinfo=UTC),
        )
        older = registers_pg.ClaimRef(
            source_path="wiki/wiki-0002.md",
            subject="older",
            predicate="incident",
            value="the deploy pipeline breaks every other week",
            kind="risk",
            valid_from=datetime(2026, 9, 5, tzinfo=UTC),
        )
        row = registers_pg.Recurrence(newer, (older,), distance, 15, False)
        return {
            "rows": [row.as_dict()],
            "days": 30,
            "max_distance": 0.2,
            "min_days_apart": 3,
        }

    def test_identical_recurrences_ok(self):
        payload = self._payload("subj")
        event = registers_shadow.run_shadow(_request(payload, Ok(payload), surface="recurrences"))
        self.assertEqual(event.status, "ok")

    def test_distance_tolerates_f32_roundtrip(self):
        engine = self._payload("subj", distance=0.11347193360328674)  # f32 0.11347193 의 f64 상승
        python = self._payload("subj", distance=0.11347193)
        event = registers_shadow.run_shadow(_request(engine, Ok(python), surface="recurrences"))
        self.assertEqual(event.status, "ok")

    def test_group_set_diff_is_python_defect(self):
        engine = self._payload("subj")
        python = self._payload("different")
        event = registers_shadow.run_shadow(_request(engine, Ok(python), surface="recurrences"))
        self.assertEqual(event.status, "mismatch")
        self.assertIn("rows missing=1", event.reason)

    def test_days_apart_diff_is_python_defect(self):
        engine = self._payload("subj")
        python = json.loads(json.dumps(engine))
        python["rows"][0]["days_apart"] = 16
        event = registers_shadow.run_shadow(_request(engine, Ok(python), surface="recurrences"))
        self.assertEqual(event.status, "mismatch")
        self.assertIn("field days_apart", event.reason)


class StatusCompareTests(unittest.TestCase):
    def _payload(self, answer: str, sources: list[str]) -> dict:
        return {"answer": answer, "sources": sources}

    def test_generated_answer_not_compared_sources_match(self):
        payload = self._payload("## Status\n- Done: x", ["wiki/wiki-0001.md"])
        event = registers_shadow.run_shadow(_request(payload, Ok(payload), surface="status"))
        self.assertEqual(event.status, "ok")
        self.assertFalse(event.answer_compared)

    def test_empty_path_compared(self):
        payload = self._payload(registers_pg.STATUS_EMPTY.format(project="omb"), [])
        event = registers_shadow.run_shadow(_request(payload, Ok(payload), surface="status"))
        self.assertEqual(event.status, "ok")
        self.assertTrue(event.answer_compared)

    def test_empty_path_divergence_is_python_defect(self):
        engine = self._payload(registers_pg.STATUS_EMPTY.format(project="omb"), [])
        python = self._payload("## Status\n- Done: x", ["wiki/wiki-0001.md"])
        event = registers_shadow.run_shadow(_request(engine, Ok(python), surface="status"))
        self.assertEqual(event.status, "mismatch")
        self.assertIn("empty-path", event.reason)

    def test_sources_diff_is_python_defect(self):
        engine = self._payload("## Status", ["wiki/a.md"])
        python = self._payload("## Status", ["wiki/b.md"])
        event = registers_shadow.run_shadow(_request(engine, Ok(python), surface="status"))
        self.assertEqual(event.status, "mismatch")
        self.assertIn("sources rows", event.reason)


class RenderHelperTests(unittest.TestCase):
    def test_rfc3339_chrono_autosi(self):
        self.assertEqual(
            registers_pg.rfc3339(datetime(2026, 9, 30, 1, 2, 3, tzinfo=UTC)),
            "2026-09-30T01:02:03Z",
        )
        self.assertEqual(
            registers_pg.rfc3339(datetime(2026, 9, 30, 1, 2, 3, 123000, tzinfo=UTC)),
            "2026-09-30T01:02:03.123Z",
        )
        self.assertEqual(
            registers_pg.rfc3339(datetime(2026, 9, 30, 1, 2, 3, 123456, tzinfo=UTC)),
            "2026-09-30T01:02:03.123456Z",
        )

    def test_register_limit_rules(self):
        self.assertEqual(registers_pg.parse_register_limit(None).value, 50)
        self.assertEqual(registers_pg.parse_register_limit(7).value, 7)
        for bad in (0, 51, "5", 5.0, True, -1):
            match registers_pg.parse_register_limit(bad):
                case Err(registers_pg.Rejected(message)):
                    self.assertEqual(message, "limit must be an integer in 1..=50")
                case other:
                    self.fail(f"{bad!r} should reject, got {other!r}")

    def test_http_args_status_requires_string_project(self):
        match registers_pg.http_args("status", {"project": 5}):
            case Err(registers_pg.Rejected(message)):
                self.assertEqual(message, "unprocessable entity")
            case other:
                self.fail(f"expected rejection, got {other!r}")
        self.assertEqual(registers_pg.http_args("status", {"project": ""}).value, {"project": ""})

    def test_mcp_args_coerce_garbage_to_defaults(self):
        self.assertEqual(
            registers_pg.mcp_args("stalled", {"older_than_days": -1}).value,
            {"project": None, "older_than_days": 7, "limit": 50},
        )
        match registers_pg.mcp_args("stalled", {"older_than_days": -1, "limit": "5"}):
            case Err(registers_pg.Rejected(message)):
                self.assertEqual(message, "limit must be an integer in 1..=50")
            case other:
                self.fail(f"expected rejection, got {other!r}")
        match registers_pg.mcp_args("stalled", {"older_than_days": 2**33}):
            case Err(registers_pg.Rejected(message)):
                self.assertEqual(message, "older_than_days is too large")
            case other:
                self.fail(f"expected rejection, got {other!r}")

    def test_recurrences_limit_clamps_silently(self):
        self.assertEqual(registers_pg.recurrences_limit(0), 1)
        self.assertEqual(registers_pg.recurrences_limit(999), 50)
        self.assertEqual(registers_pg.recurrences_limit(-1), 10)


if __name__ == "__main__":
    unittest.main()
