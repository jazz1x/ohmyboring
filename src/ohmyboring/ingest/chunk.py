"""본문 자르기 두 벌 — fixed_chunks 는 Rust DefaultChunker(문자 1500·겹침 200) 미러,
heading_chunks 는 `## ` 경계 자르기(신규 — 통계만 내고 저장·검색 교체는 E2).

둘 다 순수 함수. 문자 단위는 Rust 의 chars() 와 같다(코드 포인트 하나가 한 자).
"""

from __future__ import annotations

import statistics
from typing import Any


def fixed_chunks(body: str, size: int = 1500, overlap: int = 200) -> list[str]:
    """Rust DefaultChunker::chunk 와 같은 자르기. size 이하면 통째 한 조각."""
    if size == 0 or overlap >= size:
        return [body]
    chars = list(body)
    if len(chars) <= size:
        return [body]
    step = size - overlap
    out: list[str] = []
    start = 0
    total = len(chars)
    while start < total:
        end = min(start + size, total)
        out.append("".join(chars[start:end]))
        if end == total:
            break
        start += step
    return out


def _sections(body: str) -> list[str]:
    """`## ` 로 시작하는 줄을 경계로 절을 나눈다(연속 줄을 "\\n" 로 다시 잇는 어셈블과 그대로)."""
    lines = body.split("\n")
    sections: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("## ") and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    sections.append(current)
    return ["\n".join(section) for section in sections]


def _pack(title: str, body: str, max_chars: int) -> list[str]:
    header = f"# {title}" if title else "# (제목 없음)"
    # 제목이 한도의 절반을 먹을 만큼 길면(머리줄 `# <title>` 이 749자 — 제목 748자 — 를 넘으면)
    # 머리줄 끝을 "…" 로 잘라 본문 예산을 max_chars//2 이상으로 지킨다 — 아니면 예산이 겹침 200
    # 아래로 떨어져 고정 자르기가 절을 통째로 돌려주거나(조각이 한도를 넘음) 조각 수가 폭증한다.
    max_header = max_chars - max_chars // 2 - 1
    if len(header) > max_header:
        header = header[: max_header - 1] + "…"
    budget = max_chars - len(header) - 1  # 본문 상한 = 전체 한도에서 머리줄과 그 \n 을 뺀 것
    out: list[str] = []
    current: list[str] = []
    current_len = 0
    for section in _sections(body):
        if len(section) > budget:
            if current:
                out.append(f"{header}\n" + "\n".join(current))
                current, current_len = [], 0
            out.extend(f"{header}\n{piece}" for piece in fixed_chunks(section, size=budget))
            continue
        added = len(section) if not current else current_len + 1 + len(section)
        if current and added > budget:
            out.append(f"{header}\n" + "\n".join(current))
            current, current_len = [section], len(section)
        else:
            current.append(section)
            current_len = added
    if current:
        out.append(f"{header}\n" + "\n".join(current))
    return out


def heading_chunks(title: str, body: str, max_chars: int = 1500) -> list[str]:
    """`## ` 경계로 절을 모아 max_chars 안으로 싸고, 넘는 절만 고정 자르기.

    조각맨 앞에 `# <title>` 한 줄을 붙여 에이전트가 조각만 봐도 무슨 노트인지 알게 한다(wiki-2475).
    max_chars 는 머리줄까지 포함한 조각 전체 길이 한도다. 제목이 비정상적으로 길면 머리줄을 잘라
    본문 예산(최소 max_chars//2)을 지키고 어느 조각이든 max_chars 를 넘지 않게 한다.
    """
    return _pack(title, body, max_chars)


def chunk_stats(titles_bodies: list[tuple[str, str]], max_chars: int = 1500) -> dict[str, Any]:
    """고정 자르기와 제목 경계 자르기의 조각 수 분포를 나란히 낸다(분모=본문 있는 노트 수)."""
    fixed_counts: list[int] = []
    heading_counts: list[int] = []
    heading_chunk_max = 0
    for title, body in titles_bodies:
        pieces = fixed_chunks(body.strip())
        if not pieces or all(not piece.strip() for piece in pieces):
            continue
        heads = _pack(title, body.strip(), max_chars)
        fixed_counts.append(len(pieces))
        heading_counts.append(len(heads))
        heading_chunk_max = max(heading_chunk_max, max(len(h) for h in heads))
    n = len(fixed_counts)
    if n == 0:
        return {"notes": 0, "notes_over_1500_chars": 0}
    return {
        "notes": n,
        "notes_over_1500_chars": sum(1 for c in fixed_counts if c > 1),
        "chunks_fixed_total": sum(fixed_counts),
        "chunks_heading_total": sum(heading_counts),
        "per_note_chunks_fixed": {
            "min": min(fixed_counts),
            "median": statistics.median(fixed_counts),
            "max": max(fixed_counts),
        },
        "per_note_chunks_heading": {
            "min": min(heading_counts),
            "median": statistics.median(heading_counts),
            "max": max(heading_counts),
        },
        "heading_chunk_chars_max": heading_chunk_max,
    }
