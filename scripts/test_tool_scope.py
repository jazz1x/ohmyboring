"""The one test C-TOOLLEAK1 owes: the quote check (인용 수 ≠ 표 수 → 실패)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tool_scope  # noqa: E402


def _row(row_id, calls, sessions, repos):
    return {"id": row_id, "calls": calls, "sessions": sessions, "repos": repos, "den": 300, "share_pct": 0.3}


class QuoteCheck(unittest.TestCase):
    def test_fails_on_a_missing_row_and_on_a_cite_the_table_does_not_hold(self):
        table = [_row("a0faa78e|connector:Datadog_2", 7, 1, 1), _row("a0faa78e|builtin:Artifact", 10, 2, 2)]
        answers = {
            # cites a value the table does not hold: calls is 7, not 12
            "a0faa78e|connector:Datadog_2": {
                "scope": "끔",
                "reason": "x",
                "cites": {"calls": 12, "sessions": 1},
            },
        }
        check = tool_scope.check_quotes(table, answers)
        self.assertEqual(check.missing, ["a0faa78e|builtin:Artifact"])
        self.assertEqual(check.mismatches, [("a0faa78e|connector:Datadog_2", "calls", 12, 7)])
        self.assertFalse(check.ok)

        answers["a0faa78e|connector:Datadog_2"]["cites"]["calls"] = 7
        answers["a0faa78e|builtin:Artifact"] = {
            "scope": "필요할 때만",
            "reason": "y",
            "cites": {"sessions": 2},
        }
        self.assertTrue(tool_scope.check_quotes(table, answers).ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
