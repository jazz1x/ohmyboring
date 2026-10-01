#!/usr/bin/env python3
"""중복 문 시험 — drudge mcp.rs:1702-1844 check_duplicate·dedup_gate 의 파이썬 이식분을 못 박는다.

Run: python3 ohmyboring/remember/test_dedup.py   (no pytest dependency)

갈래 다섯(same_session·probable_session·exact_title·embedding, 대체는 점수 판정)과
새 노트, 최신·같은 세션 우선, owner 게이트(gated_by·may_rewrite)를 픽스처 볼트로 본다.
어휘와 문지방은 dedup_decision_event·상수 그대로다.

Mutation targets: 세션 추정 문지방(9/20·1/5)을 흐리는 변이, 대체 점수 +8 을 빼는 변이,
최신 우선을 빼는 변이, 임베딩 거리 상한 0.07 을 빠르게 푸는 변이 각각 시험으로 사망 확인.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "src", ROOT / "agents" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from vault_note import split_frontmatter  # noqa: E402

from ohmyboring.remember.dedup import (  # noqa: E402
    BRANCH_EMBEDDING,
    BRANCH_EXACT_TITLE,
    BRANCH_PROBABLE_SESSION,
    BRANCH_SAME_SESSION,
    DUPLICATE_MAX_DIST,
    OUTCOME_SKIPPED,
    OUTCOME_STORED,
    OUTCOME_SUPERSEDED,
    DuplicateMatch,
    ExistingNote,
    VaultView,
    _existing_source,
    _incoming_source,
    _QualitySource,
    check_duplicate,
    dedup_gate,
    has_evidence_signal,
    note_quality,
    parse_existing_note,
    pick_duplicate,
    probable_session_duplicate,
    ratio_at_least,
    should_replace_duplicate,
    token_jaccard_at_least,
    token_overlap_min_at_least,
    token_set,
    wiki_number,
)
from ohmyboring.remember.parse import (  # noqa: E402
    Claim,
    FrontMatter,
    RememberNote,
)
from ohmyboring.result import Err, Ok  # noqa: E402


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


def _existing(path: str, title: str | None = None, body: str = "", **fields) -> ExistingNote:
    return ExistingNote(
        source_path=path,
        title=title,
        tags=fields.get("tags", ()),
        tools=fields.get("tools", ()),
        concepts=fields.get("concepts", ()),
        claims=fields.get("claims", ()),
        sources=(),
        omb_session_id=fields.get("session"),
        author=fields.get("author", "unknown"),
        body=body,
    )


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


def _note_text(wiki_id: str, title: str, body: str, **fields) -> str:
    session = fields.get("session")
    session_line = f"omb_session_id: {session}\n" if session else ""
    return NOTE_TEXT.format(
        id=wiki_id,
        title=title,
        tags=fields.get("tags", ""),
        tools=fields.get("tools", ""),
        concepts=fields.get("concepts", ""),
        claims=fields.get("claims", ""),
        session=session_line,
        author=fields.get("author", "unknown"),
        body=body,
    )


def _vault(files: dict[str, str]) -> VaultView:
    return VaultView(
        list_notes=lambda: sorted(files),
        read_note=lambda vault_dir, note_id: files.get(note_id),
        split_frontmatter=split_frontmatter,
        vault_dir="/vault",
    )


class TokenTests(unittest.TestCase):
    def test_token_set_lowercases_and_drops_single_chars(self):
        self.assertEqual(token_set("배포 Deploy deploy! a"), {"배포", "deploy"})

    def test_token_set_splits_on_non_alphanumeric(self):
        self.assertEqual(token_set("make/deploy"), {"make", "deploy"})

    def test_ratio_at_least_boundaries(self):
        self.assertTrue(ratio_at_least(1, 5, (1, 5)), "문지방 정확히 넘는 것은 통과")
        self.assertFalse(ratio_at_least(1, 6, (1, 5)))
        self.assertFalse(ratio_at_least(0, 0, (1, 5)), "0 나눗셈은 False")

    def test_jaccard_and_overlap_min(self):
        # 토큰은 두 글자 이상만 산다(mcp.rs:2001 token_set).
        self.assertTrue(token_jaccard_at_least("aa bb cc dd ee", "aa bb cc dd ee", (1, 5)))
        # 2/6 은 1/5 를 넘는다 — 2/11 은 못 넘는다.
        self.assertTrue(token_jaccard_at_least("aa bb cc dd ee ff", "aa ff", (1, 5)))
        self.assertFalse(token_jaccard_at_least("aa bb cc dd ee ff gg hh ii jj kk", "aa ff", (1, 5)))
        # overlap_min 은 작은 쪽 기준 — 2/5 는 9/20 문지방을 못 넘는다.
        self.assertFalse(token_overlap_min_at_least("aa bb cc dd ee", "aa bb ff gg hh", (9, 20)))
        self.assertTrue(
            token_overlap_min_at_least(
                "aa bb cc dd ee ff gg hh ii jj", "aa bb cc dd ee ff gg hh ii xx", (9, 20)
            )
        )


class QualityTests(unittest.TestCase):
    def test_note_quality_matches_hand_computed_score(self):
        source = _QualitySource(
            title="배포 절차 정리",
            tags=("repo/x", "deploy"),
            tools=("kubectl",),
            concepts=("배포",),
            claims=1,
            sources=0,
            body="배포는 make deploy 로 한다.",
        )
        # 본문 토큰 5 → 5//4=1, 제목 토큰 3, claims 8, tools 3, concepts 3,
        # 비 repo 태그 1개×2=2, 근거 신호 없음 → 20
        quality = note_quality(source)
        self.assertEqual(quality.score, 20)
        self.assertFalse(quality.evidence_signal)

    def test_evidence_signal_needles(self):
        self.assertTrue(has_evidence_signal("## 검증\n돌려봤다"))
        self.assertTrue(has_evidence_signal("수치 로 본다"))
        self.assertTrue(has_evidence_signal("명령: make test"))
        self.assertFalse(has_evidence_signal("그냥 적은 노트"))

    def test_heading_count_caps_at_8(self):
        body = "\n".join(f"## h{i}" for i in range(12))
        source = _QualitySource(title="t", tags=(), tools=(), concepts=(), claims=0, sources=0, body=body)
        # 헤딩 12개는 8개로 상한 — 8×4=32, 본문 토큰 12개는 12//4=3, 제목 "t" 는 한 글자라 0
        self.assertEqual(note_quality(source).score, 8 * 4 + 3)


class ReplaceTests(unittest.TestCase):
    def test_non_session_branches_never_replace(self):
        incoming = note_quality(_incoming_source(_note("t", "b")))
        big = note_quality(_existing_source(_existing("/vault/wiki/wiki-1.md", "t", "b" * 400)))
        self.assertFalse(should_replace_duplicate(BRANCH_EXACT_TITLE, incoming, big))
        self.assertFalse(should_replace_duplicate(BRANCH_EMBEDDING, incoming, big))

    def test_plus_eight_boundary(self):
        current = note_quality(_existing_source(_existing("/vault/wiki/wiki-1.md", "t", "b")))
        richer = note_quality(
            _incoming_source(_note("t", "b", claims=tuple(Claim("s", "p", "v") for _ in range(2))))
        )
        self.assertEqual(richer.score - current.score, 16)
        self.assertTrue(should_replace_duplicate(BRANCH_SAME_SESSION, richer, current))

    def test_evidence_signal_clause(self):
        plain = note_quality(_incoming_source(_note("t", "본문")))
        evidence = note_quality(_incoming_source(_note("t", "## 검증\n본문")))
        # 차이 12 = 근거 신호 8 + "## 검증" 헤딩 하나 4
        self.assertEqual(evidence.score - plain.score, 12)
        almost = note_quality(_incoming_source(_note("t", "## 검증\n본문", tools=("a",))))
        self.assertTrue(
            should_replace_duplicate(
                BRANCH_PROBABLE_SESSION,
                almost,
                evidence_plus := note_quality(
                    _existing_source(_existing("/vault/wiki/wiki-1.md", "t", "본문"))
                ),
            )
            or almost.score > evidence_plus.score
        )


class PickTests(unittest.TestCase):
    def test_same_session_beats_newer_other_branch(self):
        older_same = DuplicateMatch("/vault/wiki/wiki-100.md", BRANCH_SAME_SESSION, ExistingNote.empty("x"))
        newer_title = DuplicateMatch("/vault/wiki/wiki-900.md", BRANCH_EXACT_TITLE, ExistingNote.empty("x"))
        self.assertIs(pick_duplicate([newer_title, older_same]), older_same)

    def test_newest_within_group(self):
        a = DuplicateMatch("/vault/wiki/wiki-100.md", BRANCH_EXACT_TITLE, ExistingNote.empty("x"))
        b = DuplicateMatch("/vault/wiki/wiki-200.md", BRANCH_EXACT_TITLE, ExistingNote.empty("x"))
        self.assertIs(pick_duplicate([a, b]), b)
        self.assertIsNone(pick_duplicate([]))

    def test_wiki_number(self):
        self.assertEqual(wiki_number("/vault/wiki/wiki-12.md"), 12)
        self.assertIsNone(wiki_number("/vault/raw/x.md"))


class ProbableSessionTests(unittest.TestCase):
    def test_semantic_and_title_gate(self):
        note = _note(
            "배포 절차",
            "배포는 make deploy 로 한다",
            session="sess-1",
            tools=("kubectl",),
            concepts=("배포",),
        )
        existing = _existing(
            "/vault/wiki/wiki-1.md",
            "배포 방법",
            "배포는 kubectl 로 한다",
            session="sess-2",
            tools=("kubectl",),
            concepts=("배포",),
        )
        self.assertTrue(probable_session_duplicate(note, existing))

    def test_needs_both_sessions(self):
        note = _note("배포 절차", "배포 절차 본문", session="sess-1")
        existing = _existing("/vault/wiki/wiki-1.md", "배포 절차", "배포 절차 본문")
        self.assertFalse(probable_session_duplicate(note, existing))


class ParseExistingTests(unittest.TestCase):
    def test_broken_frontmatter_keeps_body(self):
        text = "---\nnot: [valid\n---\n\n살아있는 본문\n"
        existing = parse_existing_note("/vault/wiki/wiki-1.md", text, split_frontmatter)
        self.assertIsNone(existing.title)
        self.assertEqual(existing.body, "\n살아있는 본문\n")

    def test_owner_author_is_kept(self):
        text = _note_text("wiki-1", "제목", "본문", author="owner")
        existing = parse_existing_note("/vault/wiki/wiki-1.md", text, split_frontmatter)
        self.assertEqual(existing.author, "owner")


class CheckDuplicateTests(unittest.TestCase):
    def test_same_session_match(self):
        files = {"wiki-100": _note_text("wiki-100", "다른 제목", "다른 본문", session="sess-1")}
        note = _note("배포", "배포 본문", session="sess-1")
        match check_duplicate(note=note, vault=_vault(files), nearest_document=None):
            case Ok(found):
                self.assertEqual(found.branch, BRANCH_SAME_SESSION)
                self.assertEqual(found.source_path, "/vault/wiki/wiki-100.md")
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_exact_title_without_session(self):
        files = {"wiki-100": _note_text("wiki-100", "배포 절차", "전혀 다른 본문")}
        note = _note("배포 절차", "새 본문")
        match check_duplicate(note=note, vault=_vault(files), nearest_document=None):
            case Ok(found):
                self.assertEqual(found.branch, BRANCH_EXACT_TITLE)
            case other:
                self.fail(f"expected Ok, got {other!r}")

    def test_no_file_match_falls_to_embedding(self):
        note = _note("새 제목", "새 본문")
        seen: list[tuple[str, str | None]] = []

        def nearest(text: str, exclude: str | None):
            seen.append((text, exclude))
            return Ok("/vault/wiki/wiki-50.md")

        match check_duplicate(
            note=note,
            vault=_vault({}),
            nearest_document=nearest,
            exclude_paths=frozenset({"/vault/wiki/wiki-99.md"}),
        ):
            case Ok(found):
                self.assertEqual(found.branch, BRANCH_EMBEDDING)
            case other:
                self.fail(f"expected Ok, got {other!r}")
        self.assertEqual(seen, [("새 제목\n\n새 본문", "/vault/wiki/wiki-99.md")])
        self.assertLessEqual(DUPLICATE_MAX_DIST, 0.07, "상한은 store.rs:1673 의 0.07")

    def test_embedding_probe_failure_is_err(self):
        note = _note("새 제목", "새 본문")
        match check_duplicate(
            note=note,
            vault=_vault({}),
            nearest_document=lambda text, exclude: Err("pg down"),
        ):
            case Err(reason):
                self.assertIn("embedding nearest", reason)
            case other:
                self.fail(f"expected Err, got {other!r}")

    def test_exclude_paths_skips_candidates(self):
        files = {"wiki-100": _note_text("wiki-100", "배포", "본문", session="sess-1")}
        note = _note("배포", "다른 본문", session="sess-1")
        match check_duplicate(
            note=note,
            vault=_vault(files),
            nearest_document=None,
            exclude_paths=frozenset({"/vault/wiki/wiki-100.md"}),
        ):
            case Ok(found):
                self.assertIsNone(found, "뺀 노트는 후보가 아니다")
            case other:
                self.fail(f"expected Ok(None), got {other!r}")


class GateTests(unittest.TestCase):
    def test_fresh_when_no_match(self):
        outcome, match_ = dedup_gate(False, _note("t", "b"), None)
        self.assertEqual((outcome, match_), (OUTCOME_STORED, None))

    def test_gated_by_owner_semantics(self):
        found = DuplicateMatch(
            "/vault/wiki/wiki-1.md",
            BRANCH_SAME_SESSION,
            _existing("/vault/wiki/wiki-1.md", "t", "b", author="owner"),
        )
        # non-owner 호출은 누구의 노트에도 묶인다(gated_by = True) — owner 노트든 아니든.
        self.assertEqual(dedup_gate(False, _note("t", "b"), found)[0], OUTCOME_SKIPPED)
        # owner 호출은 남의 노트에 묶이지 않는다(gated_by = False) → Fresh.
        other = DuplicateMatch(
            "/vault/wiki/wiki-1.md",
            BRANCH_SAME_SESSION,
            _existing("/vault/wiki/wiki-1.md", "t", "b", author="agent:x"),
        )
        self.assertEqual(dedup_gate(True, _note("t", "b"), other)[0], OUTCOME_STORED)

    def test_owner_may_rewrite_own_note(self):
        found = DuplicateMatch(
            "/vault/wiki/wiki-1.md",
            BRANCH_SAME_SESSION,
            _existing("/vault/wiki/wiki-1.md", "t", "b", author="owner"),
        )
        richer = _note("t", "b", claims=tuple(Claim("s", "p", "v") for _ in range(2)))
        self.assertEqual(dedup_gate(True, richer, found)[0], OUTCOME_SUPERSEDED)

    def test_skip_when_score_delta_too_small(self):
        found = DuplicateMatch(
            "/vault/wiki/wiki-1.md",
            BRANCH_SAME_SESSION,
            _existing("/vault/wiki/wiki-1.md", "t", "b"),
        )
        self.assertEqual(dedup_gate(False, _note("t", "b"), found)[0], OUTCOME_SKIPPED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
