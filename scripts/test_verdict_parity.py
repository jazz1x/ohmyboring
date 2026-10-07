#!/usr/bin/env python3
"""verdict-parity 의 순수 함수 시험 — 운영 DSN 단언·정규화·차이 계산·PASS 판정만. DB·엔진은 안 만진다."""

import importlib.util
import pathlib
import sys
import unittest
from datetime import UTC, datetime

HERE = pathlib.Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[1] / "src")]
SPEC = importlib.util.spec_from_file_location("verdict_parity", HERE.with_name("verdict-parity.py"))
vp = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = vp  # dataclass 가 모듈 이름으로 자기 이름공간을 찾는다
SPEC.loader.exec_module(vp)

PROD = "postgresql://boring:s3cret@127.0.0.1:5432/boring"
A = "postgresql://boring:s3cret@127.0.0.1:5433/parity_a"
B = "postgresql://boring:s3cret@127.0.0.1:5433/parity_b"


class DsnGuardTest(unittest.TestCase):
    def test_scratch_pair_passes_and_the_line_names_both_without_a_password(self):
        ok, line = vp.assert_scratch(A, B, {"DOOR_PG_DSN": PROD})
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
            self.assertFalse(vp.assert_scratch(prod_like, B, {})[0], prod_like)
            self.assertFalse(vp.assert_scratch(A, prod_like, {})[0], prod_like)

    def test_env_named_production_is_refused_and_same_database_twice_is_refused(self):
        self.assertFalse(vp.assert_scratch(A, B, {"PG_DSN": A})[0])
        self.assertFalse(vp.assert_scratch(A, A, {})[0])


class NormalizeTest(unittest.TestCase):
    START = datetime(2026, 10, 7, tzinfo=UTC)

    def test_only_a_stamp_from_the_batch_window_folds_to_now(self):
        old = ("s", "p", "v", "/a.md", "2026-09-01", "fact", datetime(2026, 9, 2, tzinfo=UTC))
        fresh = ("s", "p", "v", "/a.md", "2026-09-01", "fact", datetime(2026, 10, 7, 0, 0, 5, tzinfo=UTC))
        live = ("s", "p", "v", "/a.md", "2026-09-01", "fact", None)
        self.assertEqual(vp.normalize_claim(old, self.START)[-1], old[-1])
        self.assertEqual(vp.normalize_claim(fresh, self.START)[-1], "NOW")
        self.assertIsNone(vp.normalize_claim(live, self.START)[-1])

    def test_event_projection_is_stable_over_target_key_order(self):
        row = ("drudge.owner", "owner_supersede_refused", "warn", "WARN", "consumption")
        self.assertEqual(vp.normalize_event((*row, ["/x"])), vp.normalize_event((*row, ["/x"])))
        self.assertNotEqual(vp.normalize_event((*row, ["/x"])), vp.normalize_event((*row, ["/y"])))


class DiffRowsTest(unittest.TestCase):
    def test_multiset_counts_duplicates(self):
        self.assertEqual(vp.diff_rows([1, 1, 2], [1, 2]), ([1], []))
        self.assertEqual(vp.diff_rows([1], [1, 3]), ([], [3]))
        self.assertEqual(vp.diff_rows([2, 1], [1, 2]), ([], []))


class BatchTest(unittest.TestCase):
    ROWS = [
        ("/o1.md", "owner", 1),
        ("/o2.md", "owner", 0),
        ("/p1.md", "unknown", 2),
        ("/p2.md", "inferred", 1),
        ("/p3.md", "unknown", 0),
        ("/p4.md", "unknown", 0),
    ]

    def test_pick_docs_prefers_notes_with_current_claims_and_needs_enough_seed(self):
        docs = vp.pick_docs(self.ROWS)
        self.assertEqual(
            (docs.owner_old, docs.owner_new, docs.old_a, docs.old_b), ("/o1.md", "/o2.md", "/p1.md", "/p2.md")
        )
        self.assertEqual((docs.new_a, docs.new_b), ("/p3.md", "/p4.md"))
        self.assertIsNone(vp.pick_docs(self.ROWS[:3]))

    def test_batch_covers_all_three_surfaces_and_has_a_repeat_a_refusal_and_a_validation_error(self):
        batch = vp.build_batch(vp.pick_docs(self.ROWS))
        counts = vp.surface_counts(batch)
        self.assertTrue(all(counts[s] > 0 for s in vp.SURFACES), counts)
        self.assertGreater(len(batch), len(set(map(repr, batch))), "같은 요청 반복이 있다")
        self.assertTrue(any("/o1.md" in r.body.get("supersedes", [[]])[0] and not r.owner for r in batch))
        self.assertTrue(any(r.label.startswith("400") for r in batch))
        self.assertIn(vp.BOOM, vp.midway_request(vp.pick_docs(self.ROWS)).body["used"])


class DecideTest(unittest.TestCase):
    def good(self, **over):
        result = vp.Result(
            counts={"handover": 2, "consumption": 8, "mcp.verdict": 3},
            answered={"handover": 2, "consumption": 8, "mcp.verdict": 3},
            midway=vp.Midway(500, 500, 2, 0),
        )
        for key, value in over.items():
            setattr(result, key, value)
        return result

    def test_clean_run_passes(self):
        self.assertEqual(vp.decide(self.good()), (True, []))

    def test_a_surface_with_no_answered_requests_never_passes(self):
        answered = {"handover": 2, "consumption": 8, "mcp.verdict": 0}
        ok, problems = vp.decide(self.good(answered=answered))
        self.assertFalse(ok)
        self.assertIn("mcp.verdict", problems[0])

    def test_any_table_or_response_difference_blocks_pass(self):
        self.assertFalse(vp.decide(self.good(table_diffs={"edge": ([("a",)], [])}))[0])
        self.assertFalse(
            vp.decide(self.good(response_diffs=[("x", vp.Answer(200, b"a"), vp.Answer(200, b"b"))]))[0]
        )

    def test_midway_must_be_engine_partial_and_python_zero(self):
        self.assertFalse(
            vp.decide(self.good(midway=vp.Midway(500, 500, 0, 0)))[0],
            "엔진이 반쯤 못 썼다 = 주입이 안 물었다",
        )
        self.assertFalse(vp.decide(self.good(midway=vp.Midway(500, 500, 2, 1)))[0], "파이썬이 행을 남겼다")
        self.assertFalse(vp.decide(self.good(midway=None))[0])


if __name__ == "__main__":
    unittest.main()
