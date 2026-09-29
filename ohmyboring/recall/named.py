"""번호로 부른 노트 — wiki-NNNN 찾기와 문 앞에 붙는 노트 블록, 문·hermes 가 같이 쓰는 한 벌.

Measured 2026-09-28: MCP recall("wiki-2226") 이 wiki-2226 이 아니라 그 번호를 언급한
wiki-2227 을 줬다 — 노트 본문에 자기 번호가 없어서 엔진 검색이 번호를 못 찾는다. 소유자가
번호로 부른 노트는 검색이 아니라 이름 풀이로 답해야 한다. 이 모듈이 그 한 벌: 번호
목록 뽑기(named_ids)와 노트 블록(note_block). 블록 모양은 hermes boring-memory 플러그인이
슬랙 턴에 싣던 것과 같다 — 「- 노트 wiki-NNNN — <제목>」, 날짜, 본문 앞 800자, 없는
노트는 「- wiki-NNNN 은 볼트에 없음」.

프레임워크·HTTP import 0 (re·collections.abc 뿐) — 볼트 한 장 읽고 줄 몇 개 만드는 일에
의존이 없어야 훅·플러그인·문 어디서든 그대로 import 된다.
"""

from __future__ import annotations

import re
from collections.abc import Callable

#: 소유자가 카드와 슬랙에서 노트를 부르는 모양 — 소문자 네 자리, 경계 없이(「wiki-22265」도
#: 앞 네 자리가 그 노트의 이름이다).
_WIKI_TOKEN = re.compile(r"wiki-\d{4}")

#: 블록에 실리는 본문 상한 — 슬랙 한 턴과 MCP 회상문 한 장 사이에서 번호 노트 몇 개가
#: 함께 실릴 만큼만.
_BODY_CHARS = 800

#: 머리말에서 렌더에 필요한 스칼라 하나만 긁는다 — vault_note 가 쪼개기까지 하고
#: 매핑(YAML 파싱)은 일부러 안 한다.
_FRONTMATTER_FIELD = re.compile(r"^([a-z_]+):[ \t]*(.*)$")

#: 호출자가 넘기는 머리말 쪼개기 — vault_note.split_frontmatter 이다. 이 모듈은 쪼개기를
#: 만들지 않고 받아 쓴다(저장소 이전 조각에서 정해진 포트).
SplitFrontmatter = Callable[[str], tuple[str, str] | None]


def named_ids(text: str) -> list[str]:
    """text 안의 wiki-NNNN 목록 — 나온 순서대로, 중복 없이, 소문자 그대로."""
    return list(dict.fromkeys(_WIKI_TOKEN.findall(text)))


def _frontmatter_value(frontmatter: str, name: str) -> str:
    """머리말에서 스칼라 하나 — 따옴표로 싼 값은 따옴표를 벗긴다."""
    for line in frontmatter.splitlines():
        match = _FRONTMATTER_FIELD.match(line.strip())
        if not match or match.group(1) != name:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            return value[1:-1]
        return value
    return ""


def note_block(note_id: str, text: str | None, split: SplitFrontmatter) -> str:
    """하나의 번호 노트 블록 — 「- 노트 <id> — <제목>」, 날짜, 본문 앞 800자.

    text 가 None(볼트에 파일이 없음)이면 「- <id> 은 볼트에 없음」 — 조용히 빠지지 않는다:
    「그 노트는 모른다」가 이 기능이 존재하는 이유니까. text 가 있으면 split(호출자의 머리말
    쪼개기)로 앞뒤를 갈라 제목·날짜를 머리말에서, 본문 앞머리를 몸통에서 꺼낸다.
    """
    if text is None:
        return f"- {note_id} 은 볼트에 없음"
    parts = split(text)
    frontmatter, body = parts if parts is not None else ("", text)
    title = _frontmatter_value(frontmatter, "title") or "(제목 없음)"
    date = _frontmatter_value(frontmatter, "date")
    lines = [f"- 노트 {note_id} — {title}"]
    if date:
        lines.append(f"  날짜: {date}")
    excerpt = " ".join(body.split())[:_BODY_CHARS]
    if excerpt:
        lines.append(f"  {excerpt}")
    return "\n".join(lines)
