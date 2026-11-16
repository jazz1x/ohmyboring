"""쓰기 때 충돌 후보 — decision·blocked claim 의 value 와 식별자를 공유하는, 다른 칸의 열린 decision.

판단(추출·정규식·출력 줄)은 순수 함수, 읽기는 SQL 한 번. 저장은 막지 않는다 — 후보를 돌려줄 뿐,
부딪히는지는 읽는 에이전트가 정한다(C-CONFLICT1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ohmyboring.registers.pg import NOT_USER_MEMORY_RE
from ohmyboring.remember.dedup import wiki_number
from ohmyboring.remember.graph import canon, canon_sql

#: 이 종류의 claim 이 들어올 때만 후보를 찾는다.
TRIGGER_KINDS = frozenset({"decision", "blocked"})
MAX_CANDIDATES = 5
VALUE_PREVIEW_CHARS = 80
#: 같은 project 열린 decision 의 이 비율을 넘는 식별자는 흔한 낱말이라 일치에 안 쓴다.
COMMON_SHARE = 0.05

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_CAMEL_RE = re.compile(r"[a-z][A-Za-z0-9]*[A-Z]")
_PASCAL_RE = re.compile(r"[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+")


def _is_identifier(token: str) -> bool:
    if "." in token:
        return all(len(part) >= 2 for part in token.split("."))
    if len(token) < 3:
        return False
    if "_" in token.strip("_"):
        return True
    return bool(_CAMEL_RE.match(token) or _PASCAL_RE.fullmatch(token))


def extract_identifiers(value: str) -> tuple[str, ...]:
    """camelCase·snake_case·SCREAMING_SNAKE·점 경로·PascalCase 타입명 — 등장 순서, 중복 없이. 보통 낱말은 아니다."""
    seen: dict[str, None] = {}
    for token in _TOKEN_RE.findall(value):
        if _is_identifier(token):
            seen.setdefault(token)
    return tuple(seen)


def identifier_pattern(identifier: str) -> str:
    """value 안에서 이 식별자가 통째로 나오는 자리 — 앞뒤가 식별자 글자가 아니어야 한다(한글은 경계)."""
    return rf"(^|[^A-Za-z0-9_.]){re.escape(identifier)}($|[^A-Za-z0-9_])"


@dataclass(frozen=True)
class ConflictCandidate:
    source_path: str
    subject: str
    predicate: str
    value: str
    overlap: tuple[str, ...]


def candidate_line(candidate: ConflictCandidate) -> str | None:
    """MCP message 한 줄 — 번호를 못 읽는 경로(wiki 노트가 아님)는 줄을 만들지 않는다."""
    if wiki_number(candidate.source_path) is None:
        return None
    stem = candidate.source_path.rsplit("/", 1)[-1].removesuffix(".md")
    first = candidate.value.strip()[:VALUE_PREVIEW_CHARS]
    return (
        f"후보: {stem} {candidate.subject}/{candidate.predicate}"
        f" — 겹친 낱말 {', '.join(candidate.overlap)} — {first}"
    )


def message_suffix(candidates: tuple[ConflictCandidate, ...]) -> str:
    lines = [line for line in map(candidate_line, candidates) if line]
    return "".join(f"\n{line}" for line in lines)


_SEARCH_SQL = (
    "WITH ids AS ("
    " SELECT t.id, t.pat FROM unnest(%(ids)s::text[], %(pats)s::text[]) AS t(id, pat)"
    "), open_dec AS ("
    " SELECT c.source_path, c.subject, c.predicate, c.value, c.valid_from"
    " FROM claim c JOIN document d ON d.source_path = c.source_path"
    " WHERE c.superseded_at IS NULL AND c.kind = 'decision'"
    " AND d.project = %(project)s"
    " AND NOT (d.origin = ANY(%(exclude_origins)s))"
    " AND d.source_path !~ %(not_user_memory)s"
    " AND c.source_path <> %(own_path)s"
    "), usable AS ("
    " SELECT i.id, i.pat FROM ids i"
    " CROSS JOIN LATERAL (SELECT count(*) AS hits FROM open_dec o WHERE o.value ~ i.pat) h"
    " WHERE h.hits <= %(share)s * (SELECT count(*) FROM open_dec)"
    ") SELECT o.source_path, o.subject, o.predicate, o.value, array_agg(u.id ORDER BY u.id) AS overlap"
    " FROM open_dec o JOIN usable u ON o.value ~ u.pat"
    " WHERE NOT EXISTS (SELECT 1 FROM unnest(%(slot_subjects)s::text[], %(slot_predicates)s::text[])"
    " AS s(subject, predicate) WHERE s.subject = "
    + canon_sql("o.subject")
    + " AND s.predicate = "
    + canon_sql("o.predicate")
    + ")"
    " GROUP BY o.source_path, o.subject, o.predicate, o.value, o.valid_from"
    " ORDER BY count(*) DESC, o.valid_from DESC, o.source_path"
    " LIMIT %(limit)s"
)


def search_conflicts(
    cur: Any,
    project: str,
    claims: list[tuple[str, str, str]],
    own_path: str,
    exclude_origins: tuple[str, ...] = (),
) -> tuple[ConflictCandidate, ...]:
    """claims 는 (subject, predicate, value) 들 — decision·blocked 만 넘긴다. SQL 한 번, 읽기만."""
    ids: dict[str, None] = {}
    for _subject, _predicate, value in claims:
        for identifier in extract_identifiers(value):
            ids.setdefault(identifier)
    if not ids or not project:
        return ()
    names = list(ids)
    cur.execute(
        _SEARCH_SQL,
        {
            "ids": names,
            "pats": [identifier_pattern(name) for name in names],
            "project": project,
            "exclude_origins": list(exclude_origins),
            "not_user_memory": NOT_USER_MEMORY_RE,
            "share": COMMON_SHARE,
            "own_path": own_path,
            "slot_subjects": [canon(subject) for subject, _p, _v in claims],
            "slot_predicates": [canon(predicate) for _s, predicate, _v in claims],
            "limit": MAX_CANDIDATES,
        },
    )
    return tuple(ConflictCandidate(row[0], row[1], row[2], row[3], tuple(row[4])) for row in cur.fetchall())
