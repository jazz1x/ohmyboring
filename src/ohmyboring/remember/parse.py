"""remember 요청 파싱 — drudge 의 parse_remember_note 를 파이썬으로 미러한다 (순수).

정본: drudge/src/serve/mcp.rs:2087-2192 (인자 꺼내기·normalize_body·비밀 가림·태그·claims·
author). 여기서 나온 RememberNote 는 어디에도 쓰이지 않는다 — 문이 같은 요청을 파이썬
쓰기 경로에 「쓰지 않고」 태워 엔진이 실제로 쓴 노트와 칸별로 대조하는 그림자(E3a-1)뿐.
비밀 가림은 ohmyboring.search.redact(drudge/src/redact.rs:16 SECRET_PATTERN 이식분)를
그대로 쓴다. date 는 쓰기 때 채우는 칸이라 여기 비워 두고, 그림자는 엔진이 쓴 값을
그대로 받는다(E3a-1 — 번호·날짜 대조는 E3a-2 이후). 실패는 예외 대신 사유 문자열
값으로 돌아온다 — 문 핸들러 경계에서 한 번 접는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from ohmyboring import config as omb_env
from ohmyboring.result import Either, Err, Ok
from ohmyboring.search.redact import redact as _scrub

#: str 로만 읽을 태그 — 아래 로더의 클래스 본체가 아니라 모듈에서 참조해야 한다(내포 함수는
#: 클래스 속성을 보지 못한다 — ohmyboring.ingest.note 의 것과 같은 규약).
_STR_ONLY = "tag:yaml.org,2002:str"


class _StrOnlyLoader(yaml.SafeLoader):
    """머리말 스칼라 전부를 str 로 읽는 로더 — ohmyboring.ingest.note 의 것과 같은 규칙
    (serde_yaml 의 String 필드 규칙과 가깝게 — `date: 2026-10-01` 이 날짜가 아니라 글자로 온다)."""

    yaml_implicit_resolvers = {
        key: [(tag, regexp) for tag, regexp in resolvers if tag in (_STR_ONLY, "tag:yaml.org,2002:null")]
        for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


#: 엔진이 받는 origin 어휘 — drudge config::Origin::FromStr 과 같은 넷.
_ORIGINS = ("personal", "company", "mirror", "community")

#: author 어휘 — drudge frontmatter::Author::FromStr. agent:<name> 만 따로 해석한다.
_AUTHORS = ("owner", "inferred", "unknown")


@dataclass(frozen=True)
class Claim:
    """frontmatter::Claim — kind·confidence 는 파일에 그대로 저장되는 정제된 원시 값."""

    subject: str
    predicate: str
    value: str
    kind: str = ""
    confidence: str = ""
    said_by: str | None = None  # 오너 말한 claim 만 "owner"


@dataclass(frozen=True)
class FrontMatter:
    """render 가 받는 머리말 칸들 — remember 가 정하는 것만 담는다."""

    title: str
    kind: str
    origin: str
    project: str
    date: str
    tags: tuple[str, ...]
    tools: tuple[str, ...]
    concepts: tuple[str, ...]
    claims: tuple[Claim, ...]
    sources: tuple[str, ...]
    omb_session_id: str | None
    author: str


@dataclass(frozen=True)
class RememberNote:
    """파싱된 remember 노트 하나 — Rust 의 RememberNote { front, body }."""

    front: FrontMatter
    body: str


def sanitize_tag(raw: str) -> str | None:
    """옵시디안 안전 태그로 — drudge vault/remember.rs:16 그대로. 공백·허용 외 문자 → `-`,
    대시 쪼개기 붙임, 양끝 `-`/`/` 베기, 소문자. 빈 값·숫자만 → None."""
    out: list[str] = []
    prev_dash = False
    for ch in raw.strip().lower():
        if ch.isascii() and (ch.isalnum() or ch in "_/"):
            mapped = ch
        else:
            mapped = "-"
        if mapped == "-":
            if prev_dash:
                continue
            prev_dash = True
        else:
            prev_dash = False
        out.append(mapped)
    trimmed = "".join(out).strip("-/")
    if not trimmed or all(ch in "0123456789" for ch in trimmed):
        return None
    return trimmed


def _is_atx_heading(line: str) -> bool:
    hashes = len(line) - len(line.lstrip("#"))
    return 1 <= hashes <= 6 and line[hashes:].startswith(" ")


def _strip_trailing_empty_heading(body: str) -> str:
    """밑에 내용 없는 끝 헤딩을 벗긴다 — remember.rs:55. 빈 줄을 까고 마지막 줄이
    ATX 헤딩이면 빼고, 안 나올 때까지 반복(빈 절이 여러 겹이어도 전부)."""
    lines = body.split("\n")
    while True:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and _is_atx_heading(lines[-1].strip()):
            lines.pop()
            continue
        return "\n".join(lines)


def normalize_body(body: str) -> str:
    """LLM 이 낸 노트 본문을 깨끗한 마크다운으로 — remember.rs:80 그대로.

    리터럴 `\\n`·`\\t`·`\\r` 을 진짜 문자로, 모형이 과하게 붙이는 마크다운 문장 부호
    이스케이프(`` \\` ``·`\\#`·`\\"` …)를 풀고, 모르는 이스케이프는 `\\` 두 글자를 그대로
    둔다(진짜 백슬래시 — 경로·정규식 — 를 해치지 않는다). 마지막으로 내용 없는 끝
    헤딩을 벗기고 양끝을 다듬는다."""
    out: list[str] = []
    i = 0
    n = len(body)
    punctuation = '`#"*_[]()\\'
    while i < n:
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= n:
            out.append("\\")
            break
        nxt = body[i]
        i += 1
        if nxt == "n":
            out.append("\n")
        elif nxt == "t":
            out.append("\t")
        elif nxt == "r":
            out.append("\r")
        elif nxt in punctuation:
            out.append(nxt)
        else:
            out.append("\\")
            out.append(nxt)
    return _strip_trailing_empty_heading("".join(out).strip()).strip()


def _get_str(args: dict[str, Any], key: str) -> str:
    """Rust 의 get_str — 문자열이면 벗긴 값, 아니면 빈 문자열."""
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _get_arr(args: dict[str, Any], key: str) -> list[str]:
    """Rust 의 get_arr — 문자열 항목만 벗기고 빈 것 버림."""
    value = args.get(key)
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _clean(text: str) -> str:
    """필드 하나 정리 — normalize_body 뒤 비밀 가림(Rust 의 clean 클로저)."""
    return _scrub(normalize_body(text))


def parse_author(raw: Any) -> Either[str, str]:
    """author 칸 하나 — frontmatter::Author::FromStr. 없으면 unknown."""
    if raw is None:
        return Ok("unknown")
    if not isinstance(raw, str):
        return Err("author must be a string")
    match raw.strip():
        case "owner" | "inferred" | "unknown" as known:
            return Ok(known)
        case other:
            name = other.removeprefix("agent:").strip()
            if other.startswith("agent:") and name:
                return Ok(f"agent:{name}")
            return Err(f"author must be owner | inferred | unknown | agent:<name>, got {raw!r}")


def _parse_claim(value: Any) -> Either[Claim | None, str]:
    """claims 항목 하나 — 세 칸이 모두 비면 None(버림), said_by 는 오너만."""
    if not isinstance(value, dict):
        return Ok(None)

    def field(key: str) -> str:
        raw = value.get(key)
        return raw.strip() if isinstance(raw, str) else ""

    said_by: str | None = None
    if "said_by" in value:
        raw = value["said_by"]
        if not isinstance(raw, str):
            return Err("claims[].said_by: said_by must be a string")
        if raw.strip() != "owner":
            return Err(f"claims[].said_by: said_by must be owner, got {raw!r}")
        said_by = "owner"
    subject, predicate, claim_value = field("subject"), field("predicate"), field("value")
    if not subject or not predicate or not claim_value:
        return Ok(None)
    return Ok(Claim(subject, predicate, claim_value, field("kind"), field("confidence"), said_by))


def _parse_claims(args: dict[str, Any]) -> Either[tuple[Claim, ...], str]:
    """claims 배열 전부 — 빈 칸(세 칸 중 하나라도 빈)은 버리고, 각 칸은 정리(clean)한다."""
    raw_claims = args.get("claims")
    if not isinstance(raw_claims, list):
        return Ok(())
    claims: list[Claim] = []
    for raw in raw_claims:
        match _parse_claim(raw):
            case Ok(None):
                continue
            case Ok(claim):
                claims.append(
                    Claim(
                        subject=_clean(claim.subject),
                        predicate=_clean(claim.predicate),
                        value=_clean(claim.value),
                        kind=_clean(claim.kind),
                        confidence=_clean(claim.confidence),
                        said_by=claim.said_by,
                    )
                )
            case Err(reason):
                return Err(reason)
    return Ok(tuple(claims))


def normalize_supersedes_path(raw: str) -> str:
    """교정 대상 경로의 여러 적법을 document 표 키 형태로 — mcp.rs:2054.

    에이전트가 본 적법(`/vault/wiki/…`·`wiki/…`·맨바닥 `wiki-NNNN.md`)을 접고, 그
    밖의 것은 그대로 둔다(그러면 unknown 으로 잡혀 응답이 이름을 밝힌다).
    """
    if raw.startswith("/vault/wiki/"):
        return raw
    if raw.startswith("wiki/"):
        return f"/vault/{raw}"
    if "/" not in raw:
        return f"/vault/wiki/{raw}"
    return raw


def parse_supersedes(args: dict[str, Any]) -> Either[list[str], str]:
    """supersedes 인자 하나 — mcp.rs:2026. 없으면 빈 목록, 모양이 다류면 -32602 사유."""
    if "supersedes" not in args:
        return Ok([])
    raw = args["supersedes"]
    if not isinstance(raw, list):
        return Err("supersedes must be an array of source_path strings")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return Err("supersedes items must be strings (source_path of the note this one corrects)")
        if item.strip():
            out.append(normalize_supersedes_path(item.strip()))
    return Ok(out)


def parse_judge(args: dict[str, Any]) -> Either[str | None, str]:
    """judge 인자 하나 — mcp.rs:2068 parse_person. 없으면 None, 어휘는 author 와 같다."""
    if "judge" not in args:
        return Ok(None)
    raw = args["judge"]
    if not isinstance(raw, str):
        return Err("judge must be a string")
    return parse_author(raw)


def parse_remember_note(args: dict[str, Any]) -> Either[RememberNote, str]:
    """remember 인자 하나를 RememberNote 로 — mcp.rs:2087 의 순서 그대로.

    title·body 는 정제 전 원시 값이 비어 있으면 거절(「missing argument: …」).
    실패는 사유 문자열 값 — 경계에서 한 번 접는다.
    """
    title = _get_str(args, "title")
    body = normalize_body(_get_str(args, "body"))
    if not title:
        return Err("missing argument: title")
    if not body:
        return Err("missing argument: body")

    title = _clean(title)
    body = _scrub(body)

    origin_in = _get_str(args, "origin")
    if not origin_in:
        origin = "personal"
    elif origin_in in _ORIGINS:
        origin = origin_in
    else:
        return Err(f"invalid origin: {origin_in}")
    repo = omb_env.canonical_repo(_get_str(args, "repo"))

    omb_session_id = _get_str(args, "omb_session_id") or None

    tags = [t for t in (sanitize_tag(raw) for raw in _get_arr(args, "tags")) if t is not None][:6]
    if repo and (sanitized := sanitize_tag(repo)):
        tags.insert(0, f"repo/{sanitized}")

    match _parse_claims(args):
        case Ok(claims):
            pass
        case Err(reason):
            return Err(reason)

    match parse_author(args.get("author")):
        case Ok(author):
            pass
        case Err(reason):
            return Err(reason)

    front = FrontMatter(
        title=title,
        kind="note",
        origin=origin,
        project=repo,
        date="",
        tags=tuple(tags),
        tools=tuple(_clean(t) for t in _get_arr(args, "tools")),
        concepts=tuple(_clean(c) for c in _get_arr(args, "concepts")),
        claims=claims,
        sources=tuple(_scrub(s) for s in _get_arr(args, "sources")),
        omb_session_id=omb_session_id,
        author=author,
    )
    return Ok(RememberNote(front, body))
