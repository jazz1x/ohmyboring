"""브리핑 stdout 을 올릴 수 있는 payload 로 — 할 말 없음과 생성 실패를 가른다."""

from __future__ import annotations

import json
from typing import Any

from ohmyboring.briefing.weekly_render import EMPTY_MESSAGE
from ohmyboring.result import Err, Ok
from ohmyboring.weekly.report import one_line
from ohmyboring.weekly.state import GenerationFailed, NothingToSay


def build_payload(stdout: str) -> Ok[dict[str, Any]] | Ok[None] | Err[str]:
    """The weekly's blocks-mode stdout as a postable payload.

    Ok(None) means there is nothing to say this week — the briefing prints its empty message as
    plain text even in blocks mode. Any other non-JSON output is the briefing's own failure
    line (a dead engine, an unparseable response), returned as Err so the caller can quote it
    on stderr and let the scheduler DM the owner.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        if EMPTY_MESSAGE in stdout:
            return Ok(None)
        return Err(one_line(stdout))
    if not isinstance(payload, dict) or not isinstance(payload.get("blocks"), list):
        return Err(f"blocks 페이로드 아님: {one_line(stdout)[:120]}")
    return Ok(payload)


def to_payload(state):
    match build_payload(state["stdout"]):
        case Ok(None):
            return {"outcome": NothingToSay()}
        case Ok(payload):
            return {"payload": payload}
        case Err(reason):
            return {"outcome": GenerationFailed(reason)}
