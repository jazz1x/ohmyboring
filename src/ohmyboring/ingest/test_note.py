#!/usr/bin/env python3
"""note.py — 볼트 노트 파싱이 Rust 동작을 미러하는지.

Run: python3 src/ohmyboring/ingest/test_note.py   (no pytest dependency)

고정 변이: BOM 제거·NUL 제거·utf-8 거절·`\\n---\\n` 경계·trim_start 를 하나씩 빼면
여기 시험이 빨갛게 끝난다.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ohmyboring.ingest.note import Skipped, read_note  # noqa: E402
from ohmyboring.result import Err, Ok  # noqa: E402


class ReadNoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, content: str | bytes) -> str:
        path = self.root / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return str(path)

    def test_frontmatter_title_kind_tags_and_trimmed_body(self):
        path = self._write(
            "n.md",
            "---\norigin: personal\ntitle: 실험 노트\nkind: wiki\ntags: [a, b]\n---\n\n  본문 시작\n둘째 줄\n",
        )
        result = read_note(path)
        assert isinstance(result, Ok), result
        note = result.value
        self.assertEqual(note.title, "실험 노트")
        self.assertEqual(note.kind, "wiki")
        self.assertEqual(note.tags, ("a", "b"))
        self.assertEqual(note.body, "본문 시작\n둘째 줄\n")
        self.assertEqual(note.source_path, path)

    def test_sha_is_over_nul_stripped_content(self):
        path = self._write("n.md", b"a\x00b")
        raw = Path(path).read_bytes()
        result = read_note(path)
        assert isinstance(result, Ok), result
        self.assertEqual(result.value.body, "ab")
        self.assertEqual(result.value.sha, hashlib.sha256(b"ab").hexdigest())
        self.assertEqual(raw, b"a\x00b", "사전: 파일에 NUL 이 있다")

    def test_bom_is_parsed_away_but_stays_in_sha(self):
        path = self._write("n.md", b"\xef\xbb\xbf" + "---\ntitle: T\n---\n본문".encode())
        result = read_note(path)
        assert isinstance(result, Ok), result
        note = result.value
        self.assertEqual(note.title, "T")
        self.assertEqual(note.body, "본문")
        bom_text = Path(path).read_bytes().decode("utf-8")
        self.assertEqual(note.sha, hashlib.sha256(bom_text.encode("utf-8")).hexdigest())

    def test_without_frontmatter_derives_kind_from_path(self):
        note_path = self._write("plain.md", "  그냥 본문")
        doc_path = self._write("wiki-z.md", "z")
        for path, want_kind, want_body in (
            (note_path, "doc", "그냥 본문"),
            (doc_path, "doc", "z"),
        ):
            result = read_note(path)
            assert isinstance(result, Ok), result
            self.assertEqual(result.value.kind, want_kind)
            self.assertEqual(result.value.body, want_body)
            self.assertIsNone(result.value.title)
        notes_dir = self.root / "notes"
        notes_dir.mkdir()
        proj_dir = self.root / "proj"
        proj_dir.mkdir()
        p = self._write(str(notes_dir / "a.md"), "b")
        nested = self._write(str(proj_dir / "memory-1.md"), "b")
        self.assertEqual(read_note(p).value.kind, "note")
        self.assertEqual(read_note(nested).value.kind, "memory")

    def test_missing_file_is_skipped_with_reason(self):
        result = read_note(str(self.root / "없는파일.md"))
        assert isinstance(result, Err), result
        self.assertIsInstance(result.error, Skipped)
        self.assertTrue(result.error.reason.startswith("unreadable:"))

    def test_non_utf8_file_is_skipped_with_reason(self):
        path = self._write("bad.md", b"\xff\xfe\xfd")
        result = read_note(path)
        assert isinstance(result, Err), result
        self.assertIn("not utf-8", result.error.reason)

    def test_malformed_yaml_is_skipped(self):
        path = self._write("bad.md", "---\norigin: [unclosed\n---\n본문")
        result = read_note(path)
        assert isinstance(result, Err), result
        self.assertTrue(result.error.reason.startswith("yaml:"))

    def test_frontmatter_boundary_needs_newline_dashes(self):
        path = self._write("n.md", "---\ntitle: T\n---\n\n## 절\n본문\n---\n꼬리")
        result = read_note(path)
        assert isinstance(result, Ok), result
        self.assertEqual(result.value.title, "T")
        self.assertEqual(result.value.body, "## 절\n본문\n---\n꼬리")

    def test_kind_default_and_block_tags(self):
        path = self._write(
            "n.md",
            "---\ntags:\n  - rust\n  - rop\n---\n본문",
        )
        note = read_note(path).value
        self.assertEqual(note.tags, ("rust", "rop"))
        self.assertEqual(note.kind, "doc")

    def test_tags_shape_must_be_list(self):
        path = self._write("n.md", "---\ntags: 그냥문자열\n---\n본문")
        result = read_note(path)
        assert isinstance(result, Err), result
        self.assertIn("tags", result.error.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
