"""remember 그림자 — 같은 요청을 파이썬 쓰기 경로에 「쓰지 않고」 태워 「엔진이 한 결정과
같은 결정을 했을지」를 사건 한 줄로 보여 준다 (E3a-2).

문이 remember 를 엔진으로 바이트 그대로 넘기고 응답까지 본 뒤에 이 모듈을 돌린다 — 그림자는
응답에 영향이 없어야 한다. 쓰기 0: 볼트·DB·그래프에 손 대는 일 없이 사건(remember_shadow)
한 줄의 재료만 만든다(기록은 문 핸들러가 adapters/events 에). 본문 원문은 사건에 싣지
않는다(비밀 경계 — 칸 이름·경로·규칙 이름·사유만 싣는다).

대조는 두 겹이다:
1. 결정 대조 — 파이썬이 같은 결정을 낼지: PII 게이트(pii.py, block 이면 거절)를 지나
   중복 문(dedup.py)의 갈래마다(same_session·probable_session·exact_title·embedding,
   대체는 점수 판정) 걸러서 stored | superseded | skipped | blocked | refused 를 내고
   엔진 응답의 결정과 맞춘다. 걸러진 요청은 결정만 비교한다 — 기존 노트와 칸별 대조는
   안 한다(요청을 쓴 것이 아니니 칸을 맞출 이유가 없다. E3a-1 은 여기서 어긋남 하나를
   거짓으로 냈고, 그 사례가 걸러둘 결정만 비교한다는 단언으로 못 박혀 있다).
2. 칸 대조(E3a-1) — stored/superseded 에서만: 엔진이 실제로 쓴 노트를 볼트(ro)에서 읽어
   파이썬 렌더와 칸별로 맞춘다. id·date 는 엔진 것을 그대로 받아 렌더에 넣고 relates_to
   는 그래프 투영이 다시 쓰므로 제외.

어긋남마다 사유를 남겨, 사걸만 보고 (가) 파이썬 결함 (나) 엔진이 틀렸거나 의도한 차이
(다) 모름 셋 중 어디인지 가른다. 사유 어휘(한 줄, grep 가능):
  decision engine=<결정> python=<결정>   — 결정 불일치(경로 포함)
  fields <칸,…>                        — 칸 불일치
  pii-rules <규칙,…>                   — 칸 차이를 설명하는 PII 규칙(가림 입력이 갈린 것)
  pii-gate-missing                     — 그림자에 규칙 파일이 없는데 엔진 노트에 pii-flag

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

from ohmyboring.remember import dedup as _dedup
from ohmyboring.remember import pii as _pii
from ohmyboring.remember.parse import (
    RememberNote,
    _StrOnlyLoader,
    parse_judge,
    parse_remember_note,
    parse_supersedes,
)
from ohmyboring.remember.render import render_wiki_note
from ohmyboring.result import Either, Err, Ok

#: 사건 이름 — 문이 돌리니 문의 다른 사건들과 같은 축.
EVENT_NAME = "remember_shadow"

#: relates_to 제외 사유 — 엔진이 쓴 뒤 그래프 투영이 다시 쓴다(사건에 적는다).
RELATES_TO_EXCLUDED = "excluded: engine rewrites relates_to after write (graph projection)"

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

_PII_FLAG_TAG = "pii-flag"

#: 엔진 응답에서 읽는 결정 어휘 — DedupOutcome::status + 게이트 거절 둘.
DECISION_STORED = _dedup.OUTCOME_STORED
DECISION_SUPERSEDED = _dedup.OUTCOME_SUPERSEDED
DECISION_SKIPPED = _dedup.OUTCOME_SKIPPED
DECISION_BLOCKED = "blocked"
DECISION_REFUSED = "refused"

_REMEMBERED_RE = re.compile(r"remembered → wiki/wiki-([0-9]+)\.md(?: \(supersedes wiki/(wiki-[0-9]+)\.md\))?")
_DUPLICATE_RE = re.compile(r"skipped — duplicate of (\S+)")
_PII_BLOCK_RE = re.compile(r"PII gate blocked by rule '([^']+)'")
_OWNER_TOKEN_NEEDLE = "owner door token"
_OWNER_SUPERSEDE_NEEDLE = "only the owner may supersede"


@dataclass(frozen=True)
class Extracted:
    """엔진 응답에서 얻은 실제 경로 — remembered 면 새 노트, duplicate 면 기존 노트."""

    note_id: str
    source_path: str
    duplicate: bool
    supersedes: str | None = None  # 엔진이 대체했다고 말한 기존 노트 id (wiki-NNNN)


@dataclass(frozen=True)
class Decision:
    """한 쪽의 결정 — outcome 은 엔진·파이썬 공통 어휘, 경로와 규칙 한 줄은 사유용."""

    outcome: str
    note_path: str | None = None
    existing_path: str | None = None
    detail: str = ""

    def describe(self) -> str:
        """사유 한 줄에 쓰는 짧은 형태 — 경로가 없는 차단·거절은 규칙·사유를 덧붙인다."""
        out = self.outcome
        if self.existing_path:
            out += f":{self.existing_path}"
        elif self.note_path:
            out += f":{self.note_path}"
        elif self.detail:
            out += f":{self.detail}"
        return out


@dataclass(frozen=True)
class ShadowEvent:
    """사건 한 줄의 재료 — status ok(일치)·mismatch·error, 칸별 불일치 이름(fields)."""

    status: str
    source_path: str | None = None
    omb_session_id: str | None = None
    fields: tuple[str, ...] = ()
    reason: str | None = None
    duplicate: bool = False
    decision: str | None = None  # 파이썬 결정 describe()
    engine_decision: str | None = None  # 엔진 결정 describe()
    branch: str | None = None  # 파이썬이 찾은 중복 갈래


def wiki_stem(source_path: str) -> str | None:
    """경로 끝의 `wiki-NNNN`(확장자 없음) — drudge vault::wiki_stem 과 같다."""
    stem = source_path.rsplit("/", 1)[-1].removesuffix(".md")
    return stem if stem.startswith("wiki-") else None


def _decision_from_error(message: str) -> Decision | None:
    """엔진 거절 메시지를 결정으로 — PII 게이트 차단과 owner 거절만 결정 대조에 쓴다.
    그 밖의 -32603(저장·수집 고장)은 그림자가 예측할 수 없는 일이라 여전히 error 다."""
    if match := _PII_BLOCK_RE.search(message):
        return Decision(DECISION_BLOCKED, detail=match.group(1))
    if _OWNER_TOKEN_NEEDLE in message or _OWNER_SUPERSEDE_NEEDLE in message:
        return Decision(DECISION_REFUSED, detail=message.split(":", 1)[0])
    return None


def _extract_from_mcp_text(text: str) -> Either[tuple[Extracted, Decision], str]:
    if match := _REMEMBERED_RE.search(text):
        note_id = f"wiki-{match.group(1)}"
        supersedes = match.group(2)  # 이미 "wiki-NNNN" 형태 — 접두사를 더 붙이지 않는다
        decision = (
            Decision(
                DECISION_SUPERSEDED,
                note_path=f"/vault/wiki/{note_id}.md",
                existing_path=f"/vault/wiki/{supersedes}.md",
            )
            if supersedes
            else Decision(DECISION_STORED, note_path=f"/vault/wiki/{note_id}.md")
        )
        return Ok(
            (
                Extracted(note_id, f"/vault/wiki/{note_id}.md", supersedes is not None, supersedes),
                decision,
            )
        )
    if match := _DUPLICATE_RE.search(text):
        stem = wiki_stem(match.group(1))
        if stem is None:
            return Err(f"duplicate answer names no wiki note: {match.group(1)!r}")
        return Ok(
            (
                Extracted(stem, match.group(1), True),
                Decision(DECISION_SKIPPED, existing_path=match.group(1)),
            )
        )
    return Err("unrecognized remember answer")


def extract_from_mcp_text(text: str) -> Either[Extracted, str]:
    """MCP tools/call remember 답 텍스트에서 실제 경로 하나 (E3a-1 호환 껍질)."""
    match _extract_from_mcp_text(text):
        case Ok((extracted, _)):
            return Ok(extracted)
        case Err(reason):
            return Err(reason)


def extract_from_mcp_response(status: int, body: bytes) -> Either[Extracted, str]:
    """POST /mcp 의 엔진 답 전체 — 2xx JSON-RPC result.content[].text 에서 경로를 얻는다."""
    match _extract_mcp_decision(status, body):
        case Ok((extracted, _)):
            return Ok(extracted)
        case Err(reason):
            return Err(reason)


def _mcp_error_decision(error: dict[str, Any]) -> Either[tuple[Extracted, Decision], str]:
    """JSON-RPC error 객체를 (빈 경로, 결정)으로 — 차단·거절만 결정이고 나머지는 error."""
    message = str(error.get("message") or "")
    if decision := _decision_from_error(message):
        return Ok((Extracted("", "", False), decision))
    return Err(f"engine answered error {error.get('code')}: {message}")


def _mcp_result_text(data: dict[str, Any]) -> Either[str, str]:
    """JSON-RPC result.content[].text 를 한 덩어리로."""
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
    return Ok(text)


def _extract_mcp_decision(status: int, body: bytes) -> Either[tuple[Extracted, Decision], str]:
    """MCP 답에서 (경로, 결정) — JSON-RPC error 도 결정으로 읽는다(200 위를 탈 수 있다)."""
    if not 200 <= status < 300:
        return Err(f"engine answered {status} — no note was written")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        return Err(f"engine answer is not JSON: {e}")
    if not isinstance(data, dict):
        return Err("engine answer is not a JSON object")
    if isinstance(data.get("error"), dict):
        return _mcp_error_decision(data["error"])
    match _mcp_result_text(data):
        case Err(reason):
            return Err(reason)
        case Ok(text):
            pass
    return _extract_from_mcp_text(text)


def extract_from_http_response(status: int, body: bytes) -> Either[Extracted, str]:
    """POST /remember 의 엔진 답 전체 — JSON {source_path,…} 에서 실제 경로를 얻는다."""
    match _extract_http_decision(status, body):
        case Ok((extracted, _)):
            return Ok(extracted)
        case Err(reason):
            return Err(reason)


def _http_success_decision(data: dict[str, Any]) -> Either[tuple[Extracted, Decision], str]:
    """2xx JSON {source_path, duplicate, …} — duplicate 가 새 경로와 다륾면 대체다."""
    source_path = data.get("source_path")
    if not isinstance(source_path, str) or not source_path.strip():
        return Err("engine answer names no source_path")
    source_path = source_path.strip()
    stem = wiki_stem(source_path)
    if stem is None:
        return Err(f"source_path names no wiki note: {source_path!r}")
    duplicate = data.get("duplicate")
    if not isinstance(duplicate, str) or not duplicate.strip():
        return Ok((Extracted(stem, source_path, False), Decision(DECISION_STORED, note_path=source_path)))
    duplicate = duplicate.strip()
    if duplicate == source_path:
        return Ok((Extracted(stem, source_path, True), Decision(DECISION_SKIPPED, existing_path=duplicate)))
    return Ok(
        (
            Extracted(stem, source_path, True, wiki_stem(duplicate)),
            Decision(DECISION_SUPERSEDED, note_path=source_path, existing_path=duplicate),
        )
    )


def _http_error_decision(data: dict[str, Any], status: int) -> Either[tuple[Extracted, Decision], str]:
    """비 2xx JSON — 차단·거절 메시지면 결정으로, 아니면 error 그대로."""
    message = str(data.get("error") or "")
    if decision := _decision_from_error(message):
        return Ok((Extracted("", "", False), decision))
    return Err(f"engine answered {status}: {message or 'no note was written'}")


def _extract_http_decision(status: int, body: bytes) -> Either[tuple[Extracted, Decision], str]:
    """HTTP 답에서 (경로, 결정) — 400/500 JSON 에러도 결정으로 읽는다."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        if 200 <= status < 300:
            return Err(f"engine answer is not JSON: {e}")
        return Err(f"engine answered {status} — no note was written")
    if not isinstance(data, dict):
        return Err("engine answer is not a JSON object")
    if not 200 <= status < 300:
        return _http_error_decision(data, status)
    return _http_success_decision(data)


def _extract(route: str, status: int, body: bytes) -> Either[tuple[Extracted, Decision], str]:
    match route:
        case "mcp":
            return _extract_mcp_decision(status, body)
        case "remember":
            return _extract_http_decision(status, body)
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
    list_notes: Callable[[], list[str]]  # wiki 디렉토리의 note_id 목록 (읽기 전용)
    pii_scanner: _pii.PiiScanner | None  # 문이 볼트 rules 에서 불러 온 게이트 (없음 = 비활성)
    is_owner: bool = False  # owner token 검증 통과 여부 — 문이 헤더와 비교해 싣는다
    nearest_document: _dedup.NearestDocument | None = None  # 임베딩 갈래 (없음 = 그 갈래 못 봄)


def _gate_decision(request: ShadowRequest, note: RememberNote) -> tuple[Decision | None, RememberNote, bool]:
    """owner 자격 → supersedes 모양 → PII 게이트 — mcp.rs:1350-1364 순서 그대로.

    결정이 났으면 (결정, 노트, False) — refused/blocked. 아니면 (None, 게이트를 지난
    노트, supersedes 여부)를 넘겨 중복 문으로 본다."""
    match parse_judge(request.arguments):
        case Err(reason):
            return (Decision(DECISION_REFUSED, detail=f"-32602 {reason}"), note, False)
        case Ok(judge):
            pass
    claims_owner = note.front.author == "owner" or judge == "owner"
    if claims_owner and not request.is_owner:
        return (Decision(DECISION_REFUSED, detail="-32602 owner door token"), note, False)

    match parse_supersedes(request.arguments):
        case Err(reason):
            return (Decision(DECISION_REFUSED, detail=f"-32602 {reason}"), note, False)
        case Ok(supersedes):
            pass

    if request.pii_scanner is None:
        return (None, note, bool(supersedes))
    match _pii.apply_pii_gate(request.pii_scanner, note):
        case Err(reason):
            # "PII gate blocked by rule '…' (…): …" — 규칙 이름은 detail 에.
            detail = reason
            if match := _PII_BLOCK_RE.search(reason):
                detail = match.group(1)
            return (Decision(DECISION_BLOCKED, detail=detail), note, False)
        case Ok(gated):
            pass
    return (None, gated, bool(supersedes))


def _dedup_decision(request: ShadowRequest, note: RememberNote, exclude: frozenset[str]) -> Decision:
    """중복 문 판정 — check_duplicate + dedup_gate, 임베딩 갈래 미확인은 사유에 남긴다."""
    if request.nearest_document is None:
        # 문이 프로브를 싣지 않은 것 — 본 갈래를 못 본 채 저장을 내고 사유에 남긴다.
        return Decision(DECISION_STORED, detail="embedding-unchecked: probe not wired")
    match _dedup.check_duplicate(
        note=note,
        vault=_dedup.VaultView(
            list_notes=request.list_notes,
            read_note=request.read_note,
            split_frontmatter=request.split_frontmatter,
            vault_dir=request.vault_dir,
        ),
        nearest_document=request.nearest_document,
        exclude_paths=exclude,
    ):
        case Err(reason):
            # 파일 갈래에서 아무것도 안 걸리고 임베딩 갈래를 못 본 것(DSN 부재·임베딩
            # 서버 불응). 결정을 못 내리는 것보다 「저장(임베딩 미확인)」을 내고 사유에
            # 남긴다 — 엔진이 그 갈래로 걸러뒀다면 어긋남 사유가 (다) 를 가리킨다.
            return Decision(DECISION_STORED, detail=f"embedding-unchecked: {reason}")
        case Ok(found):
            pass
    outcome, match_ = _dedup.dedup_gate(request.is_owner, note, found)
    if outcome == DECISION_STORED:
        return Decision(DECISION_STORED)
    assert match_ is not None
    return Decision(outcome, existing_path=match_.source_path, detail=match_.branch)


def _python_decision(
    request: ShadowRequest, note: RememberNote, exclude: frozenset[str]
) -> tuple[Decision, RememberNote]:
    """파이썬 쓰기 경로의 결정 — 게이트(자격·모양·PII)를 지나 중복 문까지.

    돌려주는 RememberNote 는 게이트를 지난 노트 — 칸 대조와 중복 문 모두 엔진처럼 가린
    뒤의 노트로 한다(엔진이 dedup 에 싣는 것도 게이트 뒤 노트다, mcp.rs:1364-1372)."""
    early, note, supersedes = _gate_decision(request, note)
    if early is not None:
        return (early, note)
    if supersedes:
        # 교정은 언제나 새 노트로 떨어진다(중복 문을 안 탄다 — mcp.rs:1367). owner 가
        # 아닌 호출이 owner 노트를 교정 대상으로 명명하면 엔진은 -32602 로 거절하는데,
        # 그 판정(owner_authored 조회)은 이식하지 않았다 — 사유가 필요하면 여기서 다름으로 뜬다.
        return (Decision(DECISION_STORED), note)
    return (_dedup_decision(request, note, exclude), note)


def _engine_new_note_exclude(engine_decision: Decision, extracted: Extracted) -> frozenset[str]:
    """엔진이 방금 쓴 새 노트 하나를 후보에서 빼는 집합 — 그림자만의 것.

    그림자는 응답 뒤에 도니 그 노트가 이미 볼트에 있다. 빼지 않으면 파이썬이 자기 자신과
    중복으로 걸러 거짓 어긋남이 난다. 걸러진 답에서 추출된 경로는 기존 노트이니(새 노트가
    없는 것) 빼면 안 된다 — 저장·대체에서만 뺀다."""
    if engine_decision.outcome not in (DECISION_STORED, DECISION_SUPERSEDED):
        return frozenset()
    if not extracted.note_id:
        return frozenset()
    return frozenset({f"/vault/wiki/{extracted.note_id}.md"})


def _read_inputs(
    request: ShadowRequest, extracted: Extracted, omb_session_id: str | None
) -> Either[tuple[dict[str, Any], str], ShadowEvent]:
    """칸 대조의 읽기 단계 — 볼트에서 엔진 노트를 읽어 (머리말 맵, 본문)으로.

    어느 단계든 실패는 ShadowEvent(status=error) 값으로 돌아온다."""
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
    return Ok((engine_front, engine_note_body))


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


def _pii_reason(
    scanner: _pii.PiiScanner | None,
    engine_view: dict[str, Any],
    differing: dict[str, tuple[str, str]],
) -> str | None:
    """칸 차이를 PII 가림으로 설명할 수 있으면 그 규칙들을, 게이트가 없으면 그 사실을 뜬다.

    엔진과 그림자가 같은 규칙으로 돌면 어긋남이 남지 않는다(ok). 여기 도달한 차이는
    가림 입력(규칙 파일)이 갈렸거나 게이트가 빠진 것 — 둘 다 사유 한 줄로 (가)/(나) 를
    가르는 재료다. 차이 나는 칸의 양쪽 값을 스캔해 적중한 규칙 이름을 모은다."""
    if scanner is None:
        if _PII_FLAG_TAG in engine_view["tags"]:
            return "pii-gate-missing"
        return None
    rules: list[str] = []
    for engine_value, py_value in differing.values():
        scan = scanner.scan(f"{engine_value}\n{py_value}")
        names = [m.rule for m in scan.flags]
        if scan.block is not None:
            names.append(scan.block.rule)
        for name in names:
            if name not in rules:
                rules.append(name)
    if rules:
        return f"pii-rules {','.join(rules)}"
    if _PII_FLAG_TAG in engine_view["tags"]:
        return "pii-flag-drift"
    return None


@dataclass(frozen=True)
class _CompareCtx:
    """칸 대조의 부가 맥락 — 사건 한 줄에 얹을 결정들과 omb 세션·스캐너."""

    omb_session_id: str | None
    scanner: _pii.PiiScanner | None
    python_decision: Decision
    engine_decision: Decision


def _differing_values(field: str, engine_view: dict[str, Any], py_view: dict[str, Any]) -> tuple[str, str]:
    """불일치 칸의 양쪽 값을 스캔 가능한 문자열 한 쌍으로 — 목록 칸은 공백으로 이은다."""
    engine_value, py_value = engine_view[field], py_view[field]
    if field in ("tags", "tools", "concepts", "sources"):
        return (" ".join(engine_value), " ".join(py_value))
    if field == "claims":
        return (
            " ".join(f"{c['subject']} {c['predicate']} {c['value']}" for c in engine_value),
            " ".join(f"{c['subject']} {c['predicate']} {c['value']}" for c in py_value),
        )
    return (str(engine_value or ""), str(py_value or ""))


def _compare(
    extracted: Extracted,
    engine_view: dict[str, Any],
    py_view: dict[str, Any],
    bodies: tuple[str, str],
    ctx: _CompareCtx,
) -> ShadowEvent:
    """칸별 대조 — 불일치한 칸 이름만 모아 사건 한 줄로. 어긋남마다 사유를 붙인다."""
    engine_note_body, py_body = bodies
    differing: dict[str, tuple[str, str]] = {}
    mismatches: list[str] = []
    for field in COMPARED_FIELDS:
        if field == "body":
            if engine_note_body.strip() == py_body.strip():
                continue
            differing[field] = (engine_note_body.strip(), py_body.strip())
        elif engine_view[field] != py_view[field]:
            differing[field] = _differing_values(field, engine_view, py_view)
        else:
            continue
        mismatches.append(field)
    reason = None
    if mismatches:
        clauses = [f"fields {','.join(mismatches)}"]
        if pii := _pii_reason(ctx.scanner, engine_view, differing):
            clauses.append(pii)
        reason = "; ".join(clauses)
    return ShadowEvent(
        "mismatch" if mismatches else "ok",
        source_path=extracted.source_path,
        omb_session_id=ctx.omb_session_id,
        fields=tuple(mismatches),
        reason=reason,
        duplicate=extracted.duplicate,
        decision=ctx.python_decision.describe(),
        engine_decision=ctx.engine_decision.describe(),
        branch=ctx.python_decision.detail or None,
    )


def _same_outcome(engine: Decision, python: Decision) -> bool:
    """결정이 같은지 — 걸러진 요청은 기존 노트 경로까지 같아야 같다."""
    if engine.outcome != python.outcome:
        return False
    if engine.outcome in (DECISION_SKIPPED, DECISION_SUPERSEDED):
        return engine.existing_path == python.existing_path
    return True


def _ok_event(
    extracted: Extracted,
    omb_session_id: str | None,
    python_decision: Decision,
    engine_decision: Decision,
) -> ShadowEvent:
    """결정이 같고 칸 대조가 필요 없는 경우의 ok 한 줄 — 차단·거절·걸러짐."""
    return ShadowEvent(
        "ok",
        source_path=extracted.source_path or None,
        omb_session_id=omb_session_id,
        duplicate=extracted.duplicate,
        decision=python_decision.describe(),
        engine_decision=engine_decision.describe(),
        branch=python_decision.detail or None,
    )


def _early_event(
    extracted: Extracted,
    omb_session_id: str | None,
    python_decision: Decision,
    engine_decision: Decision,
) -> ShadowEvent | None:
    """결정 대조의 끝 — 어긋남 한 줄, 결정만 같으면 끝난 ok 한 줄, 아니면 None(칸 대조로)."""
    if not _same_outcome(engine_decision, python_decision):
        return ShadowEvent(
            "mismatch",
            source_path=extracted.source_path or None,
            omb_session_id=omb_session_id,
            reason=(f"decision engine={engine_decision.describe()} python={python_decision.describe()}"),
            duplicate=extracted.duplicate,
            decision=python_decision.describe(),
            engine_decision=engine_decision.describe(),
            branch=python_decision.detail or None,
        )
    if engine_decision.outcome in (DECISION_BLOCKED, DECISION_REFUSED, DECISION_SKIPPED):
        # 차단·거절은 양쪽 다 거절이면 끝(쓴 노트가 없다). 걸러진 것은 결정만 같으면 끝 —
        # 기존 노트와 칸을 맞추는 일은 없다(요청을 쓴 것이 아니니까).
        return _ok_event(extracted, omb_session_id, python_decision, engine_decision)
    return None


def _open(request: ShadowRequest) -> Either[tuple[RememberNote, Extracted, Decision], ShadowEvent]:
    """그림자의 입구 — 요청 파싱과 엔진 답 뽑기. 어느 쪽이든 실패는 error 사건."""
    omb_session_id = _omb_session_id(request.arguments)
    match parse_remember_note(request.arguments):
        case Err(reason):
            return Err(ShadowEvent("error", omb_session_id=omb_session_id, reason=f"python parse: {reason}"))
        case Ok(note):
            pass
    match _extract(request.route, request.engine_status, request.engine_body):
        case Err(reason):
            return Err(ShadowEvent("error", omb_session_id=omb_session_id, reason=reason))
        case Ok((extracted, engine_decision)):
            pass
    return Ok((note, extracted, engine_decision))


def _field_compare(
    request: ShadowRequest,
    extracted: Extracted,
    gated_note: RememberNote,
    ctx: _CompareCtx,
) -> ShadowEvent:
    """저장·대체의 칸 대조 — 읽기·렌더 각 단계의 실패는 error, 마지막에 칸별 대조."""
    match _read_inputs(request, extracted, ctx.omb_session_id):
        case Err(event):
            return event
        case Ok((engine_front, engine_note_body)):
            pass
    match _render_python(extracted, gated_note, engine_front, request.split_frontmatter, ctx.omb_session_id):
        case Err(event):
            return event
        case Ok((engine_view, py_view, py_body)):
            pass
    return _compare(
        extracted,
        engine_view,
        py_view,
        (engine_note_body, py_body),
        ctx,
    )


def run_shadow(request: ShadowRequest) -> ShadowEvent:
    """파이썬 쓰기 경로를 「쓰지 않고」 태워 엔진과 결정·칸을 대조 — 사건 한 줄의 재료.

    읽기 쪽 실패(vault 파일 없음 등)는 값으로 돌아오고, 예외는 문 핸들러 경계에서 접는다."""
    omb_session_id = _omb_session_id(request.arguments)
    match _open(request):
        case Err(event):
            return event
        case Ok((note, extracted, engine_decision)):
            pass
    python_decision, gated_note = _python_decision(
        request, note, _engine_new_note_exclude(engine_decision, extracted)
    )
    if event := _early_event(extracted, omb_session_id, python_decision, engine_decision):
        return event
    return _field_compare(
        request,
        extracted,
        gated_note,
        _CompareCtx(
            omb_session_id=omb_session_id,
            scanner=request.pii_scanner,
            python_decision=python_decision,
            engine_decision=engine_decision,
        ),
    )


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
    if event.decision is not None:
        payload["decision"] = event.decision
    if event.engine_decision is not None:
        payload["engine_decision"] = event.engine_decision
    if event.branch is not None:
        payload["branch"] = event.branch
    return payload
