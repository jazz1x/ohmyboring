"""remember 그림자 — 같은 요청을 파이썬 쓰기 경로에 「쓰지 않고」 태워 「엔진이 한 결정과
같은 결정을 했을지」를 사건 한 줄로 보여 준다 (E3a-2, E3b).

문이 remember 를 엔진으로 바이트 그대로 넘기고 응답까지 본 뒤에 이 모듈을 돌린다 — 그림자는
응답에 영향이 없어야 한다. 쓰기 0: 볼트·DB·그래프에 손 대는 일 없이 사건(remember_shadow)
한 줄의 재료만 만든다(기록은 문 핸들러가 adapters/events 에). 본문 원문은 사건에 싣지
않는다(비밀 경계 — 칸 이름·경로·규칙 이름·사유만 싣는다).

대조는 세 겹이다:
1. 결정 대조 — 파이썬이 같은 결정을 낼지: PII 게이트(pii.py, block 이면 거절)를 지나
   중복 문(dedup.py)의 갈래마다(same_session·probable_session·exact_title·embedding,
   대체는 점수 판정) 걸러서 stored | superseded | skipped | blocked | refused 를 내고
   엔진 응답의 결정과 맞춘다. 거절은 owner 자격 두 갈래다 — owner 를 자처하는 호출이
   토큰 없이 온 것, 그리고 (E3b) 오너가 쓴 노트를 오너 아닌 호출이 대체하려 한 것
   (owner.rs:64-80 refused_supersedes, 엔진 사건 owner_supersede_refused 와 한 쌍).
   걸러진 요청은 결정만 비교한다 — 기존 노트와 칸별 대조는 안 한다(요청을 쓴 것이
   아니니 칸을 맞출 이유가 없다. E3a-1 은 여기서 어긋남 하나를 거짓으로 냈고, 그 사례가
   걸러둘 결정만 비교한다는 단언으로 못 박혀 있다).
2. 칸 대조(E3a-1) — stored/superseded 에서만: 엔진이 실제로 쓴 노트를 볼트(ro)에서 읽어
   파이썬 렌더와 칸별로 맞춘다. id·date 는 엔진 것을 그대로 받아 렌더에 넣고 relates_to
   는 그래프 투영이 다시 쓰므로 제외.
3. 그래프 대조(E3b) — stored/superseded 에서만, 읽기 전용 세션으로 실제 그래프를 읽어
   「그 노트가 그래프와 사실에 남긴 것」까지 목표(graph.py)와 맞춘다:
   · 간선 집합 — uses·about·claims·is_a·said·tagged(+in_project·claim_of_project)
     ·supersedes. 어긋남은 빠지거나 다른 간선이니 사유는 (가) python 결함 쪽이다.
   · claim 봉인 — 새 노트가 쓴 행은 산다. 대체에서 옛 노트의 행은 새 노트가 다시 말한
     (subject, predicate) 슬롯만 닫힌다(부분 닫기). 엔진이 통째로 닫으면(store.rs:2898
     seal_superseded_claims) 그 어긋남은 사유 (나) 「의도한 차이 — 부분 닫기」
     (판정 wiki-2855)다. 모델 호출 0 — 그래프 조회는 읽기 전용 DB 세션뿐이다.

어긋남마다 사유를 남겨, 사걸만 보고 (가) 파이썬 결함 (나) 엔진이 틀렸거나 의도한 차이
(다) 모름 셋 중 어디인지 가른다. 사유 어휘(한 줄, grep 가능):
  decision engine=<결정> python=<결정>   — 결정 불일치(경로 포함)
  fields <칸,…>                        — 칸 불일치
  pii-rules <규칙,…>                   — 칸 차이를 설명하는 PII 규칙(가림 입력이 갈린 것)
  pii-gate-missing                     — 그림자에 규칙 파일이 없는데 엔진 노트에 pii-flag
  edges missing=<부류:n,…> extra=<부류:n,…> — 간선 집합 어긋남 (가)
  seal engine-only=<n> (나 intended-diff partial-close) — 대체 때 엔진이 목표보다 더 닫음
  seal python-only=<n> (가)            — 목표는 닫았는데 실제로 산 행
  graph-unchecked (다): <사유>         — 그래프 조회 자체를 못 함(DSN 부재 등)

사건에는 어긋남과 별개로 소요 시간이 항상 찍힌다 — elapsed_total_s·elapsed_embedding_s
(프로브)·elapsed_db_s(그래프 조회)를 나눠 적는다(2026-10-02 문 수정 뒤 첫 운영 건: 응답
0.67s, 그림자 5.7s — 어디서 쓰는지 미확인이던 것의 근거).

실패는 전부 값(ShadowEvent status=error 의 사유)으로 돌아오고, 예외는 문 핸들러 경계에서
한 번 접는다.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import yaml

from ohmyboring.remember import dedup as _dedup
from ohmyboring.remember import graph as _graph
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
    """사건 한 줄의 재료 — status ok(일치)·mismatch·error, 칸별 불일치 이름(fields).

    edges·seal 은 그래프 대조(E3b)의 요약 — 각각 "ok"·"unchecked"·어긋남 한 줄.
    elapsed_* 는 어긋남과 별개로 항상 찍히는 소요 시간(초, 임베딩·DB 읽기·전체)."""

    status: str
    source_path: str | None = None
    omb_session_id: str | None = None
    fields: tuple[str, ...] = ()
    reason: str | None = None
    duplicate: bool = False
    decision: str | None = None  # 파이썬 결정 describe()
    engine_decision: str | None = None  # 엔진 결정 describe()
    branch: str | None = None  # 파이썬이 찾은 중복 갈래
    edges: str | None = None  # 간선 집합 대조 요약 — ok / missing=… extra=… / unchecked
    seal: str | None = None  # claim 봉인 대조 요약 — ok / engine-only=… (나…) / unchecked
    elapsed_total_s: float = 0.0
    elapsed_embedding_s: float = 0.0
    elapsed_db_s: float = 0.0


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
    read_graph: Callable[[tuple[str, ...]], _graph.GraphSnapshot | None] | None = None
    # 문이 싣는 읽기 전용 그래프 조회(E3b) — 없음/None 반환 = 조회 못 함(사유 (다))


def _owner_written_targets(request: ShadowRequest, targets: list[str]) -> list[str]:
    """대체 대상 중 오너가 쓴 노트의 경로 — owner.rs:64-80 refused_supersedes 의 판정.

    근거는 볼트 머리말 author(엔진이 document.author 에 넣은 것과 같다). 없는 노트는
    엔진이 unknown 으로 세고 간선·거절 모두 안 하는 것이니 판정에서 빼고, wiki 가 아닌
    경로는 읽을 수 없어 뺀다. 비오너 호출에서 이 목록이 비어 있지 않으면 거절이다."""
    refused: list[str] = []
    for target in targets:
        stem = wiki_stem(target)
        if stem is None:
            continue
        text = request.read_note(request.vault_dir, stem)
        if text is None:
            continue
        match _parse_note_text(text, request.split_frontmatter):
            case Err(_):
                continue
            case Ok((front, _)):
                pass
        if front.get("author") == "owner":
            refused.append(target)
    return refused


def _owner_supersede_refusal(request: ShadowRequest, supersedes: list[str]) -> Decision | None:
    """오너가 쓴 노트를 오너 아닌 호출이 대체하려 할 때의 거절 결정 — 아니면 None.

    엔진과 같은 자리(PII 게이트 앞, mcp.rs:1355-1358)에서 같은 사유로 거절한다. 엔진은
    이 때 owner_supersede_refused 사건을 남기고 -32602 로 끝낸다(owner.rs:64-92) —
    사유 문구가 같아야 결정 describe 가 맞는다."""
    if not supersedes or request.is_owner:
        return None
    if _owner_written_targets(request, supersedes):
        return Decision(DECISION_REFUSED, detail="only the owner may supersede an owner-written note")
    return None


def _pii_gate_decision(request: ShadowRequest, note: RememberNote) -> tuple[Decision | None, RememberNote]:
    """PII 게이트 — 규칙 파일이 없으면 비활성으로 그대로 통과, block 이면 거절 결정."""
    if request.pii_scanner is None:
        return (None, note)
    match _pii.apply_pii_gate(request.pii_scanner, note):
        case Err(reason):
            # "PII gate blocked by rule '…' (…): …" — 규칙 이름은 detail 에.
            detail = reason
            if match := _PII_BLOCK_RE.search(reason):
                detail = match.group(1)
            return (Decision(DECISION_BLOCKED, detail=detail), note)
        case Ok(gated):
            return (None, gated)


def _gate_decision(request: ShadowRequest, note: RememberNote) -> tuple[Decision | None, RememberNote, bool]:
    """owner 자격 → supersedes 모양·대상 → PII 게이트 — mcp.rs:1346-1364 순서 그대로.

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

    if refusal := _owner_supersede_refusal(request, supersedes):
        return (refusal, note, False)

    decision, note = _pii_gate_decision(request, note)
    if decision is not None:
        return (decision, note, False)
    return (None, note, bool(supersedes))


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
        # 교정은 언제나 새 노트로 떨어진다(중복 문을 안 탄다 — mcp.rs:1367 needs_dedup).
        # 오너 노트를 오너 아닌 호출이 대상으로 명명한 거절은 이미 게이트(_gate_decision)에서
        # 끝났다 — 여기까지 온 교정은 저장이 맞다.
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
) -> tuple[ShadowEvent, dict[str, Any] | None]:
    """저장·대체의 칸 대조 — 읽기·렌더 각 단계의 실패는 (error, None), 성공은 (사건, 엔진 머리말).

    엔진 머리말은 그래프 대조(E3b)에서 투영 입력으로 다시 쓴다 — 그래프 조회는 실제로 쓰인
    노트를 기준으로 해야 렌더 어긋남과 섞이지 않는다."""
    match _read_inputs(request, extracted, ctx.omb_session_id):
        case Err(event):
            return (event, None)
        case Ok((engine_front, engine_note_body)):
            pass
    match _render_python(extracted, gated_note, engine_front, request.split_frontmatter, ctx.omb_session_id):
        case Err(event):
            return (event, None)
        case Ok((engine_view, py_view, py_body)):
            pass
    return (
        _compare(
            extracted,
            engine_view,
            py_view,
            (engine_note_body, py_body),
            ctx,
        ),
        engine_front,
    )


#: 그래프 대조(E3b)의 시간을 재는 막대기 — run_shadow 가 입구에서 감싼다.
@dataclass
class _Timers:
    embedding: float = 0.0
    db: float = 0.0


def _timed(fn: Callable[..., Any], timers: _Timers, attr: str) -> Callable[..., Any]:
    """호출 한 번마다 걸린 초를 timers 에 누적하는 같은 모양의 껍질."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            setattr(timers, attr, getattr(timers, attr) + time.monotonic() - started)

    return wrapper


def _edge_summary(missing: dict[str, int], extra: dict[str, int]) -> str:
    """간선 어긋남 한 줄 — missing=uses:1,about:1 extra=is_a:2 식. 둘 다 비면 ok."""
    if not missing and not extra:
        return "ok"
    parts = []
    if missing:
        parts.append("missing=" + ",".join(f"{k}:{n}" for k, n in sorted(missing.items())))
    if extra:
        parts.append("extra=" + ",".join(f"{k}:{n}" for k, n in sorted(extra.items())))
    return " ".join(parts)


def _graph_section(
    request: ShadowRequest,
    extracted: Extracted,
    engine_decision: Decision,
    engine_front: dict[str, Any],
    base: ShadowEvent,
) -> ShadowEvent:
    """그래프 대조(E3b) — 저장·대체에서만: 실제 그래프를 읽어 간선 집합과 claim 봉인을
    목표(graph.py)와 맞추고, 어긋남을 사건에 얹는다. 조회 소요 시간은 run_shadow 가 싼
    껍질이 재니 여기서는 안 본다.

    조회 자체를 못 하면 칸에 unchecked 와 (다) 사유만 남기고 넘어간다 — 「모름」은 어긋남이
    아니라 status 를 올리지 않는다. 그림자는 응답 뒤라 실패가 요청에 닿는 일은 없지만, 사건이
    「못 봤다」를 속이지 않고 말해야 한다."""
    match _front_view(engine_front):
        case Err(reason):
            return _attach_graph(
                base,
                edges="unchecked",
                seal="unchecked",
                clause=f"graph-unchecked (다): engine note: {reason}",
            )
        case Ok(engine_view):
            pass
    paths = _graph_paths(request, extracted, engine_decision)
    if request.read_graph is None:
        return _attach_graph(base, edges="unchecked", seal="unchecked", clause=None)
    snapshot = request.read_graph(paths)
    if snapshot is None:
        return _attach_graph(
            base, edges="unchecked", seal="unchecked", clause="graph-unchecked (다): read failed"
        )
    plan = _build_graph_plan(request, extracted, engine_decision, engine_view, snapshot)
    return _graph_verdict(plan, snapshot.claims, extracted.source_path, base)


def _graph_paths(request: ShadowRequest, extracted: Extracted, engine_decision: Decision) -> tuple[str, ...]:
    """조회할 문서 경로 — 새 노트와 교정·대체 대상들(요청 인자 + 엔진 답)."""
    paths = {extracted.source_path}
    match parse_supersedes(request.arguments):
        case Ok(targets):
            paths.update(t for t in targets if t != extracted.source_path)
        case Err(_):
            pass
    if engine_decision.outcome == DECISION_SUPERSEDED and engine_decision.existing_path:
        paths.add(engine_decision.existing_path)
    return tuple(sorted(paths))


@dataclass(frozen=True)
class _GraphPlan:
    """그래프 대조 한 번의 재료 — 목표 간선·실제 간선·소속 노드·대상 경로·다시 말한 슬롯."""

    expected: frozenset[tuple[str, str, str]]
    actual: frozenset[tuple[str, str, str]]
    new_nodes: frozenset[str]
    superseded_paths: frozenset[str]
    slots: frozenset[tuple[str, str]]


def _valid_supersedes_targets(
    request: ShadowRequest,
    extracted: Extracted,
    engine_decision: Decision,
    documents: frozenset[str],
) -> list[str]:
    """교정·대체 대상 중 간선이 실제로 쓰일 경로 — 문서 행이 확인된 것만. 엔진이 unknown 으로
    세고 안 쓰는 것과 같다(store.rs:2866-2876). 요청 인자의 대상 + 엔진 답이 말한 대체."""
    match parse_supersedes(request.arguments):
        case Ok(targets):
            arg_targets = [t for t in targets if t != extracted.source_path]
        case Err(_):
            arg_targets = []
    valid = [t for t in arg_targets if t in documents]
    if engine_decision.outcome == DECISION_SUPERSEDED and engine_decision.existing_path:
        if engine_decision.existing_path not in valid:
            valid.append(engine_decision.existing_path)
    return valid


def _build_graph_plan(
    request: ShadowRequest,
    extracted: Extracted,
    engine_decision: Decision,
    engine_view: dict[str, Any],
    snapshot: _graph.GraphSnapshot,
) -> _GraphPlan:
    """대조 재료 한 벌 — 목표 투영과 실제 그래프, 그리고 이 노트가 책임지는 노드 집합."""
    targets = _valid_supersedes_targets(request, extracted, engine_decision, snapshot.documents)
    expected = _graph.expected_edges(engine_view, extracted.source_path, tuple(targets))
    actual = frozenset(e for e in snapshot.edges if e[1] in _graph.PROJECTION_KINDS)
    # 이 노트가 책임지는 노드 — 새 문서 노드와 새 노트가 말한 claim 노드. 함께 조회된 옛
    # 노트의 잔여 간선(is_a 등)은 이 노트의 것이 아니라 어긋남에 안 센다.
    new_nodes = frozenset({f"doc:{extracted.source_path}"} | {dst for _, k, dst in expected if k == "claims"})
    return _GraphPlan(
        expected=expected,
        actual=actual,
        new_nodes=new_nodes,
        superseded_paths=frozenset(targets),
        slots=_graph.restated_slots(engine_view),
    )


def _edge_divergence(
    expected: frozenset[tuple[str, str, str]],
    actual: frozenset[tuple[str, str, str]],
    new_nodes: frozenset[str],
) -> tuple[dict[str, int], dict[str, int]]:
    """목표와 실제의 차이를 부류별로 — 끝점 하나가 이 노트의 노드인 간선만 센다."""
    missing: dict[str, int] = {}
    extra: dict[str, int] = {}
    for src, kind, dst in expected - actual:
        if src in new_nodes or dst in new_nodes:
            missing[kind] = missing.get(kind, 0) + 1
    for src, kind, dst in actual - expected:
        if src in new_nodes or dst in new_nodes:
            extra[kind] = extra.get(kind, 0) + 1
    return missing, extra


def _graph_clauses(edges: str, verdict: _graph.SealVerdict) -> list[str]:
    """어긋남 사유 절들 — 각각 (가)/(나) 를 겉에 두어 grep 으로 갈래를 가른다."""
    clauses = []
    if edges != "ok":
        clauses.append(f"edges {edges} (가)")
    if verdict.python_only:
        clauses.append(f"seal python-only={verdict.python_only} (가)")
    superseded_only = verdict.engine_only - verdict.engine_only_on_new_note
    if superseded_only > 0:
        # 대상 노트에서만 더 닫힌 행 — 통째 봉인 대 부분 닫기의 어긋남, 판정이 난 의도한 차이.
        clauses.append(f"seal engine-only={superseded_only} (나 intended-diff partial-close)")
    if verdict.engine_only_on_new_note:
        clauses.append(f"seal engine-only-on-new={verdict.engine_only_on_new_note} (나)")
    return clauses


def _graph_verdict(
    plan: _GraphPlan, claims: tuple[_graph.ClaimRow, ...], new_path: str, base: ShadowEvent
) -> ShadowEvent:
    """간선 집합과 봉인을 각각 목표와 맞춘다 — 어긋남이면 사유 절을 얹고 status 를 올린다."""
    missing, extra = _edge_divergence(plan.expected, plan.actual, plan.new_nodes)
    edges = _edge_summary(missing, extra)
    verdict = _graph.compare_seals(
        _graph.expected_seal_states(claims, new_path, plan.slots, plan.superseded_paths), new_path
    )
    seal = _seal_summary(verdict)
    clauses = _graph_clauses(edges, verdict)
    return _attach_graph(
        base, edges=edges, seal=seal, clause="; ".join(clauses) or None, divergence=bool(clauses)
    )


def _seal_summary(verdict: _graph.SealVerdict) -> str:
    """봉인 대조 요약 한 줄 — 어긋남이 없으면 ok."""
    if verdict.engine_only == 0 and verdict.python_only == 0:
        return "ok"
    parts = []
    if verdict.engine_only:
        parts.append(f"engine-only={verdict.engine_only}")
    if verdict.python_only:
        parts.append(f"python-only={verdict.python_only}")
    return " ".join(parts)


def _attach_graph(
    base: ShadowEvent, *, edges: str, seal: str, clause: str | None, divergence: bool = False
) -> ShadowEvent:
    """칸 대조 사건에 그래프 대조 결과를 얹는다 — 사유 절은 덧붙이고, 실제 어긋남이
    있을 때만 status 를 mismatch 로 올린다(「모름」 사유는 status 에 영향 없음)."""
    reason = base.reason
    if clause:
        reason = f"{reason}; {clause}" if reason else clause
    return replace(
        base,
        status="mismatch" if divergence else base.status,
        reason=reason,
        edges=edges,
        seal=seal,
    )


def _run(request: ShadowRequest) -> ShadowEvent:
    """그림자의 몸통 — 입구부터 결정·칸·그래프 대조까지. 실패는 전부 값으로."""
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
    base, engine_front = _field_compare(
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
    if base.status == "error" or engine_front is None:
        return base
    return _graph_section(request, extracted, engine_decision, engine_front, base)


def run_shadow(request: ShadowRequest) -> ShadowEvent:
    """파이썬 쓰기 경로를 「쓰지 않고」 태워 엔진과 결정·칸·그래프를 대조 — 사건 한 줄의 재료.

    읽기 쪽 실패(vault 파일 없음 등)는 값으로 돌아오고, 예외는 문 핸들러 경계에서 접는다.
    어느 경로로 끝나든 소요 시간(전체·임베딩·DB)은 사건에 찍힌다."""
    started = time.monotonic()
    timers = _Timers()
    if request.nearest_document is not None:
        request = replace(request, nearest_document=_timed(request.nearest_document, timers, "embedding"))
    if request.read_graph is not None:
        request = replace(request, read_graph=_timed(request.read_graph, timers, "db"))
    event = _run(request)
    return replace(
        event,
        elapsed_total_s=round(time.monotonic() - started, 3),
        elapsed_embedding_s=round(timers.embedding, 3),
        elapsed_db_s=round(timers.db, 3),
    )


def event_payload(event: ShadowEvent) -> dict[str, Any]:
    """adapters/events 기록용 본문 — 본문 원문은 전부 빠진다(비밀 경계)."""
    payload: dict[str, Any] = {
        "fields": list(event.fields),
        "relates_to": RELATES_TO_EXCLUDED,
        "elapsed_total_s": event.elapsed_total_s,
        "elapsed_embedding_s": event.elapsed_embedding_s,
        "elapsed_db_s": event.elapsed_db_s,
    }
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
    if event.edges is not None:
        payload["edges"] = event.edges
    if event.seal is not None:
        payload["seal"] = event.seal
    return payload
