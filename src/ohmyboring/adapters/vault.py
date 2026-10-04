"""볼트 어댑터 — 노트 텍스트 읽기만 하는 나가는 쪽 한 곳."""

from __future__ import annotations

import os


def read_note(vault_dir: str, note_id: str) -> str | None:
    """`<vault_dir>/wiki/<note_id>.md` 의 텍스트, 없으면 None.

    FileNotFoundError 만 None — 그 밖의 OSError(읽기 권한, 볼트 마운트 불량)는 그대로 올려
    호출자가 결정하게 둔다. 문과 hermes 플러그인이 같은 정책으로 쓴다: 볼트를 못 읽으면
    번호 찾기를 죽이지 말고 그 사실을 로그 한 줄에 남긴 뒤 엔진 답을 그대로 넘긴다.
    """
    path = os.path.join(vault_dir, "wiki", f"{note_id}.md")
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None
