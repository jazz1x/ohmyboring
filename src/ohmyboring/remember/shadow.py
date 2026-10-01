"""remember 그림자 — 같은 요청을 파이썬 쓰기 경로에 「쓰지 않고」 태워 칸별 대조 (E3a-1).

문이 remember 를 엔진으로 바이트 그대로 넘기고 응답까지 본 뒤에 이 모듈을 돌린다 — 그림자는
응답에 영향이 없어야 한다. 쓰기 0: 볼트·DB·그래프에 손 대는 일 없이 사건(remember_shadow)
한 줄의 재료만 만든다(기록은 문 핸들러가 adapters/events 에). 본문 원문은 사건에 싣지
않는다(비밀 경계 — 칸 이름·사유만 싣는다).

대조 근거(계약): 엔진 응답(MCP 텍스트 'remembered → wiki/wiki-NNNN.md …' / 'skipped —
duplicate of …', HTTP JSON {source_path,…})에서 실제 경로를 얻어 문의 /vault(ro) 에서 그
노트를 읽고, 엔진 파일의 머리말·본문과 파이썬 렌더를 칸별로 대조한다. id·date 는 엔진 것을
그대로 받아 렌더에 넣는다(번호 매기기·날짜 대조는 E3a-2 이후). relates_to 는 엔진이 노트를
쓴 뒤 그래프 투영이 다시 쓰므로 비교에서 빼고 사유를 사건에 적는다. PII 게이트(E3a-2)가
붙인 pii-flag 태그·가린 본문은 아직 이식 전이라, 그 태그가 있는 노트의 불일치는 사유
'pii_not_ported' 로 따로 센다.

실패는 전부 값(ShadowEvent status=error 의 사유)으로 돌아오고, 예외는 문 핸들러 경계에서
한 번 접는다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import yaml

from ohmyboring.remember.parse import RememberNote, parse_remember_note
from ohmyboring.remember.render import render_wiki_note
from ohmyboring.result import Either, Err, Ok

#: 사건 이름 — 문이 돌리니 문의 다른 사건들과 같은 축.
EVENT_NAME = "remember_shadow"

#: relates_to 제외 사유 — 엔진이 쓴 뒤 그래프 투영이 다시 쓴다(사건에 적는다).
RELATES_TO_EXCLUDED = "excluded: engine rewrites relates_to after write (graph projection)"

#: PII 게이트 이식(E3a-2) 전의 알려진 불일치 — 따로 센다.
REASON_PII_NOT_PORTED = "pii_not_ported"

_PII_FLAG_TAG = "pii-flag"

#: 대조하는 칸들 — id·date 는 엔진 것을 받아 넣어 대조 밖이고 relates_to 는 제외.
COMPARED_FIELDS = (
    "title",
    "kind",
    "origin",
    "project",
    "tags",
    "tools",
    "concepts",
    "claims",
    "sources",
    "omb_session_id",
    "author",
    "body",
)

_REMEMBERED_RE = re.compile(r"remembered → (wiki/wiki-([0-9]+)\.md)")
_DUPLICATE_RE = re.compile(r"skipped — duplicate of (\S+)")

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


@dataclass(frozen=True)
class Extracted:
    """엔진 응답에서 얻은 실제 경로 — remembered 면 새 노트, duplicate 면 기존 노트."""

    note_id: str
    source_path: str
    duplicate: bool


@dataclass(frozen=True)
class ShadowEvent:
    """사건 한 줄의 재료 — status ok(일치)·mismatch·error, 칸별 불일치 이름 목록(fields)."""

    status: str
    source_path: str | None = None
    omb_session_id: str | None = None
    fields: tuple[str, ...] = ()
    reason: str | None = None
    duplicate: bool = False


def wiki_stem(source_path: str) -> str | None:
    """경로 끝의 `wiki-NNNN`(확장자 없음) — drudge vault::wiki_stem 과 같다."""
    stem = source_path.rsplit("/", 1)[-1].removesuffix(".md")
    return stem if stem.startswith("wiki-") else None


def extract_from_mcp_text(text: str) -> Either[Extracted, str]:
    """MCP tools/call remember 답 텍스트에서 실제 경로 하나."""
    if match := _REMEMBERED_RE.search(text):
        note_id = f"wiki-{match.group(2)}"
        return Ok(Extracted(note_id, f"/vault/wiki/{note_id}.md", False))
    if match := _DUPLICATE_RE.search(text):
        stem = wiki_stem(match.group(1))
        if stem is None:
            return Err(f"duplicate answer names no wiki note: {match.group(1)!r}")
        return Ok(Extracted(stem, match.group(1), True))
    return Err("unrecognized remember answer")


def extract_from_mcp_response(status: int, body: bytes) -> Either[Extracted, str]:
    """POST /mcp 의 엔진 답 전체 — 2xx JSON-RPC result.content[].text 에서 경로를 얻는다."""
    if not 200 <= status < 300:
        return Err(f"engine answered {status} — no note was written")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        return Err(f"engine answer is not JSON: {e}")
    if not isinstance(data, dict):
        return Err("engine answer is not a JSON object")
    result = data.get("result")
    if not isinstance(result, dict):
        return Err("engine answer has no result object")
    content = result.get("content")
    text = "".join(
        item.get("text", "")
        for item in (content if isinstance(content, list) else [])
        if isinstance(item, dict) and item.get("type") == "text"
    )
    if not text:
        return Err("engine answer carries no text content")
    return extract_from_mcp_text(text)


def extract_from_http_response(status: int, body: bytes) -> Either[Extracted, str]:
    """POST /remember 의 엔진 답 전체 — JSON {source_path,…} 에서 실제 경로를 얻는다."""
    if not 200 <= status < 300:
        return Err(f"engine answered {status} — no note was written")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        return Err(f"engine answer is not JSON: {e}")
    if not isinstance(data, dict):
        return Err("engine answer is not a JSON object")
    source_path = data.get("source_path")
    if not isinstance(source_path, str) or not source_path.strip():
        return Err("engine answer names no source_path")
    stem = wiki_stem(source_path.strip())
    if stem is None:
        return Err(f"source_path names no wiki note: {source_path!r}")
    return Ok(Extracted(stem, source_path.strip(), data.get("duplicate") is not None))


def _extract(route: str, status: int, body: bytes) -> Either[Extracted, str]:
    match route:
        case "mcp":
            return extract_from_mcp_response(status, body)
        case "remember":
            return extract_from_http_response(status, body)
        case _:
            return Err(f"unknown remember route: {route!r}")


def _parse_note_text(
    text: str, split_frontmatter: Callable[[str], tuple[str, str] | None]
) -> Either[tuple[dict[str, Any], str], str]:
    """노트 텍스트를 (머리말 맵, 본문) 으로 — 쪼개기는 주입받는다(정본은 vault_note.split_frontmatter)."""
    split = split_frontmatter(text)
    if split is None:
        return Err("note has no frontmatter")
    raw_yaml, body = split
    try:
        front = yaml.load(raw_yaml, Loader=_StrOnlyLoader)
    except yaml.YAMLError as e:
        return Err(f"note frontmatter is not YAML: {e}")
    if not isinstance(front, dict):
        return Err("note frontmatter is not a mapping")
    return Ok((front, body))


def _str_list(raw: Any) -> Either[list[str], str]:
    if raw is None:
        return Ok([])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return Err(f"expected a list of strings, got {raw!r}")
    return Ok(list(raw))


def _claims_view(raw: Any) -> Either[list[dict[str, Any]], str]:
    """claims 를 대조용으로 — 칸 순서 고정, said_by 는 없으면 None."""
    if raw is None:
        return Ok([])
    if not isinstance(raw, list):
        return Err(f"claims is not a list: {raw!r}")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            return Err(f"claim is not a mapping: {item!r}")
        out.append(
            {
                "subject": item.get("subject") or "",
                "predicate": item.get("predicate") or "",
                "value": item.get("value") or "",
                "kind": item.get("kind") or "",
                "confidence": item.get("confidence") or "",
                "said_by": item.get("said_by") or None,
            }
        )
    return Ok(out)


def _front_view(front: dict[str, Any]) -> Either[dict[str, Any], str]:
    """머리말 맵을 대조용 뷰로 — 비교 칸이 모양을 갖추지 못하면 Err(대상 노트의 결함은 error)."""
    view: dict[str, Any] = {}
    for key in ("title", "kind", "origin", "project", "author"):
        value = front.get(key)
        if value is not None and not isinstance(value, str):
            return Err(f"{key} is not a string: {value!r}")
        view[key] = value or ""
    omb = front.get("omb_session_id")
    if omb is not None and not isinstance(omb, str):
        return Err(f"omb_session_id is not a string: {omb!r}")
    view["omb_session_id"] = omb or None
    for key in ("tags", "tools", "concepts", "sources"):
        match _str_list(front.get(key)):
            case Ok(items):
                view[key] = items
            case Err(reason):
                return Err(f"{key}: {reason}")
    match _claims_view(front.get("claims")):
        case Ok(claims):
            view["claims"] = claims
        case Err(reason):
            return Err(reason)
    return Ok(view)


def _omb_session_id(arguments: Any) -> str | None:
    raw = arguments.get("omb_session_id") if isinstance(arguments, dict) else None
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


@dataclass(frozen=True)
class ShadowRequest:
    """그림자 한 번의 입력 — 문 핸들러가 응답을 본 뒤에 싣는다."""

    route: str
    arguments: dict[str, Any]
    engine_status: int
    engine_body: bytes
    vault_dir: str
    read_note: Callable[[str, str], str | None]
    split_frontmatter: Callable[[str], tuple[str, str] | None]


def _read_inputs(
    request: ShadowRequest, omb_session_id: str | None
) -> Either[tuple[Extracted, RememberNote, dict[str, Any], str], ShadowEvent]:
    """읽기·파싱 단계 전부 — 경로 뽑기·볼트 읽기·요청 파싱·엔진 노트 파싱.

    어느 단계든 실패는 ShadowEvent(status=error) 값으로 돌아온다."""
    match _extract(request.route, request.engine_status, request.engine_body):
        case Err(reason):
            return Err(ShadowEvent("error", omb_session_id=omb_session_id, reason=reason))
        case Ok(extracted):
            pass
    note_text = request.read_note(request.vault_dir, extracted.note_id)
    if note_text is None:
        return Err(
            ShadowEvent(
                "error",
                source_path=extracted.source_path,
                omb_session_id=omb_session_id,
                reason=f"note not in vault: {extracted.note_id}",
            )
        )
    match parse_remember_note(request.arguments):
        case Err(reason):
            return Err(
                ShadowEvent(
                    "error",
                    source_path=extracted.source_path,
                    omb_session_id=omb_session_id,
                    reason=f"python parse: {reason}",
                )
            )
        case Ok(note):
            pass
    match _parse_note_text(note_text, request.split_frontmatter):
        case Err(reason):
            return Err(
                ShadowEvent(
                    "error",
                    source_path=extracted.source_path,
                    omb_session_id=omb_session_id,
                    reason=f"engine note: {reason}",
                )
            )
        case Ok((engine_front, engine_note_body)):
            pass
    return Ok((extracted, note, engine_front, engine_note_body))


def _render_python(
    extracted: Extracted,
    note: RememberNote,
    engine_front: dict[str, Any],
    split_frontmatter: Callable[[str], tuple[str, str] | None],
    omb_session_id: str | None,
) -> Either[tuple[dict[str, Any], dict[str, Any], str], ShadowEvent]:
    """파이썬 렌더 한 벌 — id·date 는 엔진 것을 그대로 받아 렌더에 넣고(번호·날짜 대조는
    E3a-2 이후), 렌더를 다시 파싱해 양쪽 머리말을 대조용 뷰로 만든다."""
    engine_id = engine_front.get("id")
    if not isinstance(engine_id, str) or not engine_id:
        engine_id = extracted.note_id
    engine_date = engine_front.get("date")
    py_note = replace(
        note,
        front=replace(note.front, date=engine_date if isinstance(engine_date, str) else ""),
    )
    rendered = render_wiki_note(engine_id, py_note.front, py_note.body)
    match _parse_note_text(rendered, split_frontmatter):
        case Err(reason):
            return Err(
                ShadowEvent(
                    "error",
                    source_path=extracted.source_path,
                    omb_session_id=omb_session_id,
                    reason=f"python render: {reason}",
                )
            )
        case Ok((py_front, py_body)):
            pass
    match _front_view(engine_front):
        case Err(reason):
            return Err(
                ShadowEvent(
                    "error",
                    source_path=extracted.source_path,
                    omb_session_id=omb_session_id,
                    reason=f"engine note: {reason}",
                )
            )
        case Ok(engine_view):
            pass
    match _front_view(py_front):
        case Err(reason):
            return Err(
                ShadowEvent(
                    "error",
                    source_path=extracted.source_path,
                    omb_session_id=omb_session_id,
                    reason=f"python render: {reason}",
                )
            )
        case Ok(py_view):
            pass
    return Ok((engine_view, py_view, py_body))


def _compare(
    extracted: Extracted,
    engine_view: dict[str, Any],
    py_view: dict[str, Any],
    bodies: tuple[str, str],
    omb_session_id: str | None,
) -> ShadowEvent:
    """칸별 대조 — 불일치한 칸 이름만 모아 사건 한 줄로."""
    engine_note_body, py_body = bodies
    mismatches: list[str] = []
    for field in COMPARED_FIELDS:
        if field == "body":
            if engine_note_body.strip() != py_body.strip():
                mismatches.append(field)
        elif engine_view[field] != py_view[field]:
            mismatches.append(field)
    reason = None
    if mismatches and _PII_FLAG_TAG in engine_view["tags"] and _PII_FLAG_TAG not in py_view["tags"]:
        reason = REASON_PII_NOT_PORTED
    return ShadowEvent(
        "mismatch" if mismatches else "ok",
        source_path=extracted.source_path,
        omb_session_id=omb_session_id,
        fields=tuple(mismatches),
        reason=reason,
        duplicate=extracted.duplicate,
    )


def run_shadow(request: ShadowRequest) -> ShadowEvent:
    """파이썬 쓰기 경로를 「쓰지 않고」 태워 엔진 노트와 칸별 대조 — 사건 한 줄의 재료.

    읽기 쪽 실패(vault 파일 없음 등)는 값으로 돌아오고, 예외는 문 핸들러 경계에서 접는다.
    """
    omb_session_id = _omb_session_id(request.arguments)
    match _read_inputs(request, omb_session_id):
        case Err(event):
            return event
        case Ok((extracted, note, engine_front, engine_note_body)):
            pass
    match _render_python(extracted, note, engine_front, request.split_frontmatter, omb_session_id):
        case Err(event):
            return event
        case Ok((engine_view, py_view, py_body)):
            pass
    return _compare(extracted, engine_view, py_view, (engine_note_body, py_body), omb_session_id)


def event_payload(event: ShadowEvent) -> dict[str, Any]:
    """adapters/events 기록용 본문 — 본문 원문은 전부 빠진다(비밀 경계)."""
    payload: dict[str, Any] = {"fields": list(event.fields), "relates_to": RELATES_TO_EXCLUDED}
    if event.source_path is not None:
        payload["source_path"] = event.source_path
    if event.omb_session_id is not None:
        payload["omb_session_id"] = event.omb_session_id
    if event.reason is not None:
        payload["reason"] = event.reason
    if event.duplicate:
        payload["duplicate"] = True
    return payload
