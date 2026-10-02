#!/usr/bin/env python3
"""노트 색인 시험 — 문 프로세스 안의 경로 → (바뀜 표지, 파싱 결과) 표의 계약(E3b-2)을 못 박는다.

Run: python3 ohmyboring/remember/test_index.py   (no pytest dependency)

색인은 중복 문이 매 쓰기마다 볼트 전체를 읽고 파싱하던 것을, 쓰기마다 디렉터리 목록·
stat 만 하고 바뀌었거나 새로 생긴 노트만 다시 읽고 파싱하는 형태로 줄인다. 판정은
바꾸지 않는다 — 색인은 읽기를 줄일 뿐이다.

Mutation targets: 바뀐 노트를 다시 읽지 않는 변이(옛 머리말로 판정), 새 노트를
놓치는 변이, 지운 노트를 색인에 남기는 변이, 채우기 도중의 sync 가 빈 색인을
돌려주는 변이 각각 시험으로 사망 확인.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "src", ROOT / "agents" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from vault_note import split_frontmatter  # noqa: E402

from ohmyboring.adapters import vault as vault_notes  # noqa: E402
from ohmyboring.remember import dedup  # noqa: E402
from ohmyboring.remember.dedup import check_duplicate  # noqa: E402
from ohmyboring.remember.index import DiskSeams, NoteIndex  # noqa: E402
from ohmyboring.remember.parse import FrontMatter, RememberNote  # noqa: E402
from ohmyboring.result import Ok  # noqa: E402

NOTE_TEXT = """---
id: {id}
title: {title}
kind: note
origin: personal
project: ''
date: "2026-10-01"
tags: [{tags}]
tools: [{tools}]
concepts: [{concepts}]
claims: [{claims}]
relates_to: []
sources: []
{session}author: {author}
---

{body}
"""


def _note_text(note_id: str, title: str, body: str, **fields) -> str:
    session = fields.get("session")
    session_line = f"omb_session_id: {session}\n" if session else ""
    return NOTE_TEXT.format(
        id=note_id,
        title=title,
        tags=fields.get("tags", ""),
        tools=fields.get("tools", ""),
        concepts=fields.get("concepts", ""),
        claims=fields.get("claims", ""),
        session=session_line,
        author=fields.get("author", "unknown"),
        body=body,
    )


def _write(wiki: Path, note_id: str, title: str, body: str, **fields) -> None:
    (wiki / f"{note_id}.md").write_text(_note_text(note_id, title, body, **fields), encoding="utf-8")


def _note(title: str, body: str, **fields) -> RememberNote:
    front = FrontMatter(
        title=title,
        kind="note",
        origin="personal",
        project="",
        date="",
        tags=fields.get("tags", ()),
        tools=fields.get("tools", ()),
        concepts=fields.get("concepts", ()),
        claims=fields.get("claims", ()),
        sources=fields.get("sources", ()),
        omb_session_id=fields.get("session"),
        author=fields.get("author", "unknown"),
    )
    return RememberNote(front=front, body=body)


class IndexFixture:
    """tmp 볼트 위의 진짜 디스크로 색인을 돌린다 — 목록·stat·읽기가 실제 경로를 본다."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.wiki = Path(self._tmp.name) / "wiki"
        self.wiki.mkdir()

    def cleanup(self) -> None:
        self._tmp.cleanup()

    def list_notes(self) -> list[str]:
        return sorted(p.name[: -len(".md")] for p in self.wiki.iterdir() if p.name.endswith(".md"))

    def make_index(self, **overrides) -> NoteIndex:
        seams = DiskSeams(
            vault_dir=self._tmp.name,
            list_notes=self.list_notes,
            read_note=vault_notes.read_note,
            split_frontmatter=split_frontmatter,
        )
        seam_keys = ("vault_dir", "list_notes", "read_note", "split_frontmatter")
        for key in seam_keys:
            if key in overrides:
                seams = replace(seams, **{key: overrides.pop(key)})
        return NoteIndex(seams, **overrides)

    def direct_view(self) -> dedup.VaultView:
        """디스크 훑기 — 엔진(mcp.rs:1718-1740)과 그림자의 옛 길이 보는 후보."""
        return dedup.VaultView(
            list_notes=self.list_notes,
            read_note=vault_notes.read_note,
            split_frontmatter=split_frontmatter,
            vault_dir=self._tmp.name,
        )

    def indexed_view(self, index: NoteIndex) -> dedup.VaultView:
        """색인 길 — 동기화 결과를 후보로 싣는다. 목록·읽기는 타면 안 된다."""

        def _forbidden(kind: str):
            def _raise(*_args):  # noqa: ANN202 — 가짜 읽기 면, 반환 없이 단언으로 죽는다
                raise AssertionError(f"색인 길은 디스크 {kind}를 하지 않는다")

            return _raise

        result = index.sync()
        assert result is not None, "prefill() 된 색인이어야 동기화 결과가 있다"
        return dedup.VaultView(
            list_notes=_forbidden("목록"),
            read_note=_forbidden("읽기"),
            split_frontmatter=split_frontmatter,
            vault_dir=self._tmp.name,
            parsed_entries=result.entries,
        )


class PrefillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = IndexFixture()
        self.addCleanup(self.fx.cleanup)

    def test_prefill_covers_every_note_with_its_parse_result(self):
        _write(self.fx.wiki, "wiki-100", "배포 절차", "배포 본문", session="sess-1")
        _write(self.fx.wiki, "wiki-200", "회의록", "회의 본문", author="owner")
        _write(self.fx.wiki, "wiki-300", "깨진 노트", "살아있는 본문")
        (self.fx.wiki / "wiki-300.md").write_text(
            "---\nnot: [valid\n---\n\n살아있는 본문\n", encoding="utf-8"
        )
        index = self.fx.make_index()
        index.prefill()
        self.assertTrue(index.ready)
        self.assertTrue(index.usable, "볼트를 읽을 수 있으면 채울 수 있다")
        self.assertIsNotNone(index.prefill_s)
        self.assertEqual(index.prefill_count, 3)
        result = index.sync()
        assert result is not None
        self.assertEqual(len(result.entries), 3)
        by_path = dict(result.entries)
        for note_id in ("wiki-100", "wiki-200", "wiki-300"):
            path = f"/vault/wiki/{note_id}.md"
            text = (self.fx.wiki / f"{note_id}.md").read_text(encoding="utf-8")
            expected = dedup.parse_existing_note(path, text, split_frontmatter)
            self.assertEqual(by_path[path], expected, f"{note_id} 파싱 결과는 디스크 훑기와 같다")

    def test_failed_prefill_falls_back_instead_of_hanging(self):
        index = self.fx.make_index(list_notes=lambda: (_ for _ in ()).throw(OSError("vault gone")))
        index.prefill()
        self.assertTrue(index.ready, "끝났음은 표시돼야 다음 쓰기가 디스크 훑기로 떨어진다")
        self.assertFalse(index.usable)
        self.assertIsNone(index.sync(), "못 채운 색인은 쓰이지 않는다 — 디스크 훑기로 귀결")

    def test_sync_before_prefill_returns_none(self):
        index = self.fx.make_index()
        _write(self.fx.wiki, "wiki-100", "배포 절차", "배포 본문")
        self.assertIsNone(index.sync(), "채우기 전 — 쓰기는 디스크 훑기로 떨어진다")


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = IndexFixture()
        self.addCleanup(self.fx.cleanup)
        _write(self.fx.wiki, "wiki-100", "배포 절차", "배포 본문", session="sess-1")
        _write(self.fx.wiki, "wiki-200", "회의록", "회의 본문", author="owner")
        self.index = self.fx.make_index()
        self.index.prefill()
        self.assertTrue(self.index.usable)

    def test_sync_rereads_changed_note(self):
        _write(self.fx.wiki, "wiki-100", "배포 절차 (고침)", "바뀐 배포 본문", session="sess-9")
        result = self.index.sync()
        assert result is not None
        changed = dict(result.entries)["/vault/wiki/wiki-100.md"]
        self.assertEqual(
            changed.title, "배포 절차 (고침)", "바뀐 노트는 옛 머리말이 아니라 새 것으로 판정한다"
        )
        self.assertEqual(changed.omb_session_id, "sess-9")
        self.assertGreater(result.parse_s, 0, "다시 파싱한 시간은 parse 칸에 잰다")
        self.assertGreater(result.read_s, 0, "다시 읽은 시간은 vault 칸에 잰다")

    def test_sync_without_changes_rereads_nothing(self):
        first = self.index.sync()
        second = self.index.sync()
        assert first is not None and second is not None
        self.assertEqual(first.entries, second.entries)
        self.assertEqual(second.parse_s, 0.0, "안 바뀐 노트는 다시 파싱하지 않는다")
        self.assertEqual(second.read_s, 0.0, "안 바뀐 노트는 다시 읽지 않는다")
        self.assertGreaterEqual(second.stat_s, 0, "목록·stat 는 매 쓰기 한다")

    def test_sync_discovers_new_note(self):
        _write(self.fx.wiki, "wiki-300", "새 노트", "새 본문", session="sess-3")
        result = self.index.sync()
        assert result is not None
        paths = dict(result.entries)
        self.assertIn("/vault/wiki/wiki-300.md", paths)
        self.assertEqual(paths["/vault/wiki/wiki-300.md"].title, "새 노트")

    def test_sync_drops_removed_note(self):
        (self.fx.wiki / "wiki-200.md").unlink()
        result = self.index.sync()
        assert result is not None
        self.assertNotIn("/vault/wiki/wiki-200.md", dict(result.entries), "사라진 노트는 색인에서 빠진다")
        self.assertEqual(len(result.entries), 1)

    def test_sync_drops_note_that_vanishes_between_stat_and_read(self):
        real_read = vault_notes.read_note

        def vanishing_read(vault_dir: str, note_id: str) -> str | None:
            if note_id == "wiki-200":
                return None
            return real_read(vault_dir, note_id)

        index = self.fx.make_index(read_note=vanishing_read)
        index.prefill()
        self.assertTrue(index.usable)
        result = index.sync()
        assert result is not None
        self.assertNotIn(
            "/vault/wiki/wiki-200.md",
            dict(result.entries),
            "stat 직후 사라진 노트는 디스크 훑기가 못 읽는 것과 같이 후보에서 빠진다",
        )

    def test_sync_result_is_sorted_and_timed(self):
        _write(self.fx.wiki, "wiki-050", "옛 번호", "본문")
        result = self.index.sync()
        assert result is not None
        paths = [path for path, _ in result.entries]
        self.assertEqual(paths, sorted(paths))
        self.assertGreaterEqual(result.stat_s, 0)
        self.assertGreaterEqual(result.read_s, 0)
        self.assertGreaterEqual(result.parse_s, 0)


class DecisionEqualityTests(unittest.TestCase):
    """색인 있음·없음이 같은 결정 — 같은 픽스처 볼트에서 두 경로의 check_duplicate 가
    같은 DuplicateMatch(경로·갈래·파싱 결과)를 낸다. 엔진은 쓰기 순간 디스크를 훑고
    색인은 그 순간의 목록·stat 로 맞추니, 지운·옮긴 노트의 결정도 갈리지 않는다."""

    def setUp(self) -> None:
        self.fx = IndexFixture()
        self.addCleanup(self.fx.cleanup)
        _write(self.fx.wiki, "wiki-100", "배포 절차", "배포는 make deploy 로 한다", session="sess-1")
        _write(
            self.fx.wiki,
            "wiki-200",
            "배포 절차",
            "배포는 kubectl 로 한다",
            session="sess-2",
            tools=("kubectl",),
            concepts=("배포",),
        )
        _write(self.fx.wiki, "wiki-300", "회의록", "회의 본문", author="owner")
        self.index = self.fx.make_index()
        self.index.prefill()

    def _assert_same_pick(self, incoming: RememberNote) -> None:
        direct = check_duplicate(note=incoming, vault=self.fx.direct_view(), nearest_document=None)
        indexed = check_duplicate(
            note=incoming, vault=self.fx.indexed_view(self.index), nearest_document=None
        )
        self.assertEqual(direct, indexed)

    def test_indexed_scan_matches_disk_scan_across_branches(self):
        # same_session 갈래 — wiki-100 이 세션 sess-1 과 같다.
        self._assert_same_pick(_note("아무 제목", "아무 본문", session="sess-1"))
        # exact_title 갈래 — 세션 없이 제목만 같을 때.
        self._assert_same_pick(_note("배포 절차", "전혀 다른 본문"))
        # probable_session 갈래 — 세션 다르고 의미 칸이 겹칠 때.
        self._assert_same_pick(
            _note(
                "배포 정리",
                "배포는 kubectl 로 한다",
                session="sess-9",
                tools=("kubectl",),
                concepts=("배포",),
            )
        )
        # 아무 갈래도 아님 — 두 경로 모두 Ok(None).
        self._assert_same_pick(_note("완전히 새로운 제목", "새 본문"))

    def test_removed_note_decision_matches_disk_scan(self):
        # 같은 제목 둘 — 지우기 전에는 양쪽 다 최신(wiki-200)을 고른다.
        self._assert_same_pick(_note("배포 절차", "다른 본문"))
        (self.fx.wiki / "wiki-200.md").unlink()
        result = self.index.sync()
        assert result is not None
        self.assertNotIn("/vault/wiki/wiki-200.md", dict(result.entries))
        # 지운 뒤 — 엔진(디스크 훑기)은 wiki-100 만 보고, 색인도 그것과 같은 결정을 낸다.
        incoming = _note("배포 절차", "다른 본문")
        direct = check_duplicate(note=incoming, vault=self.fx.direct_view(), nearest_document=None)
        indexed = check_duplicate(
            note=incoming, vault=self.fx.indexed_view(self.index), nearest_document=None
        )
        match direct:
            case Ok(found):
                self.assertIsNotNone(found)
                assert found is not None
                self.assertEqual(found.source_path, "/vault/wiki/wiki-100.md")
            case other:
                self.fail(f"expected Ok, got {other!r}")
        self.assertEqual(direct, indexed)

    def test_gate_decision_is_identical_through_the_index(self):
        # 걸러둘 + 대체 점수 판정까지 — found 자체가 같으니 dedup_gate 결과도 같다.
        # (점수 차 +8 미만이니 걸러둔다 — 대첸 노트는 별도 시험이 덮는다)
        incoming = _note("배포 절차", "다른 본문", session="sess-2")
        outcomes = []
        for view in (self.fx.direct_view(), self.fx.indexed_view(self.index)):
            match check_duplicate(note=incoming, vault=view, nearest_document=None):
                case Ok(found):
                    self.assertIsNotNone(found)
                    outcome, match_ = dedup.dedup_gate(False, incoming, found)
                    self.assertEqual(outcome, dedup.OUTCOME_SKIPPED)
                    self.assertIsNotNone(match_)
                    outcomes.append(outcome)
                case other:
                    self.fail(f"expected Ok, got {other!r}")
        self.assertEqual(len(outcomes), 2)

    def test_embedding_branch_unaffected_by_index(self):
        # 파일 갈래가 비면 임베딩 갈래로 넘어간다 — 색인이 그 순서를 바꾸지 않는다.
        seen: list[str] = []

        def nearest(text: str, exclude: str | None):
            seen.append(text)
            return Ok("/vault/wiki/wiki-300.md")

        incoming = _note("새 제목", "새 본문")
        direct = check_duplicate(note=incoming, vault=self.fx.direct_view(), nearest_document=nearest)
        indexed = check_duplicate(
            note=incoming, vault=self.fx.indexed_view(self.index), nearest_document=nearest
        )
        self.assertEqual(direct, indexed)
        self.assertEqual(len(seen), 2, "두 경로 모두 임베딩 프로브를 정확히 한 번씩만 부른다")


if __name__ == "__main__":
    unittest.main(verbosity=2)
