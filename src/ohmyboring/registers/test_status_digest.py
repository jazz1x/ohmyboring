"""render_status_answer 가 엔진 렌더러와 같은 고정 답을 내는지 본다.

재료와 기대 답은 drudge/tests/fixtures/status_digest.json 한 파일을 양쪽이 같이 읽는다
(Rust 쪽: drudge/src/ask.rs status_digest_matches_shared_fixture).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from ohmyboring.registers import pg as registers_pg

FIXTURE = Path(__file__).resolve().parents[3] / "drudge" / "tests" / "fixtures" / "status_digest.json"


class StatusDigestFixtureTest(unittest.TestCase):
    def test_render_matches_shared_fixture(self) -> None:
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(cases), 3)
        for case in cases:
            material = registers_pg.StatusMaterial(
                docs=tuple(
                    registers_pg.StatusDoc(d["source_path"], "", [], d["content"]) for d in case["docs"]
                ),
                claims=tuple(
                    registers_pg.RegisterRow(
                        c["subject"], c["predicate"], c["value"], c["kind"], c["confidence"]
                    )
                    for c in case["claims"]
                ),
            )
            with self.subTest(case["name"]):
                self.assertEqual(
                    registers_pg.render_status_answer(material, case["lang"]), case["expected_answer"]
                )


if __name__ == "__main__":
    unittest.main()
