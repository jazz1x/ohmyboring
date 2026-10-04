"""OpenAI 호환 /embeddings 어댑터 — 로컬 bge-m3 임베딩 한 벌(urllib, 표준 라이브러리만).

모델·주소는 ohmyboring.config 정본(embed_model()·llm_base_url())을 쓰고 코드에 박지 않는다.
실패는 예외 대신 값이다 — `Unreachable`(연결·타임아웃), `Refused`(4xx/5xx), `Malformed`(200 인데
핸드쉐이크 못 함). 원격 모델은 금지(wiki-2427) — 주소가 로컬인 것은 호출자 정책이다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from ohmyboring import config as omb_env
from ohmyboring.result import Either, Err, Ok

_TIMEOUT = 60.0


@dataclass(frozen=True)
class Unreachable:
    """연결·타임아웃 — 임베딩 서버에 닿지 못했다."""

    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True)
class Refused:
    """서버가 4xx/5xx 로 답했다 — 응답 본문을 담는다."""

    status: int
    reason: str
    body: str

    def __str__(self) -> str:
        return f"HTTP Error {self.status}: {self.reason}"


@dataclass(frozen=True)
class Malformed:
    """200 인데 핸드쉐이크가 아니다 — JSON 이 아니거나 벡터 모양이 다름."""

    detail: str

    def __str__(self) -> str:
        return self.detail


EmbedFailure = Unreachable | Refused | Malformed

#: 테스트가 주입하는 전송 — (request, timeout) 을 받아 컨텍스트 매니저 응답을 돌려준다.
Opener = Callable[..., object]


def _default_opener(req: object, timeout: float) -> object:
    return urllib.request.urlopen(req, timeout=timeout)


def _parse_vector(data: object) -> Either[list[float], EmbedFailure]:
    if not isinstance(data, dict):
        return Err(Malformed("response is not a JSON object"))
    rows = data.get("data")
    if not isinstance(rows, list) or not rows:
        return Err(Malformed("response has no data[0]"))
    embedding = rows[0].get("embedding") if isinstance(rows[0], dict) else None
    if not isinstance(embedding, list) or not all(isinstance(v, int | float) for v in embedding):
        return Err(Malformed("data[0].embedding is not a numeric vector"))
    return Ok([float(v) for v in embedding])


def embed(
    text: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    opener: Opener | None = None,
) -> Either[list[float], EmbedFailure]:
    """한 텍스트를 임베딩해 Ok(벡터) 또는 Err(실패 값)를 돌려준다."""
    url = (base_url if base_url is not None else omb_env.llm_base_url()).rstrip("/") + "/embeddings"
    key = api_key if api_key is not None else omb_env.llm_api_key()
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    body = json.dumps({"model": model or omb_env.embed_model(), "input": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    send = opener or _default_opener
    try:
        with send(req, _TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001 — 실패 값을 만드는 중이면 어떤 이유든 본문은 부차적
            detail = ""
        return Err(Refused(e.code, e.reason, detail))
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return Err(Unreachable(str(e)))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return Err(Malformed(f"response is not JSON: {e}"))
    return _parse_vector(data)
