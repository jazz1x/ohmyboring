#!/usr/bin/env python3
"""hits.py 시험 — Document → hit dict 의 생략 규칙.

Run: python3 ohmyboring/search/test_hits.py   (no pytest dependency)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from langchain_core.documents import Document  # noqa: E402

from ohmyboring.search.hits import document_to_hit  # noqa: E402


def doc(**metadata_overrides) -> Document:
    metadata = {
        "source_path": "/w.md",
        "project": "omb",
        "origin": "personal",
        "used_count": 2,
        "contested_count": 1,
        "said_by_owner": 0,
        "superseded_by": [],
        "dist": 0.25,
        "dist_kind": "vector_cosine",
    }
    metadata.update(metadata_overrides)
    return Document(id="/w.md#0", page_content="조각", metadata=metadata)


class HitShapeTests(unittest.TestCase):
    def test_always_present_keys(self):
        hit = document_to_hit(doc(), claims_requested=False)
        self.assertEqual(
            list(hit),
            [
                "id",
                "origin",
                "project",
                "source_path",
                "snippet",
                "dist",
                "dist_kind",
                "used_count",
                "contested_count",
                "said_by_owner",
            ],
        )
        self.assertEqual(hit["snippet"], "조각")
        self.assertEqual(hit["dist"], 0.25)

    def test_superseded_by_omitted_when_empty_present_when_not(self):
        self.assertNotIn("superseded_by", document_to_hit(doc(), claims_requested=False))
        hit = document_to_hit(doc(superseded_by=["/new.md"]), claims_requested=False)
        self.assertEqual(hit["superseded_by"], ["/new.md"])

    def test_related_omitted_when_empty_and_placed_in_searchhit_field_order(self):
        self.assertNotIn("related", document_to_hit(doc(), claims_requested=False))
        related = [{"source_path": "/x.md", "snippet": "옛 관련 노트"}]
        hit = document_to_hit(doc(related=related, superseded_by=["/new.md"]), claims_requested=False)
        self.assertEqual(hit["related"], related)
        keys = list(hit)
        self.assertEqual(keys.index("related"), 7, "serve.rs SearchHit 필드 순 — dist_kind 뒤")
        self.assertLess(keys.index("related"), keys.index("superseded_by"))

    def test_claims_requested_rules(self):
        # 요청 안 함 — 둘 다 생략.
        hit = document_to_hit(doc(claims_total=3), claims_requested=False)
        self.assertNotIn("claims", hit)
        self.assertNotIn("claims_total", hit)
        # 요청, 행 있음.
        row = {
            "node_id": "claim:s:p",
            "subject": "s",
            "predicate": "p",
            "value": "v",
            "kind": "decision",
            "confidence": "certain",
            "valid_from": "2026-09-01T00:00:00+00:00",
            "project": "omb",
        }
        hit = document_to_hit(doc(claims=[row], claims_total=1), claims_requested=True)
        self.assertEqual(hit["claims"], [row])
        self.assertEqual(hit["claims_total"], 1)
        # 요청, 행 없음 — claims 는 생략, total 은 0 (Rust 의 Some(0)).
        hit = document_to_hit(doc(), claims_requested=True)
        self.assertNotIn("claims", hit)
        self.assertEqual(hit["claims_total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
