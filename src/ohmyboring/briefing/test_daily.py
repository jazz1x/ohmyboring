"""일일 브리프 노트 시험 — 파일이 있으면 쓰지 않는다, 쓴 노트를 주간 카드 읽기가 푼다."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
for p in (ROOT / "src", ROOT / "agents" / "shared"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from vault_note import split_frontmatter  # noqa: E402

from ohmyboring.briefing import daily, generate  # noqa: E402
from ohmyboring.result import Err, Ok  # noqa: E402
from ohmyboring.weekly.nodes.read_week import read_days  # noqa: E402

NOW = datetime(2026, 10, 23, 8, 0).astimezone()
ANSWER = "## omb\n- Done: 문 안에 브리프를 옮겼다\n- Next: 08시 실행을 지켜본다"


def _deps(wiki_dir: str, connect) -> daily.BriefDeps:
    return daily.BriefDeps(
        connect=connect,
        generate=lambda s, p: Ok("unused"),
        lang="ko",
        wiki_dir=wiki_dir,
        append_event=lambda *a, **k: True,
        clock=lambda: NOW,
    )


def _no_db():
    raise AssertionError("기존 파일이 있는데 DB 를 열었다")


class DailyBrief(unittest.TestCase):
    def test_existing_note_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(daily.note_path(tmp, "2026-10-23"))
            path.write_text("손으로 쓴 노트", encoding="utf-8")
            result = daily.run_brief(_deps(tmp, _no_db))
            self.assertEqual(path.read_text(encoding="utf-8"), "손으로 쓴 노트")
            self.assertIsInstance(result, Ok)
            self.assertEqual(result.value.status, "exists")

    def test_weekly_read_parses_the_note_the_door_wrote(self) -> None:
        out = generate.BriefOut(ANSWER, ("wiki/a.md", "wiki/b.md"), 24, 2)
        with tempfile.TemporaryDirectory() as tmp:
            conn = mock.MagicMock()
            conn.__enter__.return_value = conn
            with mock.patch.object(generate, "brief", return_value=Ok(out)):
                result = daily.run_brief(_deps(str(Path(tmp) / "wiki"), lambda: conn))
            self.assertNotIsInstance(result, Err)
            raw = Path(daily.note_path(str(Path(tmp) / "wiki"), "2026-10-23")).read_text(encoding="utf-8")
            keys = [
                ln.split(":")[0] for ln in split_frontmatter(raw)[0].splitlines() if not ln.startswith(" ")
            ]
            self.assertEqual(keys, ["title", "origin", "date", "kind", "tags", "brief_sources"])
            days = read_days(NOW, split_frontmatter, tmp, span=1)
            self.assertEqual([d for d, _ in days], ["2026-10-23"])
            self.assertTrue(days[0][1])


if __name__ == "__main__":
    unittest.main()
