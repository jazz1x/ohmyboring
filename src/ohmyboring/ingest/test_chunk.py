#!/usr/bin/env python3
"""chunk.py — 고정 자르기가 Rust 규칙과 같고, 제목 경계 자르기가 도는지.

Run: python3 src/ohmyboring/ingest/test_chunk.py   (no pytest dependency)

고정 변이: 1500 이하 한 조각 규칙·3201자 경계(0/1300/2600)·한글/이모지 코드 포인트·
겹침 200·절 묶기 한도·긴 절 고정 쪼개기·제목 줄 을 하나씩 빼면 여기 시험이 빨갛게 끝난다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ohmyboring.ingest.chunk import chunk_stats, fixed_chunks, heading_chunks  # noqa: E402


class FixedChunksTests(unittest.TestCase):
    def test_short_body_is_one_chunk(self):
        for body in ("", "짧은 본문", "가" * 1500):
            self.assertEqual(fixed_chunks(body), [body])

    def test_exactly_1500_is_one_chunk(self):
        body = "가" * 1500
        self.assertEqual(fixed_chunks(body), [body])

    def test_1501_splits_with_200_overlap(self):
        body = "나" * 1501
        pieces = fixed_chunks(body)
        self.assertEqual(len(pieces), 2)
        self.assertEqual(len(pieces[0]), 1500)
        self.assertEqual(len(pieces[1]), 201)
        self.assertEqual(pieces[1], "나" * 200 + "나", "둘째 조각은 1300..1501")
        self.assertEqual(pieces[0][1300:], pieces[1][:200], "겹침 200")

    def test_3201_yields_three_chunks_at_0_1300_2600(self):
        body = "".join(str(i % 10) for i in range(3201))
        pieces = fixed_chunks(body)
        self.assertEqual(len(pieces), 3)
        self.assertEqual(len(pieces[0]), 1500)
        self.assertEqual(len(pieces[1]), 1500)
        self.assertEqual(len(pieces[2]), 601)
        self.assertEqual(pieces[0], body[0:1500])
        self.assertEqual(pieces[1], body[1300:2800])
        self.assertEqual(pieces[2], body[2600:3201])

    def test_counts_code_points_not_bytes(self):
        body = "한🙂글" * 500
        self.assertEqual(len(body), 1500)
        self.assertEqual(fixed_chunks(body), [body])
        body2 = "한🙂글" * 501
        pieces = fixed_chunks(body2)
        self.assertEqual(len(pieces), 2)
        self.assertEqual(len(pieces[1]), 203, "1501 코드 포인트 → 둘째 조각 1300..1501")

    def test_degenerate_config_returns_whole(self):
        body = "x" * 5000
        self.assertEqual(fixed_chunks(body, size=0), [body])
        self.assertEqual(fixed_chunks(body, size=200, overlap=200), [body])


class HeadingChunksTests(unittest.TestCase):
    def test_sections_pack_into_one_chunk_under_max(self):
        body = "## A\n가나다\n## B\n라마바사"
        self.assertEqual(heading_chunks("제목", body), ["# 제목\n## A\n가나다\n## B\n라마바사"])

    def test_overlong_section_splits_alone(self):
        body = "## A\n" + "x" * 2000
        chunks = heading_chunks("제목", body)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(c.startswith("# 제목\n") for c in chunks))
        head0, body0 = chunks[0].split("\n", 1)
        head1, body1 = chunks[1].split("\n", 1)
        self.assertEqual(head0, "# 제목")
        self.assertEqual(len(body0), 1500)
        self.assertTrue(body0.startswith("## A\n"))
        self.assertEqual(len(body1), 705, "2005 자 절은 1500+705")
        self.assertEqual(body0[1300:], body1[:200], "겹침 200")

    def test_packing_respects_max(self):
        body = "## A\n" + "a" * 900 + "\n## B\n" + "b" * 900
        chunks = heading_chunks("제목", body, max_chars=1000)
        self.assertEqual(len(chunks), 2, "900+900 은 1000 을 넘어 두 조각")
        self.assertEqual(chunks[0], "# 제목\n## A\n" + "a" * 900)
        self.assertEqual(chunks[1], "# 제목\n## B\n" + "b" * 900)

    def test_body_without_headings_is_one_chunk(self):
        self.assertEqual(heading_chunks("제목", "그냥 본문"), ["# 제목\n그냥 본문"])

    def test_empty_title_uses_placeholder(self):
        self.assertEqual(heading_chunks("", "본문"), ["# (제목 없음)\n본문"])

    def test_h3_does_not_split(self):
        body = "## A\n일\n### 깊은\n이\n## B\n삼"
        self.assertEqual(len(heading_chunks("t", body)), 1, "### 은 ## 경계가 아니다")

    def test_preamble_stays_before_first_heading(self):
        body = "머리말 본문\n## A\n일"
        self.assertEqual(heading_chunks("t", body), ["# t\n머리말 본문\n## A\n일"])


class ChunkStatsTests(unittest.TestCase):
    def test_distribution_side_by_side(self):
        stats = chunk_stats(
            [
                ("t1", "## A\n" + "x" * 4000),
                ("t2", "짧음"),
                ("t3", "   "),
            ]
        )
        self.assertEqual(stats["notes"], 2, "본문 없는 노트는 분모에서 빠진다")
        self.assertEqual(stats["notes_over_1500_chars"], 1)
        self.assertEqual(stats["chunks_fixed_total"], 4)
        self.assertGreaterEqual(stats["heading_chunk_chars_max"], 1500)

    def test_empty_population(self):
        self.assertEqual(chunk_stats([]), {"notes": 0, "notes_over_1500_chars": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
