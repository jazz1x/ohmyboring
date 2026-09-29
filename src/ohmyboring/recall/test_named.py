#!/usr/bin/env python3
"""번호로 부른 노트 한 벌 — wiki-NNNN 찾기 · 노트 블록 · 볼트 읽기 (WI-1).

Run: python3 ohmyboring/recall/test_named.py   (no pytest dependency, stdlib only)

왜 있는가. 엔진 recall("wiki-2226") 이 wiki-2226 이 아니라 그 번호를 언급한 wiki-2227 을 준
버그(2026-09-28 — 노트 본문에 자기 번호가 없어 검색이 못 찾음)의 대응: 번호 찾기와 노트 블록은
ohmyboring.recall.named 한 벌이고, 문(MCP 회상 앞에 붙임)과 hermes 플러그인(슬랙 턴 문맥)이
같이 쓴다. 이 시험은 그 한 벌을 못 박는다.

Mutation targets: named_ids 가 [] 를 돌려주면 순서·중복 시험이 빨개진다; note_block 의 없음
줄을 삼키면 없음 시험이 빨개진다; read_note 가 FileNotFoundError 말고 PermissionError 도
삼키면 OSError 시험이 빨개진다.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "agents" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import vault_note  # noqa: E402 — the one frontmatter splitter callers hand in as the port

from ohmyboring.adapters import vault  # noqa: E402
from ohmyboring.recall import named  # noqa: E402

#: 실제 볼트 노트 모양 — 따옴표 타이틀, 스칼라 날짜, 두 문단.
_NOTE = """---
id: wiki-2121
title: '카드·주간 브리핑을 hermes 가 실행'
kind: note
origin: personal
date: 2026-09-28
---

첫째 문단 — 카드와 주간 브리핑을 hermes 가 실행하게 했다.

둘째 문단 — 둘째 주자가 두 번 올라가지 못하게 카드는 오늘 자기 사건을 읽는다.
"""


_split = vault_note.split_frontmatter


def test_named_ids_in_order_without_duplicates():
    assert named.named_ids("wiki-2226 봐, 그리고 wiki-2227 도") == ["wiki-2226", "wiki-2227"]
    assert named.named_ids("wiki-2226 과 wiki-2226") == ["wiki-2226"]
    # 나온 순서가 유지된다 — 번호대로 정렬이 아니다
    assert named.named_ids("wiki-9000 그리고 wiki-1000") == ["wiki-9000", "wiki-1000"]
    # 소문자만 — 대문자 변형은 다른 이름이 아니라 없는 이름
    assert named.named_ids("WIKI-2226") == []
    # 번호가 없는 말은 빈 목록
    assert named.named_ids("그냥 검색해줘") == []


def test_note_block_renders_title_date_and_body_head():
    block = named.note_block("wiki-2121", _NOTE, _split)
    assert block.startswith("- 노트 wiki-2121 — 카드·주간 브리핑을 hermes 가 실행\n")
    assert "날짜: 2026-09-28" in block
    assert "첫째 문단 — 카드와 주간 브리핑을 hermes 가 실행하게 했다." in block


def test_note_block_says_missing_out_loud():
    assert named.note_block("wiki-9999", None, _split) == "- wiki-9999 은 볼트에 없음"


def test_note_block_tolerates_no_frontmatter():
    block = named.note_block("wiki-0001", "머리말 없는 본문", _split)
    assert block.startswith("- 노트 wiki-0001 — (제목 없음)\n")
    assert "머리말 없는 본문" in block


def test_read_note_returns_text_and_none_only_for_missing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "wiki").mkdir()
        (root / "wiki" / "wiki-2121.md").write_text(_NOTE, encoding="utf-8")
        assert vault.read_note(str(root), "wiki-2121") == _NOTE
        assert vault.read_note(str(root), "wiki-9999") is None
        # FileNotFoundError 만 None — 그 밖의 OSError 는 올라간다(호출자 정책: 로그 한 줄).
        os.chmod(root / "wiki", 0o000)
        try:
            try:
                vault.read_note(str(root), "wiki-2121")
            except PermissionError:
                pass
            else:
                raise AssertionError("a PermissionError vault must raise, not come back None")
        finally:
            os.chmod(root / "wiki", 0o755)


if __name__ == "__main__":
    test_named_ids_in_order_without_duplicates()
    test_note_block_renders_title_date_and_body_head()
    test_note_block_says_missing_out_loud()
    test_note_block_tolerates_no_frontmatter()
    test_read_note_returns_text_and_none_only_for_missing()
    print("ok - recall named: 번호 찾기(순서·중복)·노트 블록(있음·없음)·볼트 읽기(FileNotFound 만 None)")
