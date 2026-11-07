#!/usr/bin/env python3
"""relates_to 투영 시험 — 가짜 커서(실행된 SQL 기록)·임시 볼트로 파일 바이트를 본다.

Run: python3 src/ohmyboring/sync/test_links.py   (no pytest dependency)

1. 링크가 0 이면 파일을 쓰지 않는다 — 변이: links.plan_note 의 「링크 0」 갈래를 지우면 빨강.
2. seed 노트(wiki-0000)는 쓰지 않고 질의도 하지 않는다 — 변이: seed 건너뛰기를 지우면 빨강.
3. relates_to 만 바뀌고 다른 머리말 칸·본문 바이트는 그대로, 두 번째 회차는 쓰지 않는다.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ohmyboring.sync import links  # noqa: E402

_WIKI = "/vault/wiki"


class _Cursor:
    """질의별 답을 돌려준다. answers: SQL 상수 → (params → 행 목록)."""

    def __init__(self, answers: dict, log: list) -> None:
        self._answers = answers
        self._log = log
        self._rows: list = []

    def execute(self, sql, params=None):
        self._log.append((sql, params))
        self._rows = self._answers.get(sql, lambda _params: [])(params)

    def fetchall(self):
        return list(self._rows)


def _neighbors(*stems: str) -> dict:
    """어느 노트에도 같은 이웃을 답하는 DB — 그래프 길(related)만 채운다."""
    return {links.RELATED_DOCS_SQL: lambda _p: [(f"doc:{_WIKI}/{s}.md", 2) for s in stems]}


def _project(wiki: Path, answers: dict):
    log: list = []
    report = links.run_links(_Cursor(answers, log), str(wiki))
    return report, log


_FRONT = (
    "id: wiki-0001\n"
    'title: "따옴표: 있는 제목"\n'
    "kind: note\n"
    "relates_to:\n"
    '- "[[wiki-0009]]"\n'
    '- "[[wiki-0008]]"\n'
    "tags:\n"
    "- a\n"
    "- b\n"
    "claims:\n"
    "- subject: x\n"
    "  predicate: y\n"
    '  value: "z"'
)
_BODY = "본문 첫 줄\r\n둘째 줄\n\n\t끝 공백 "


def _note(front: str, body: str = _BODY) -> str:
    return f"---\n{front}\n---\n{body}"


class ProjectionTests(unittest.TestCase):
    def test_zero_links_leaves_the_file_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp)
            note = wiki / "wiki-0001.md"
            original = _note(_FRONT).encode("utf-8")
            note.write_bytes(original)
            report, log = _project(wiki, {})
            after = note.read_bytes()
        self.assertEqual(after, original)
        self.assertEqual((report.empty, report.updated), (1, 0))
        self.assertTrue(log, "질의가 실행되지 않았다 — 시험이 빈 DB 를 본 것이 아니다")

    def test_seed_note_is_not_written_and_not_queried(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp)
            seed = wiki / "wiki-0000.md"
            original = _note("id: wiki-0000\nrelates_to: []").encode("utf-8")
            seed.write_bytes(original)
            report, log = _project(wiki, _neighbors("wiki-0002", "wiki-0003"))
            after = seed.read_bytes()
        self.assertEqual(after, original)
        self.assertEqual((report.skipped_seed, report.updated), (1, 0))
        self.assertEqual(log, [])

    def test_only_relates_to_changes_and_second_pass_writes_nothing(self):
        answers = _neighbors("wiki-0002", "wiki-0003")
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp)
            note = wiki / "wiki-0001.md"
            note.write_bytes(_note(_FRONT).encode("utf-8"))
            first, _ = _project(wiki, answers)
            written = note.read_bytes()
            second, _ = _project(wiki, answers)
            again = note.read_bytes()
        new_block = 'relates_to:\n- "[[wiki-0002]]"\n- "[[wiki-0003]]"'
        old_block = 'relates_to:\n- "[[wiki-0009]]"\n- "[[wiki-0008]]"'
        self.assertEqual(written, _note(_FRONT.replace(old_block, new_block)).encode("utf-8"))
        self.assertEqual((first.updated, second.updated, second.unchanged), (1, 0, 1))
        self.assertEqual(again, written)

    def test_missing_key_is_appended_after_the_last_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp)
            note = wiki / "wiki-0001.md"
            note.write_bytes(_note("id: wiki-0001\nkind: note").encode("utf-8"))
            _project(wiki, _neighbors("wiki-0002"))
            after = note.read_bytes()
        self.assertEqual(
            after, _note('id: wiki-0001\nkind: note\nrelates_to:\n- "[[wiki-0002]]"').encode("utf-8")
        )

    def test_preview_counts_without_touching_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp)
            note = wiki / "wiki-0001.md"
            note.write_bytes(_note(_FRONT).encode("utf-8"))
            before = (note.read_bytes(), os.stat(note).st_mtime_ns)
            report = links.preview_links(_Cursor(_neighbors("wiki-0002"), []), str(wiki))
            after = (note.read_bytes(), os.stat(note).st_mtime_ns)
        self.assertEqual(after, before)
        self.assertEqual((report.would_update, report.updated), (1, 0))

    def test_only_top_level_wiki_md_files_are_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp)
            (wiki / "sub").mkdir()
            for name in ("sub/wiki-0005.md", "other-0001.md", "wiki-0006.txt"):
                (wiki / name).write_bytes(_note("id: x").encode("utf-8"))
            report, log = _project(wiki, _neighbors("wiki-0002"))
        self.assertEqual((report.updated, report.empty, report.unchanged), (0, 0, 0))
        self.assertEqual(log, [])


class MergeStemsTests(unittest.TestCase):
    def test_graph_first_then_semantic_deduped_and_non_wiki_dropped(self):
        stems = links.merge_stems(
            ["/v/wiki/wiki-0001.md", "/v/notes/other.md"],
            ["/v/wiki/wiki-0001.md", "/v/wiki/wiki-0002.md"],
            lambda: self.fail("이웃이 2개 이상이면 최근 문서를 묻지 않는다"),
        )
        self.assertEqual(stems, ["wiki-0001", "wiki-0002"])

    def test_recent_project_docs_fill_in_only_below_two_and_cap_is_eight(self):
        self.assertEqual(
            links.merge_stems(
                [], ["/v/wiki/wiki-0003.md"], lambda: ["/v/wiki/wiki-0003.md", "/v/wiki/wiki-0004.md"]
            ),
            ["wiki-0003", "wiki-0004"],
        )
        many = [f"/v/wiki/wiki-{n:04d}.md" for n in range(1, 11)]
        self.assertEqual(len(links.merge_stems(many, [], lambda: [])), 8)


if __name__ == "__main__":
    unittest.main()
