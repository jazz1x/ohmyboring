"""볼트 어댑터 — 노트 텍스트 읽기만 하는 나가는 쪽 한 곳."""

from __future__ import annotations

import os
import re

_NOTE_ID = re.compile(r"wiki-(\d+)$")


def _spellings(note_id: str) -> list[str]:
    """한 노트는 번호다 — 이름의 자릿수는 표기. 받은 이름 먼저, 같은 번호의 4자리·11자리 표기가 뒤."""
    match = _NOTE_ID.match(note_id)
    if match is None:
        return [note_id]
    number = int(match.group(1))
    out = [note_id]
    for width in (4, 11):
        candidate = f"wiki-{number:0{width}d}"
        if candidate not in out:
            out.append(candidate)
    return out


def read_note(vault_dir: str, note_id: str) -> str | None:
    """`<vault_dir>/wiki/<note_id>.md` 의 텍스트, 없으면 None. 번호가 같으면 자릿수가 달라도 같은 노트.

    FileNotFoundError 만 None — 그 밖의 OSError(읽기 권한, 볼트 마운트 불량)는 그대로 올려
    호출자가 결정하게 둔다. 문과 hermes 플러그인이 같은 정책으로 쓴다: 볼트를 못 읽으면
    번호 찾기를 죽이지 말고 그 사실을 로그 한 줄에 남긴 뒤 엔진 답을 그대로 넘긴다.
    """
    for name in _spellings(note_id):
        path = os.path.join(vault_dir, "wiki", f"{name}.md")
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        except FileNotFoundError:
            continue
    return None
