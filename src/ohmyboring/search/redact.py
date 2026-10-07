"""비밀 가림 — query_log 적기 전 누수 경계 한 곳.

drudge/src/redact.rs 의 SECRET_PATTERN 을 대안 하나 빠짐없이 그대로 옮긴다
(`(?i:...)` 스코프드 플래그 그룹 포함 — 파이썬 3.11+). 컴파일은 import 때 한 번.
query_log 은 백업과 /query-log 으로 나가므로, 사용자가 질문·답에 붙여 넣은 토큰은
저장 전 여기서 가려야 엔진(redact.rs)의 가림 보장 밖으로 새지 않는다.
"""

from __future__ import annotations

import re
from typing import Any

#: drudge/src/redact.rs:16 SECRET_PATTERN 과 대안·길이·대소문자 규약이 같다.
SECRET_PATTERN = (
    r"(?:xox[baprs]-[0-9A-Za-z-]{10,})"
    r"|(?:xapp-[0-9A-Za-z-]{10,})"
    r"|(?:sk-(?:ant-)?[A-Za-z0-9_-]{20,})"
    r"|(?:AKIA[0-9A-Z]{16})"
    r"|(?:gh[pousr]_[A-Za-z0-9]{30,})"
    r"|(?:github_pat_[A-Za-z0-9_]{30,})"
    r"|(?:AIza[0-9A-Za-z_-]{35})"
    r"|(?:AQo[A-Za-z0-9/+=]{20,})"
    r"|(?:eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
    r"|(?:-----BEGIN [A-Z ]*PRIVATE KEY-----)"
    r'|(?:(?i:api[_-]?key|secret|token|password|passwd|bearer)["\' ]*[:=]["\' ]*[A-Za-z0-9._/+-]{12,})'
)

_SECRET_RE = re.compile(SECRET_PATTERN)


def redact(text: str) -> str:
    """아는 토큰 형식을 전부 ‹REDACTED› 로 바꾼 문자열 — 순수."""
    return _SECRET_RE.sub("‹REDACTED›", text)


def redact_json_value(value: Any) -> Any:
    """store.rs redact_json_value — 문자열 값만 가리고 재귀로 날린다(키는 손 안 댄다)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_json_value(item) for key, item in value.items()}
    return value
