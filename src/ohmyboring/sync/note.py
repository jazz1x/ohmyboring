"""볼트 노트 한 장 → (FrontMatter, 본문) — drudge frontmatter.rs:224 parse 의 파이썬 짝.

경계에서 한 번 읽는다: 머리말 칸 모양이 틀리면 Err(사유) — 엔진이 sync 에서 「깨진 노트」로
세던 것과 같은 자리다. 머리말 쪼개기는 주입(정본 agents/shared/vault_note.split_frontmatter,
ingest/note.py 와 같은 규약). origin·kind·project 가 비면 경로에서 채운다(enrich).
머리말에 없는 칸(id·relates_to 등)은 엔진 serde 처럼 무시한다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import yaml

from ohmyboring.remember.parse import Claim, FrontMatter, _StrOnlyLoader, parse_author
from ohmyboring.result import Either, Err, Ok

SplitFrontmatter = Callable[[str], tuple[str, str] | None]
ClassifyOrigin = Callable[[str], str]

_BOM = "﻿"


def _derive_kind(path: str) -> str:
    if "/notes/" in path:
        return "note"
    if "/memory" in path:
        return "memory"
    return "doc"


def derive_project(path: str) -> str:
    """frontmatter.rs:212 — 경로의 `projects/<이름>` 만 선언으로 읽고 없으면 빈 값."""
    parts = [p for p in path.split("/") if p]
    if "projects" in parts:
        at = parts.index("projects")
        if at + 1 < len(parts):
            return parts[at + 1]
    return ""


def _text(raw: dict, key: str) -> Either[str, str]:
    value = raw.get(key)
    if value is None:
        return Ok("")
    if not isinstance(value, str):
        return Err(f"yaml: {key} is not a string")
    return Ok(value)


def _texts(raw: dict, key: str) -> Either[tuple[str, ...], str]:
    value = raw.get(key)
    if value is None:
        return Ok(())
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return Err(f"yaml: {key} is not a list of strings")
    return Ok(tuple(value))


def _claim(item: Any) -> Either[Claim, str]:
    if not isinstance(item, dict):
        return Err("yaml: claims item is not a mapping")
    fields: dict[str, str] = {}
    for key in ("subject", "predicate", "value"):
        if not isinstance(item.get(key), str):
            return Err(f"yaml: claims[].{key} is missing or not a string")
        fields[key] = item[key]
    for key in ("kind", "confidence"):
        value = item.get(key)
        if value is not None and not isinstance(value, str):
            return Err(f"yaml: claims[].{key} is not a string")
        fields[key] = value or ""
    said_by: str | None = None
    if item.get("said_by") is not None:
        if not isinstance(item["said_by"], str) or item["said_by"].strip() != "owner":
            return Err(f"yaml: claims[].said_by must be owner, got {item['said_by']!r}")
        said_by = "owner"
    return Ok(
        Claim(
            fields["subject"],
            fields["predicate"],
            fields["value"],
            fields["kind"],
            fields["confidence"],
            said_by,
        )
    )


def _claims(raw: dict) -> Either[tuple[Claim, ...], str]:
    items = raw.get("claims")
    if items is None:
        return Ok(())
    if not isinstance(items, list):
        return Err("yaml: claims is not a list")
    claims: list[Claim] = []
    for item in items:
        match _claim(item):
            case Err(reason):
                return Err(reason)
            case Ok(claim):
                claims.append(claim)
    return Ok(tuple(claims))


def _optional_text(raw: dict, key: str) -> Either[str | None, str]:
    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        return Err(f"yaml: {key} is not a string")
    return Ok(value)


#: 머리말 칸 이름 → 읽는 함수. 하나라도 모양이 틀리면 노트 전체가 Err.
_READERS: tuple[tuple[str, Callable[[dict, str], Either[Any, str]]], ...] = (
    ("origin", _text),
    ("project", _text),
    ("date", _text),
    ("kind", _text),
    ("title", _optional_text),
    ("omb_session_id", _optional_text),
    ("tags", _texts),
    ("tools", _texts),
    ("concepts", _texts),
    ("sources", _texts),
)


def _columns(raw: dict) -> Either[dict[str, Any], str]:
    columns: dict[str, Any] = {}
    for key, read in _READERS:
        match read(raw, key):
            case Err(reason):
                return Err(reason)
            case Ok(value):
                columns[key] = value
    return Ok(columns)


def _front(raw: dict, path: str, classify_origin: ClassifyOrigin) -> Either[FrontMatter, str]:
    match _columns(raw):
        case Err(reason):
            return Err(reason)
        case Ok(c):
            pass
    match _claims(raw):
        case Err(reason):
            return Err(reason)
        case Ok(claims):
            pass
    match parse_author(raw.get("author")):
        case Err(reason):
            return Err(reason)
        case Ok(author):
            pass
    return Ok(
        FrontMatter(
            title=c["title"],
            kind=c["kind"] or _derive_kind(path),
            origin=c["origin"] or classify_origin(path),
            project=c["project"] or derive_project(path),
            date=c["date"],
            tags=c["tags"],
            tools=c["tools"],
            concepts=c["concepts"],
            claims=claims,
            sources=c["sources"],
            omb_session_id=c["omb_session_id"],
            author=author,
        )
    )


def parse_vault_note(
    text: str, path: str, split_frontmatter: SplitFrontmatter, classify_origin: ClassifyOrigin
) -> Either[tuple[FrontMatter, str], str]:
    """NUL 을 뺀 파일 글자 → (머리말, trim_start 된 본문). 머리말이 없으면 경로에서만 채운다."""
    parse_text = text.removeprefix(_BOM)
    split = split_frontmatter(parse_text)
    raw: dict = {}
    body = parse_text
    if split is not None:
        yaml_text, body = split
        try:
            loaded = yaml.load(yaml_text, Loader=_StrOnlyLoader)  # noqa: S506 — 문자열 전용 SafeLoader 파생
        except yaml.YAMLError as e:
            return Err(f"yaml: {e}")
        if loaded is not None and not isinstance(loaded, dict):
            return Err("yaml: frontmatter is not a mapping")
        raw = loaded or {}
    match _front(raw, path, classify_origin):
        case Err(reason):
            return Err(reason)
        case Ok(front):
            return Ok((front, body.lstrip()))
