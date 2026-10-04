"""빈자리 보고의 경계 — HTTP 본문과 MCP 인자가 같은 문으로 들어와 같은 값이 된다."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from ohmyboring.result import Either, Err, Ok


class Kind(enum.Enum):
    MISSING = "missing"
    STALE = "stale"
    BROKEN = "broken"


@dataclass(frozen=True)
class Rejected:
    message: str


@dataclass(frozen=True)
class GapArgs:
    session_id: str
    query: str
    kind: Kind
    handed: tuple[str, ...]


TOOL = {
    "name": "gap",
    "description": (
        "Report a gap in memory. Call this when recall, claims or decisions returned nothing, "
        "a stale value, or failed for the question you were asking (kind: missing, stale, broken). "
        "Send the question you were looking for and the note source_paths you were handed "
        "(required for stale: say which notes were out of date). The door records the report "
        "and never judges it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Your session id."},
            "query": {"type": "string", "description": "The question memory could not answer."},
            "kind": {"type": "string", "enum": [k.value for k in Kind]},
            "handed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "source_path of the notes you were handed (required for stale).",
            },
        },
        "required": ["session_id", "query", "kind"],
    },
}


def _text(arguments: dict, key: str) -> Either[str, Rejected]:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        return Err(Rejected(f"{key} is required"))
    return Ok(value.strip())


def _kind(arguments: dict) -> Either[Kind, Rejected]:
    try:
        return Ok(Kind(arguments.get("kind")))
    except ValueError:
        return Err(Rejected("kind must be one of missing, stale, broken"))


def _handed(arguments: dict) -> Either[tuple[str, ...], Rejected]:
    value = arguments.get("handed", [])
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        return Err(Rejected("handed must be a list of strings"))
    return Ok(tuple(dict.fromkeys(value)))


def parse(arguments: dict) -> Either[GapArgs, Rejected]:
    match _text(arguments, "session_id"):
        case Err(rejected):
            return Err(rejected)
        case Ok(session_id):
            pass
    match _text(arguments, "query"):
        case Err(rejected):
            return Err(rejected)
        case Ok(query):
            pass
    match _kind(arguments):
        case Err(rejected):
            return Err(rejected)
        case Ok(kind):
            pass
    match _handed(arguments):
        case Err(rejected):
            return Err(rejected)
        case Ok(handed):
            pass
    if kind is Kind.STALE and not handed:
        return Err(Rejected("stale gap needs handed notes"))
    return Ok(GapArgs(session_id, query, kind, handed))
