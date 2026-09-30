"""The polish step keeps the original unless the rewrite carries every fact and reads better."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", ".."))

from ohmyboring.distill import polish  # noqa: E402

WALL = (
    "2026-09-29 조사. 트렁크 d3088bb 을 올리고 문(:7710) 을 다시 띄웠다 — wiki-2278 참고, `make door-up` 실행, "
    + "설명 " * 150
    + "\n"
)
TIDY = (
    "2026-09-29 조사, 트렁크 d3088bb 를 배달했다.\n\n## 배달\n- 문(:7710) 재기동 — wiki-2278 참고\n"
    "## 확인\n- `make door-up` 실행\n"
)


class PolishTests(unittest.TestCase):
    def test_a_rewrite_that_keeps_every_fact_and_reads_better_replaces_the_body(self):
        result = polish.polish(WALL, "ko", lambda prompt: {"body": TIDY})
        self.assertEqual(result, polish.Polished(body=TIDY))

    def test_a_rewrite_that_drops_a_fact_keeps_the_original_and_names_the_fact(self):
        result = polish.polish(WALL, "ko", lambda prompt: {"body": TIDY.replace(":7710", "")})
        self.assertIsInstance(result, polish.Kept)
        self.assertIn("7710", result.reason)

    def test_a_rewrite_that_reads_no_better_is_not_taken(self):
        result = polish.polish(WALL, "ko", lambda prompt: {"body": WALL + "추가"})
        self.assertIsInstance(result, polish.Kept)
        self.assertIn("no better", result.reason)

    def test_a_readable_body_never_reaches_the_model(self):
        def no_call(prompt):
            raise AssertionError("the model must not be called for a readable body")

        self.assertEqual(polish.polish(TIDY, "ko", no_call), polish.Kept(reason="already readable"))

    def test_the_markdown_shape_is_required_and_a_table_counts_as_a_list(self):
        no_summary = "## 배달\n- 문(:7710) — wiki-2278, d3088bb, 2026-09-29\n## 확인\n- `make door-up`\n"
        result = polish.polish(WALL, "ko", lambda prompt: {"body": no_summary})
        self.assertIsInstance(result, polish.Kept)
        self.assertIn("no summary", result.reason)
        table = (
            "2026-09-29 조사, d3088bb 배달.\n\n## 배달\n| 무엇 | 값 |\n|---|---|\n| 문 | :7710 |\n"
            "## 확인\n| 명령 | 참고 |\n|---|---|\n| `make door-up` | wiki-2278 |\n"
        )
        self.assertIsInstance(polish.polish(WALL, "ko", lambda prompt: {"body": table}), polish.Polished)

    def test_a_linter_rule_code_may_be_left_out(self):
        with_code = WALL.replace("참고,", "참고, PLR0913 은 불변 요청 타입,")
        self.assertIsInstance(polish.polish(with_code, "ko", lambda prompt: {"body": TIDY}), polish.Polished)

    def test_a_malformed_answer_keeps_the_original(self):
        for answer in (None, [], {"body": ""}, {"text": TIDY}):
            self.assertIsInstance(polish.polish(WALL, "ko", lambda prompt, a=answer: a), polish.Kept)


if __name__ == "__main__":
    unittest.main()
