"""DB 에 적힌 sha 와 파일 글자 하나를 견줘 바뀜 여부를 가른다 (순수).

저장 칸은 document.sha 하나다. 옛 값은 파일 전체 sha256(legacy_sha), 새 값은 파생 칸을 뺀
해시(content_hash)다. 옛 값과 같으면 바뀌지 않은 것이라 sha 칸만 새 값으로 고친다 —
임베딩·간선은 건드리지 않는다. 둘 다와 다르면 바뀐 것이다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ohmyboring.sync.hash import content_hash, legacy_sha


@dataclass(frozen=True)
class NoRow:
    """document 행이 없다 — 새 노트."""

    new_sha: str


@dataclass(frozen=True)
class Same:
    """새 해시와 같다 — 건너뛴다."""


@dataclass(frozen=True)
class LegacySame:
    """옛 방식 sha 와 같다 — 내용은 그대로, sha 칸만 new_sha 로."""

    new_sha: str


@dataclass(frozen=True)
class Different:
    """둘 다와 다르다 — 바뀐 노트."""

    new_sha: str


ShaVerdict = NoRow | Same | LegacySame | Different


def judge_sha(db_sha: str | None, text: str) -> ShaVerdict:
    new_sha = content_hash(text)
    if db_sha is None:
        return NoRow(new_sha)
    if db_sha == new_sha:
        return Same()
    if db_sha == legacy_sha(text):
        return LegacySame(new_sha)
    return Different(new_sha)
