"""판정 쓰기 요청의 경계 — POST /consumption · POST /handover · MCP verdict 를 한 번 좁힌다.

엔진 serve.rs 의 ConsumptionReq·HandoverReq(serde 모양)와 validate_consumption_req·
validate_handover_req(검사 순서와 문구)를 그대로 옮긴 순수 값 — 여기를 지난 값은 아래에서 다시
검사하지 않는다. 거절은 `Rejected` 값(HTTP 는 status, MCP 는 -32602)이고 예외가 아니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ohmyboring.remember.parse import parse_author
from ohmyboring.result import Either, Err, Ok

#: serve.rs CONSUMPTION_MAX_PATHS.
MAX_PATHS = 200

OWNER_TOKEN_NEEDED = "owner (as author or judge) needs the owner door token in x-boring-owner-token"


@dataclass(frozen=True)
class Rejected:
    """요청 거절 — 엔진 400(검증·자격)·422(serde 칸 종류)와 같은 status, MCP 에서는 -32602."""

    message: str
    status: int = 400


UNPROCESSABLE = Rejected("unprocessable entity", 422)


class Verdict(Enum):
    USED = "used"
    CONTESTED = "contested"


@dataclass(frozen=True)
class Listed:
    """경로를 직접 든 소비 — used·contested 목록 그대로."""

    used: tuple[str, ...]
    contested: tuple[str, ...]


@dataclass(frozen=True)
class Handed:
    """판정만 온 소비 — 그 세션에 건넨 문서 전부에 한 판정을 건다."""

    verdict: Verdict


Basis = Listed | Handed


@dataclass(frozen=True)
class Consumption:
    session_id: str
    observed_at: str
    basis: Basis
    supersedes: tuple[tuple[str, str], ...]
    judge: str | None


@dataclass(frozen=True)
class Handover:
    session_id: str
    observed_at: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class VerdictCall:
    session_id: str
    verdict: Verdict


_RFC3339 = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:[Zz]|[+-](\d{2}):(\d{2}))"
)
_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def is_rfc3339(raw: str) -> bool:
    """chrono::DateTime::parse_from_rfc3339 의 받아들임 — 모양과 칸 범위(초 60 은 윤초로 허용)."""
    m = _RFC3339.fullmatch(raw)
    if m is None:
        return False
    year, month, day, hour, minute, second = (int(g) for g in m.groups()[:6])
    leap_day_ok = month != 2 or day != 29 or (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
    offset_ok = all(int(g) < limit for g, limit in zip(m.groups()[6:], (24, 60)) if g is not None)
    in_range = (
        1 <= month <= 12 and 1 <= day <= _DAYS[month - 1] and hour < 24 and minute < 60 and second <= 60
    )
    return in_range and leap_day_ok and offset_ok


def rust_debug(text: str) -> str:
    """Rust 의 `{:?}`(str) — 큰따옴표로 감싸고 `"`·`\\`·제어 문자만 이스케이프한다."""
    named = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\0": "\\0"}
    body = "".join(named.get(ch) or (ch if ch.isprintable() else f"\\u{{{ord(ch):x}}}") for ch in text)
    return f'"{body}"'


def _text(data: dict[str, Any], key: str) -> Either[str, Rejected]:
    raw = data.get(key)
    return Ok(raw) if isinstance(raw, str) else Err(UNPROCESSABLE)


def _texts(data: dict[str, Any], key: str) -> Either[tuple[str, ...], Rejected]:
    raw = data.get(key, [])
    shaped = isinstance(raw, list) and all(isinstance(item, str) for item in raw)
    return Ok(tuple(raw)) if shaped else Err(UNPROCESSABLE)


def _pairs(data: dict[str, Any], key: str) -> Either[tuple[tuple[str, str], ...], Rejected]:
    raw = data.get(key, [])
    shaped = isinstance(raw, list) and all(
        isinstance(pair, list) and len(pair) == 2 and all(isinstance(s, str) for s in pair) for pair in raw
    )
    return Ok(tuple((pair[0], pair[1]) for pair in raw)) if shaped else Err(UNPROCESSABLE)


def _optional_text(data: dict[str, Any], key: str) -> Either[str | None, Rejected]:
    raw = data.get(key)
    return Ok(raw) if raw is None or isinstance(raw, str) else Err(UNPROCESSABLE)


def _verdict(raw: str) -> Either[Verdict, Rejected]:
    match raw:
        case "used":
            return Ok(Verdict.USED)
        case "contested":
            return Ok(Verdict.CONTESTED)
        case _:
            return Err(Rejected(f'verdict must be "used" or "contested", got {rust_debug(raw)}'))


def _basis(verdict: str | None, used: tuple[str, ...], contested: tuple[str, ...]) -> Either[Basis, Rejected]:
    if verdict is None:
        return Ok(Listed(used, contested))
    match _verdict(verdict.strip()):
        case Err(rejected):
            return Err(rejected)
        case Ok(parsed):
            pass
    if used or contested:
        return Err(Rejected("verdict applies to what was handed; do not also list paths"))
    return Ok(Handed(parsed))


def _judge(raw: str | None) -> Either[str | None, Rejected]:
    """엔진 Author::as_judge — unknown 은 아무도 안 이름 댄 NULL, 나머지는 어휘 그대로."""
    if raw is None:
        return Ok(None)
    match parse_author(raw):
        case Err(_):
            return Err(
                Rejected(
                    f"judge: author must be owner | inferred | unknown | agent:<name>, got {rust_debug(raw)}"
                )
            )
        case Ok("unknown"):
            return Ok(None)
        case Ok(known):
            return Ok(known)


def _oversize(lists: tuple[tuple[str, int], ...]) -> Rejected | None:
    for name, size in lists:
        if size > MAX_PATHS:
            return Rejected(f"{name}: at most {MAX_PATHS} paths, got {size}")
    return None


def _instant_rejection(session_id: str, observed_at: str) -> Rejected | None:
    if not session_id.strip():
        return Rejected("session_id must not be empty")
    if not is_rfc3339(observed_at):
        return Rejected(f"observed_at must be RFC 3339, got {rust_debug(observed_at)}")
    return None


def parse_consumption(data: dict[str, Any]) -> Either[Consumption, Rejected]:
    """POST /consumption 본문 — serde 칸 종류(422) → session_id → observed_at → verdict → judge → 상한 순."""
    shaped = (
        _text(data, "session_id"),
        _text(data, "observed_at"),
        _texts(data, "used"),
        _texts(data, "contested"),
        _pairs(data, "supersedes"),
        _optional_text(data, "verdict"),
        _optional_text(data, "judge"),
    )
    match shaped:
        case (Ok(session_id), Ok(observed_at), Ok(used), Ok(contested), Ok(supersedes), Ok(verdict), Ok(raw)):
            pass
        case _:
            return Err(UNPROCESSABLE)
    if (rejected := _instant_rejection(session_id, observed_at)) is not None:
        return Err(rejected)
    match _basis(verdict, used, contested):
        case Err(rejected):
            return Err(rejected)
        case Ok(basis):
            pass
    match _judge(raw):
        case Err(rejected):
            return Err(rejected)
        case Ok(judge):
            pass
    sizes = (("used", len(used)), ("contested", len(contested)), ("supersedes", len(supersedes)))
    if (oversize := _oversize(sizes)) is not None:
        return Err(oversize)
    return Ok(Consumption(session_id, observed_at, basis, supersedes, judge))


def parse_handover(data: dict[str, Any]) -> Either[Handover, Rejected]:
    """POST /handover 본문 — serde 칸 종류(422) → session_id → observed_at → paths 상한 순."""
    match (_text(data, "session_id"), _text(data, "observed_at"), _texts(data, "paths")):
        case (Ok(session_id), Ok(observed_at), Ok(paths)):
            pass
        case _:
            return Err(UNPROCESSABLE)
    if (rejected := _instant_rejection(session_id, observed_at)) is not None:
        return Err(rejected)
    if (oversize := _oversize((("paths", len(paths)),))) is not None:
        return Err(oversize)
    return Ok(Handover(session_id, observed_at, paths))


def parse_verdict_call(args: dict[str, Any]) -> Either[VerdictCall, Rejected]:
    """MCP verdict 인자 — mcp.rs mcp_verdict: 문자열 아닌 칸은 없는 칸, 트림, 빈 칸은 missing argument."""

    def text(key: str) -> str:
        raw = args.get(key)
        return raw.strip() if isinstance(raw, str) else ""

    if not (session_id := text("session_id")):
        return Err(Rejected("missing argument: session_id"))
    if not (verdict := text("verdict")):
        return Err(Rejected("missing argument: verdict"))
    match _verdict(verdict):
        case Err(rejected):
            return Err(rejected)
        case Ok(parsed):
            return Ok(VerdictCall(session_id, parsed))
