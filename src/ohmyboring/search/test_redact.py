#!/usr/bin/env python3
"""redact.py 비밀 가림 시험 — drudge/src/redact.rs 시험의 이식 + pg.log_query 경계.

Run: python3 ohmyboring/search/test_redact.py   (no pytest dependency)

토큰 가족마다 합성 토큰 하나씩 — 실제 키는 절대 시험에 안 쓴다. 깨끗한 한글·영문
문장은 바이트 그대로 살아야 한다.

Mutation targets: log_query 의 redact 호출을 빼는 변이는 pg 수준 시험이
(가짜 커서가 받은 값으로) 사망 확인, SECRET_PATTERN 의 (?i:...) 그룹을 빼는 변이는
password=… 대문자·소문자 쌍 사망 확인.
"""

from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ohmyboring.result import Err, Ok  # noqa: E402
from ohmyboring.search import pg, redact  # noqa: E402

SCRUB_CASES: tuple[tuple[str, str, str], ...] = (
    # (가족, 합성 토큰을 품은 더러운 문장, 사라져야 할 부분)
    ("slack-bot", "prefix xoxb-1234567890abcdef suffix", "xoxb-1234567890abcdef"),
    ("slack-app", "prefix xapp-1234567890abcdef suffix", "xapp-1234567890abcdef"),
    ("anthropic", "prefix sk-ant-abcdefghij1234567890XYZ suffix", "sk-ant-abcdefghij1234567890XYZ"),
    ("openai", "prefix sk-abcdefghij1234567890XYZ suffix", "sk-abcdefghij1234567890XYZ"),
    ("aws", "prefix AKIAIOSFODNN7EXAMPLE suffix", "AKIAIOSFODNN7EXAMPLE"),
    (
        "github",
        "prefix ghp_16C7e42F292c6912E7710c838347Ae178B4a suffix",
        "ghp_16C7e42F292c6912E7710c838347Ae178B4a",
    ),
    (
        "github-pat",
        "prefix github_pat_11ABCDEFG0hijklmn1234567890qrstuvwx_0123456789 suffix",
        "github_pat_11ABCDEFG0hijklmn1234567890qrstuvwx_0123456789",
    ),
    (
        "google",
        "prefix AIzaSyD4iE7xn0abcdefghijklmnopqrstuvwxy suffix",
        "AIzaSyD4iE7xn0abcdefghijklmnopqrstuvwxy",
    ),
    ("aqo", "prefix AQoEabcd1234+/==xyzABCDEfghij suffix", "AQoEabcd1234+/==xyzABCDEfghij"),
    (
        "jwt",
        "prefix eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4JCm3dOkqsM0k suffix",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4JCm3dOkqsM0k",
    ),
    ("private-key", "prefix -----BEGIN RSA PRIVATE KEY----- suffix", "-----BEGIN RSA PRIVATE KEY-----"),
    ("api-key", "prefix api_key=abcdef1234567890 suffix", "abcdef1234567890"),
    ("secret", "prefix secret: abcdef1234567890 suffix", "abcdef1234567890"),
    ("token", "prefix token='abcdef1234567890' suffix", "abcdef1234567890"),
    ("password", "prefix password=abcdefghijklmnop1234 훅", "abcdefghijklmnop1234"),
    ("passwd", "prefix passwd: abcdef1234567890 suffix", "abcdef1234567890"),
    ("bearer", "prefix bearer=abcdef1234567890 suffix", "abcdef1234567890"),
    (
        "case-insensitive-password",
        "prefix PASSWORD: ABCDEF1234567890 훅",
        "ABCDEF1234567890",
    ),
)


class RedactTests(unittest.TestCase):
    """redact.rs 의 redact_scrubs_known_tokens / redact_leaves_clean_text 이식."""

    def test_redact_scrubs_known_tokens(self):
        """토큰 가족마다 합성 토큰 하나씩 — 전부 ‹REDACTED› 로 바뀌고 주변 문장은 산다."""
        for family, dirty, gone in SCRUB_CASES:
            with self.subTest(family=family):
                clean = redact.redact(dirty)
                self.assertNotIn(gone, clean, f"{family} 토큰이 남았다: {clean}")
                self.assertIn("‹REDACTED›", clean, f"{family} 가림 표시가 없다: {clean}")
                self.assertTrue(clean.startswith("prefix "), f"{family} 앞 문맥이 사라졌다: {clean}")

    def test_redact_leaves_clean_text(self):
        """깨끗한 한글·영문 문장은 바이트 그대로 — 가림 경계가 내용을 먹지 않는다."""
        for clean in (
            "Just an ordinary plain sentence.",
            "훅 이중 등록 체크아웃 두 개 — 평범한 한글 문장입니다.",
            "redact probe 훅",
        ):
            self.assertEqual(redact.redact(clean), clean)


class LogQueryScrubTests(unittest.TestCase):
    """pg.log_query 가 적기 전 가림 — 가짜 커서가 받은 값으로 본다 (DB 없음)."""

    def test_log_query_redacts_query_and_answer_snippet_before_insert(self):
        conn = unittest.mock.MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        row = pg.QueryLogRow(
            endpoint="search",
            query="redact probe password=abcdefghijklmnop1234 훅",
            logged_hits=(),
            sources=(),
            answer_snippet="답에 xoxb-1234567890abcdef 실림",
            latency_ms=3,
        )
        match pg.log_query(conn, row):
            case Ok(_):
                pass
            case Err(e):
                self.fail(f"log_query: {e}")
        _, params = cur.execute.call_args[0]
        self.assertEqual(params[1], "redact probe ‹REDACTED› 훅", "query 는 INSERT 전에 가려져야 한다")
        self.assertEqual(params[6], "답에 ‹REDACTED› 실림", "answer_snippet 도 INSERT 전에 가려져야 한다")
        conn.commit.assert_called_once_with()


if __name__ == "__main__":
    unittest.main(verbosity=2)
