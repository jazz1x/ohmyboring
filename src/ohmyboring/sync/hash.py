"""노트 바뀜 판정 해시 — 파생 머리말 칸(relates_to)을 뺀 내용의 sha256 (판정 결정 1, wiki-4029).

relates_to 는 project_links 가 4시간마다 볼트에 다시 쓰는 칸이라 파일 전체 sha 에 넣으면
노트가 안 바뀌어도 해시가 바뀌어 다시 임베딩된다. 저장 칸은 기존 document.sha 다(스키마 불변).
입력은 NUL 을 이미 뺀 파일 전체 글자다(writer._ingest·sync 가 같은 경계에서 뺀다).
"""

from __future__ import annotations

import hashlib

_BOM = "﻿"
_FENCE = "---"
#: 머리말 최상위 칸 이름 — 판정에서 빼는 파생 칸. 목록으로 둔 채 relates_to 하나다.
DERIVED_KEYS = ("relates_to:",)


def legacy_sha(text: str) -> str:
    """엔진이 쓰던 해시 — 파일 전체 글자의 sha256 (ingest.rs sha256)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strip_derived(text: str) -> str:
    """머리말에서 파생 칸(키 줄과 바로 이어지는 `- ` 목록 줄)을 뺀 글자 — vault.rs set_relates_to 가
    덮어쓰는 줄 모양과 같은 규칙. 머리말이 없으면 그대로."""
    lines = text.split("\n")
    if lines[0].removeprefix(_BOM).strip() != _FENCE:
        return text
    kept = [lines[0]]
    skipping_list = False
    in_front = True
    for line in lines[1:]:
        if not in_front:
            kept.append(line)
            continue
        if line.strip() == _FENCE:
            in_front = False
            kept.append(line)
            continue
        if skipping_list and line.lstrip().startswith("-"):
            continue
        skipping_list = line.startswith(DERIVED_KEYS)
        if not skipping_list:
            kept.append(line)
    return "\n".join(kept)


def content_hash(text: str) -> str:
    """바뀜 판정 해시 — 파생 칸을 뺀 글자의 sha256."""
    return legacy_sha(strip_derived(text))
