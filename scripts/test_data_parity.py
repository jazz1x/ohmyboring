#!/usr/bin/env python3
"""data-parity 의 비교 함수 단위 시험 — canon 규칙(ingest.rs 와 동일)과 집합 대칭차만."""

import importlib.util
import pathlib
import unittest

SPEC = importlib.util.spec_from_file_location(
    "data_parity", pathlib.Path(__file__).resolve().with_name("data-parity.py")
)
dp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dp)


class CanonTest(unittest.TestCase):
    def test_matches_rust_ingest_canon(self):
        self.assertEqual(dp.canon("  OH-my  Boring  DB "), "oh-my-boring-db")
        self.assertEqual(dp.canon("foodspring front"), "foodspring-front")
        self.assertEqual(dp.canon("a_b--c"), "a-b-c")

    def test_edges(self):
        self.assertEqual(dp.canon(""), "")
        self.assertEqual(dp.canon("---"), "")
        self.assertEqual(dp.canon("-a-"), "a")
        self.assertEqual(dp.canon("MiXeD CASE"), "mixed-case")


class SymDiffTest(unittest.TestCase):
    def test_direction_and_size(self):
        a = {("x", 1), ("y", 2)}
        b = {("y", 2), ("z", 3)}
        only_a, only_b = dp.symdiff(a, b)
        self.assertEqual(only_a, [("x", 1)])
        self.assertEqual(only_b, [("z", 3)])

    def test_identical_sets_are_empty(self):
        s = {("x", 1)}
        self.assertEqual(dp.symdiff(s, set(s)), ([], []))


class NormalizeCardTest(unittest.TestCase):
    def test_register_folds_spelling_and_order(self):
        a = {
            "answer": "Showing 2 of 2 matching claims (limit_applied=false).\n"
            "* next-step — action: sanitization (kind=next, confidence=certain)\n"
            "* boro vigil — incident: orca down (kind=risk, confidence=certain)",
            "sources": ["next-step", "boro vigil"],
            "injected_claims": [],
        }
        b = {
            "answer": "Showing 2 of 2 matching claims (limit_applied=false).\n"
            "* Boro  Vigil — incident: orca down (kind=risk, confidence=certain)\n"
            "* next_step — action: sanitization (kind=next, confidence=certain)",
            "sources": ["boro vigil", "next_step"],
            "injected_claims": [],
        }
        self.assertEqual(dp.normalize_card("stalled", a), dp.normalize_card("stalled", b))

    def test_register_catches_value_change(self):
        base = {
            "answer": "Showing 1 of 1 matching claims (limit_applied=false).\n"
            "* s — action: 원래 값 (kind=next, confidence=certain)",
            "sources": ["s"],
            "injected_claims": [],
        }
        changed = {
            "answer": "Showing 1 of 1 matching claims (limit_applied=false).\n"
            "* s — action: 바뀐 값 (kind=next, confidence=certain)",
            "sources": ["s"],
            "injected_claims": [],
        }
        self.assertNotEqual(
            dp.normalize_card("next_actions", base), dp.normalize_card("next_actions", changed)
        )

    def test_recurrences_folds_older_order(self):
        row = {
            "newer": {"subject": "boro-vigil", "value": "x"},
            "older": [{"subject": "boro vigil", "value": "y"}, {"subject": "boro-vigil", "value": "z"}],
        }
        row_swapped = {
            "newer": {"subject": "boro vigil", "value": "x"},
            "older": [{"subject": "boro-vigil", "value": "z"}, {"subject": "boro  vigil", "value": "y"}],
        }
        a = {"rows": [row], "days": 30, "max_distance": 0.2, "min_days_apart": 3}
        b = {"rows": [row_swapped], "days": 30, "max_distance": 0.2, "min_days_apart": 3}
        self.assertEqual(dp.normalize_card("recurrences", a), dp.normalize_card("recurrences", b))


if __name__ == "__main__":
    unittest.main()
