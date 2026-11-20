#!/usr/bin/env python3
"""충돌 후보 순수 함수 시험 — 식별자 추출·출력 줄. SQL 모양은 test_writer 의 쓰기 길 시험이 본다.

Run: python3 src/ohmyboring/remember/test_conflict.py   (no pytest dependency)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ohmyboring.remember import conflict  # noqa: E402

#: 29254 의 checkout-503-root-cause decision claim (재현 입력).
REPRO = (
    "백엔드가 쿠폰 발급처(couponIssuer)를 null 로 보내 NON_NULL 제약을 위반하므로 주문서 조회가 실패해요. "
    "프론트 쿼리에서 이 칸을 빼지 않기로 했어요."
)


class ExtractIdentifiers(unittest.TestCase):
    def test_reproduction_value_yields_camel_and_screaming_snake(self):
        self.assertEqual(conflict.extract_identifiers(REPRO), ("couponIssuer", "NON_NULL"))

    def test_each_shape(self):
        value = "get_user order.coupon.issuer OrderCoupon couponIssuer MAX_RETRY"
        self.assertEqual(
            conflict.extract_identifiers(value),
            ("get_user", "order.coupon.issuer", "OrderCoupon", "couponIssuer", "MAX_RETRY"),
        )

    def test_control_plain_words_are_not_identifiers(self):
        self.assertEqual(conflict.extract_identifiers("The Door uses null and e.g. fixes it. 쿠폰 발급"), ())

    def test_pattern_needs_whole_token(self):
        import re

        pat = conflict.identifier_pattern("couponIssuer")
        self.assertTrue(re.search(pat, "필드(couponIssuer)를"))
        self.assertIsNone(re.search(pat, "couponIssuerId 가"))


class CandidateLine(unittest.TestCase):
    def test_line_shape_and_80_char_cut(self):
        candidate = conflict.ConflictCandidate(
            "/vault/wiki/wiki-00000022852.md",
            "주문서/contract1/coupon-issuer",
            "verdict",
            "x" * 120,
            ("couponIssuer",),
        )
        self.assertEqual(
            conflict.candidate_line(candidate),
            "후보: wiki-00000022852 주문서/contract1/coupon-issuer/verdict — 겹친 낱말 couponIssuer — "
            + "x" * 80,
        )


if __name__ == "__main__":
    unittest.main()
