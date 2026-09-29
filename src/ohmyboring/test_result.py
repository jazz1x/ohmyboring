#!/usr/bin/env python3
"""ohmyboring.result 한 벌 — Ok·Err 의 모양과 bind·map_ok 의 흐름을 못 박는다.

Run: python3 ohmyboring/test_result.py   (no pytest dependency)

Mutation targets: bind 가 Err 에서 f 를 부르는 변이, map_ok 가 Err 를 바꾸는 변이,
Ok/Err 가 mutable 하거나 동등 비교가 안 되는 변이 각각 시험으로 사망 확인.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ohmyboring.result import Either, Err, Ok, bind, map_ok  # noqa: E402


class OkErrShapeTest(unittest.TestCase):
    def test_ok_carries_its_value(self):
        ok = Ok(3)
        self.assertEqual(ok.value, 3)

    def test_err_carries_its_error(self):
        err: Either[int, str] = Err("boom")
        self.assertEqual(err.error, "boom")

    def test_they_are_frozen_and_equal_by_fields(self):
        with self.assertRaises(AttributeError):
            Ok(1).value = 2  # type: ignore[misc]
        self.assertEqual(Ok([1]), Ok([1]))
        self.assertEqual(Err("x"), Err("x"))
        self.assertNotEqual(Ok(1), Err(1))

    def test_either_is_the_union_alias(self):
        results: list[Either[int, str]] = [Ok(1), Err("no")]
        self.assertEqual([type(r).__name__ for r in results], ["Ok", "Err"])


class BindTest(unittest.TestCase):
    def test_bind_threads_ok_into_the_next_step(self):
        stepped = bind(Ok(2), lambda n: Ok(n * 10))
        self.assertEqual(stepped, Ok(20))

    def test_bind_skips_the_step_on_err(self):
        calls: list[int] = []

        def step(n: int) -> Either[int, str]:
            calls.append(n)
            return Ok(n)

        self.assertEqual(bind(Err("stop"), step), Err("stop"))
        self.assertEqual(calls, [], "Err 면 f 는 부르지 않는다")

    def test_bind_flattens_and_keeps_the_latest_err(self):
        self.assertEqual(bind(Err("first"), lambda n: Err("second")), Err("first"))


class MapOkTest(unittest.TestCase):
    def test_map_ok_rewrites_only_the_ok_value(self):
        self.assertEqual(map_ok(Ok(2), lambda n: n + 1), Ok(3))

    def test_map_ok_leaves_err_untouched(self):
        mapped = map_ok(Err("keep"), lambda n: n + 1)
        self.assertEqual(mapped, Err("keep"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
