"""볼트 노트 → NoteDoc — drudge 의 파일 읽기 + frontmatter::parse 를 파이썬으로 미러한다.

Rust 동작(정본): UTF-8 로 읽고(못 읽으면 건과 사유), NUL 제거(strip_nul — sha 는 그 뒤 본문),
BOM 은 파싱에서만 떼고 sha 에는 남는다, `---\\n` … `\\n---\\n` 사이를 YAML 로, 본문은 trim_start.
머리말이 비어 있으면 kind 만 경로에서 채운다("/notes/"→note, "/memory"→memory, 아니면 doc) —
origin·project 의 cfg 기반 enrich 는 E3 라 여기 없다. 파싱은 경계에서 한 번.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from ohmyboring.result import Either, Err, Ok

_BOM = "\ufeff"


@dataclass(frozen=True)
class NoteDoc:
    """파싱된 노트 하나 — Rust 의 (FrontMatter, body) + sha."""

    source_path: str
    title: str | None
    kind: str
    tags: tuple[str, ...]
    body: str
    sha: str


@dataclass(frozen=True)
class Skipped:
    """읽거나 파싱하지 못한 파일 — 사유를 값으로 담는다(조용한 제외는 0 과 구분 못 함, §4)."""

    source_path: str
    reason: str


class _FrontmatterLoader(yaml.SafeLoader):
    """머리말 스칼라 전부를 str 로 읽는 로더 — serde_yaml 의 String 필드 규칙과 가깝게.

    PyYAML 의 기본 해석(bool/int/timestamp)을 끄면 `title: 2026-09-03` 같은 값도 날짜가
    아니라 글자로 온다. 구조 오류(못 닫는 흐름 목록 등)는 그대로 예외.
    """


_STR_ONLY = "tag:yaml.org,2002:str"
_FrontmatterLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag in (_STR_ONLY, "tag:yaml.org,2002:null")]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _derive_kind(path: str) -> str:
    if "/notes/" in path:
        return "note"
    if "/memory" in path:
        return "memory"
    return "doc"


def _field_error(raw: dict) -> str | None:
    title = raw.get("title")
    if title is not None and not isinstance(title, str):
        return "yaml: title is not a string"
    kind = raw.get("kind")
    if kind is not None and not isinstance(kind, str):
        return "yaml: kind is not a string"
    tags = raw.get("tags")
    if tags is not None and not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
        return "yaml: tags is not a list of strings"
    return None


def _parse_frontmatter(yaml_text: str, path: str) -> Either[tuple[str | None, str, tuple[str, ...]], str]:
    try:
        raw = yaml.load(yaml_text, Loader=_FrontmatterLoader)
    except yaml.YAMLError as e:
        return Err(f"yaml: {e}")
    if raw is None:
        return Ok((None, _derive_kind(path), ()))
    if not isinstance(raw, dict):
        return Err("yaml: frontmatter is not a mapping")
    if bad := _field_error(raw):
        return Err(bad)
    title = raw.get("title")
    kind = raw.get("kind")
    tags = raw.get("tags")
    return Ok((title, kind if kind else _derive_kind(path), tuple(tags or ())))


def read_note(path: str) -> Either[NoteDoc, Skipped]:
    """파일 하나를 NoteDoc 으로 — 실패는 Skipped 값(경로+사유)이다."""
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        return Err(Skipped(path, f"unreadable: {e}"))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        return Err(Skipped(path, f"unreadable: not utf-8 ({e.reason})"))
    text = text.replace("\x00", "")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    raw = text[len(_BOM) :] if text.startswith(_BOM) else text
    title: str | None = None
    kind = _derive_kind(path)
    tags: tuple[str, ...] = ()
    if raw.startswith("---\n"):
        rest = raw[4:]
        end = rest.find("\n---\n")
        if end != -1:
            parsed = _parse_frontmatter(rest[:end], path)
            match parsed:
                case Ok(value):
                    title, kind, tags = value
                case Err(reason):
                    return Err(Skipped(path, reason))
            body = rest[end + 5 :]
        else:
            body = raw
    else:
        body = raw
    return Ok(NoteDoc(path, title, kind, tags, body.lstrip(), sha))
