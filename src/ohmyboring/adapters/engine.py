#!/usr/bin/env python3
"""엔진에 나가는 쪽 한 벌 — ohmyboring 의 drudge 엔진을 부르는 HTTP 클라이언트.

Centralizes retries, timeouts, and JSON parsing so Python adapters (recall,
distillation, schedulers, diagnostics) stop duplicating urllib boilerplate.

I/O 실패는 예외 대신 `EngineFailure` 값으로 돌아온다 — `Unreachable`(연결·타임아웃·
재시도 소진·소켓 끊김), `Refused`(엔진이 4xx/5xx 로 답함), `Malformed`(200 인데 JSON 이
아님), `NotWritable`(/health 가 쓰기 불가를 알림). 모든 공개 메서드는 `Either[값, EngineFailure]` 를 돌려주고 부르는 쪽이
match 한 자리에서 접는다. 소비 표시(`ConsumptionMarks`)는 `Verdict`/`PathMarks`
합 타입이라 둘을 같이 주는 호출 모양 자체가 없다.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ohmyboring import config as omb_env
from ohmyboring.result import Either, Err, Ok, map_ok

OWNER = "owner"
OWNER_TOKEN_HEADER = "X-Boring-Owner-Token"


@dataclass(frozen=True)
class SearchKnobs:
    """`DrudgeClient.search` 의 선택 인자 묶음 — 필드 이름·기본값은 옛 키워드 인자와 같다."""

    max_results: int = 3
    max_tokens: int = 1500
    related: int = 0
    related_heads: int = 2
    claims: int = 0


@dataclass(frozen=True)
class Verdict:
    """`consumption` 표시의 verdict 변형 — 한 세션에 대한 판정 하나가 전부에게 적용된다."""

    verdict: str
    judge: str | None = None


@dataclass(frozen=True)
class PathMarks:
    """`consumption` 표시의 경로 변형 — 쓴/반박한/대체한 경로 목록."""

    used: list[str] | None = None
    contested: list[str] | None = None
    supersedes: list[list[str]] | None = None
    judge: str | None = None


#: `consumption` 의 표시 한 벌 — verdict 하나가 전부에게 적용되거나 경로 목록이 적힌다.
#: 둘을 같이 주는 호출 모양이 없어 옛 묶음의 ValueError 지킴이는 구조적으로 사라진다.
ConsumptionMarks = Verdict | PathMarks


@dataclass(frozen=True)
class NoteProvenance:
    """`DrudgeClient.remember` 의 선택 인자 묶음 — 필드 이름·기본값은 옛 키워드 인자와 같다."""

    tags: list[str] | None = None
    supersedes: list[str] | None = None
    origin: str = "personal"
    repo: str | None = None
    author: str | None = None
    judge: str | None = None


@dataclass(frozen=True)
class Unreachable:
    """연결·타임아웃·재시도 소진 — 엔진에 닿지 못했다."""

    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True)
class Refused:
    """엔진이 4xx/5xx 로 답했다 — 응답 본문을 담는다."""

    status: int
    reason: str
    body: str

    def __str__(self) -> str:
        return f"HTTP Error {self.status}: {self.reason}"


@dataclass(frozen=True)
class Malformed:
    """엔진이 답했으나 해독할 수 없다 — 200 인데 JSON 이 아니거나 UTF-8 이 아님."""

    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True)
class NotWritable:
    """`/health` 가 쓰기 불가를 알렸다 — distillation 은 돌면 안 된다."""

    detail: str

    def __str__(self) -> str:
        return self.detail


#: 엔진 어댑터의 실패 한 벌 — 모든 공개 메서드의 Err 변형.
EngineFailure = Unreachable | Refused | Malformed | NotWritable


#: frozen 이라 여러 호출이 기본값을 같이 써도 안전 — 서명 기본값은 모듈 싱글턴으로 둔다
#: (ruff B008: 인자 기본값에서 함수 호출 금지).
_DEFAULT_SEARCH_KNOBS = SearchKnobs()
_DEFAULT_NOTE_PROVENANCE = NoteProvenance()


def owner_headers(payload: dict[str, Any] | None) -> dict[str, str]:
    """The owner token rides only on a payload that names the owner as author or judge. With
    no token in the env the claim still travels bare, so the engine refuses it out loud (400)
    instead of this client dropping the owner quietly."""
    claims_owner = payload is not None and OWNER in (payload.get("author"), payload.get("judge"))
    token = os.environ.get("BORING_OWNER_TOKEN")
    return {OWNER_TOKEN_HEADER: token} if claims_owner and token else {}


def _error_body(exc: urllib.error.HTTPError) -> str:
    """HTTP 오류 응답의 본문 — 읽을 수 없으면 빈 문자열."""
    try:
        return exc.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _hits_or_empty(data: Any) -> list[dict[str, Any]]:
    return data.get("hits", []) if isinstance(data, dict) else []


class DrudgeClient:
    """Minimal drudge HTTP client. Failures come home as values, left to callers to fold.

    The default base is the door(:7710) — 얼굴이 몇 개든 문은 하나(E4-α): every consumer
    (recall hook, secretary, card, distillation, collectors) names no URL and rides the door,
    which relays the engine's route table byte-for-byte. `base_url` stays for callers that
    must name an end themselves (tests, the parity copy, a caller-named door).
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 5.0,
        retries: int = 1,
    ):
        self.base_url = (base_url or omb_env.door_url()).rstrip("/")
        self.timeout = timeout
        self.retries = retries

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
        base_url: str | None = None,
    ) -> Any:
        url = f"{base_url or self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"content-type": "application/json"} if data is not None else {}
        headers.update(owner_headers(payload))
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
        base_url: str | None = None,
    ) -> Either[Any, EngineFailure]:
        """One JSON request with the client's retry budget — Either 로 답한다.

        5xx·연결·타임아웃은 남은 재시도를 쓰고, 그래도 실패하면 `Refused`/`Unreachable` 로
        돌아온다. 소켓 끊김은 재시도 없이 `Unreachable`, 해독 실패는 `Malformed`. 재시도·타임아웃
        값은 옛 `_retry` 와 같다. `base_url` 을 주면 그 주소로 본다 — 기본은 문(:7710)이고,
        remember 만 history 때문에 문을 다시 명시한다(E3a-1·E4-α — 이제 둘 다 문이라 같은 주소다).
        """
        for attempt in range(self.retries + 1):
            try:
                return Ok(self._request(method, path, payload, timeout, base_url))
            except urllib.error.HTTPError as e:
                if 500 <= e.code < 600 and attempt < self.retries:
                    time.sleep(1 << attempt)
                    continue
                return Err(Refused(e.code, str(e.reason), _error_body(e)))
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < self.retries:
                    time.sleep(1 << attempt)
                    continue
                return Err(Unreachable(str(e)))
            except (OSError, http.client.HTTPException) as e:
                return Err(Unreachable(str(e)))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                return Err(Malformed(str(e)))
        return Err(Unreachable("retry loop ended without sending a request"))

    def search(
        self, query: str, knobs: SearchKnobs = _DEFAULT_SEARCH_KNOBS
    ) -> Either[list[dict[str, Any]], EngineFailure]:
        """POST /search and return the hits list. `related` > 0 asks for the older notes each
        of the first `related_heads` hits shares a concept with, under the hit's `related` key.
        `claims` > 0 asks each hit to hand over that many of the claims its note declares, under
        the hit's `claims` key — the record of what was settled, not the prose around it."""
        payload = {"query": query, "max_results": knobs.max_results, "max_tokens": knobs.max_tokens}
        if knobs.related:
            payload.update(related=knobs.related, related_heads=knobs.related_heads)
        if knobs.claims:
            payload["claims"] = knobs.claims
        return map_ok(self.request("POST", "/search", payload), _hits_or_empty)

    def handover(
        self, session_id: str, observed_at: str, paths: list[str]
    ) -> Either[dict[str, Any], EngineFailure]:
        """POST /handover — record what was handed to a session under its own name, so a later
        verdict-only `/consumption` has something to apply to. The engine answers with its
        `{session, handed, unknown}` summary."""
        payload = {"session_id": session_id, "observed_at": observed_at, "paths": paths}
        return self.request("POST", "/handover", payload)

    def consumption(
        self,
        session_id: str,
        observed_at: str,
        marks: ConsumptionMarks,
    ) -> Either[dict[str, Any], EngineFailure]:
        """POST /consumption — what a session did with the notes it was handed, as graph edges.
        `supersedes` pairs are `[newer_path, older_path]`. A `Verdict` (`used`|`contested`)
        travels alone: the engine applies it to everything it handed that session. `judge`
        names who is judging ('owner', 'inferred', 'agent:<name>') and lands verbatim on every
        written edge — absent means the edges name nobody (NULL)."""
        payload: dict[str, Any] = {"session_id": session_id, "observed_at": observed_at}
        match marks:
            case Verdict(verdict, judge):
                if judge is not None:
                    payload["judge"] = judge
                # 옛 묶음이 여기서 목록과 verdict 의 동행을 거부했지만, 합 타입이라
                # 그 호출 모양 자체가 없다 — 엔진에 가기 전에 타입이 막는다.
                payload["verdict"] = verdict
            case PathMarks(used, contested, supersedes, judge):
                if judge is not None:
                    payload["judge"] = judge
                payload["used"] = used or []
                payload["contested"] = contested or []
                if supersedes:
                    payload["supersedes"] = supersedes
        return self.request("POST", "/consumption", payload)

    def remember(
        self,
        title: str,
        body: str,
        provenance: NoteProvenance = _DEFAULT_NOTE_PROVENANCE,
    ) -> Either[dict[str, Any], EngineFailure]:
        """POST /remember — a new note, optionally correcting older ones. `supersedes` names the
        source paths the new note replaces; the engine writes the supersede edges so the next
        recall sinks the old notes below the new one. `author` lands on the note, `judge` on the
        supersede edges; absent, the engine records the note as `unknown`. Same body the MCP
        `remember` tool sends; the response is `{source_path, wiki_id, duplicate, supersedes, unknown}`."""
        payload: dict[str, Any] = {"title": title, "body": body, "origin": provenance.origin}
        if provenance.tags:
            payload["tags"] = provenance.tags
        if provenance.supersedes:
            payload["supersedes"] = provenance.supersedes
        if provenance.repo:
            payload["repo"] = provenance.repo
        if provenance.author is not None:
            payload["author"] = provenance.author
        if provenance.judge is not None:
            payload["judge"] = provenance.judge
        # 모든 운영 remember 는 문(:7710) 하나를 지난다 — 문이 엔진으로 바이트 그대로 넘기고
        # 같은 요청을 파이썬 쓰기 경로에 「쓰지 않고」 태워 대조한다(E3a-1). 엔진 직행은
        # 이 계약의 non-goal 이라 이 메서드에 남지 않는다.
        return self.request("POST", "/remember", payload, base_url=omb_env.door_url())

    def health(self) -> Either[dict[str, Any], EngineFailure]:
        """GET /health."""
        return self.request("GET", "/health")

    def sync(self, timeout: float | None = None) -> Either[dict[str, Any], EngineFailure]:
        """POST /sync. The class default is sized for point reads; a whole-vault scan
        outgrows any constant, so callers that only need the engine to keep going pass
        their own deadline explicitly."""
        return self.request("POST", "/sync", timeout=timeout)

    def audit(self) -> Either[dict[str, Any], EngineFailure]:
        """GET /audit."""
        return self.request("GET", "/audit")

    def mcp_call(
        self, name: str, arguments: dict[str, Any], timeout: float = 45.0
    ) -> Either[dict[str, Any], EngineFailure]:
        """POST /mcp with a JSON-RPC tools/call payload."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        return self.request("POST", "/mcp", payload, timeout=timeout)

    def context(
        self, project: str | None = None, max_items: int = 5
    ) -> Either[dict[str, Any], EngineFailure]:
        """POST /context and return the structured context card."""
        payload: dict[str, Any] = {"max_items": max_items}
        if project:
            payload["project"] = project
        return self.request("POST", "/context", payload)


def check_drudge_writable(client: DrudgeClient | None = None) -> Either[None, NotWritable]:
    """Return Ok(None) when drudge can accept a write right now, Err(NotWritable) otherwise.

    Collectors distill first and remember second, so a dead write door used to burn a
    full LLM pass per session and per cycle: the marker went back to retry and the next
    run re-distilled the same input. This is the cheap check that stops that loop.

    Reads GET /health — with the default client that question rides the door(:7710), which
    relays the engine's answer byte-for-byte (E4-α). Blocks on an explicit ``db_healthy``
    of false, or a ``degraded`` status. A response without ``db_healthy`` is an engine
    running wiki-first (or an older build) and is allowed through — absence of the field
    is not evidence of failure. An unreachable engine blocks, since nothing can be written
    to it either — 어떤 실패든 이 한 변형으로 좁힌다 (philosophy-parse: 경계에서 한 번 좁힌다).
    """
    client = client or DrudgeClient()
    match client.health():
        case Ok(health):
            return _judge_health(health)
        case Err(failure):
            return Err(NotWritable(f"drudge /health unreachable: {failure}"))


@dataclass(frozen=True)
class RememberOutcome:
    ok: bool
    status: str


@dataclass(frozen=True)
class _Retry:
    """한 번의 시도가 일시 실패로 끝나 이미 기다렸다 — 다음 시도로 간다."""


def _remember_request(arguments: dict[str, Any]) -> urllib.request.Request:
    """문(:7710)의 POST /mcp 로 가는 remember 요청 한 개 — 얼굴이 몇 개든 문은 하나(E3a-1)."""
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "remember", "arguments": arguments},
        }
    ).encode("utf-8")
    return urllib.request.Request(
        f"{omb_env.door_url().rstrip('/')}/mcp",
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )


def _wait_then_retry(attempt: int) -> _Retry:
    time.sleep(1 << attempt)
    return _Retry()


def _post_remember(req: urllib.request.Request, timeout: int, attempt: int, max_retries: int) -> Any:
    """The decoded reply, `_Retry` after a transient failure, or the final failed outcome."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if 500 <= e.code < 600 and attempt < max_retries:
            print(
                f"[distill-session] remember attempt {attempt + 1} got HTTP {e.code}, retrying...",
                file=sys.stderr,
            )
            return _wait_then_retry(attempt)
        print(f"[distill-session] remember call failed: {e}", file=sys.stderr)
        return RememberOutcome(False, "failed")
    except (urllib.error.URLError, TimeoutError) as e:
        if attempt < max_retries:
            print(
                f"[distill-session] remember attempt {attempt + 1} failed transiently ({e}), retrying...",
                file=sys.stderr,
            )
            return _wait_then_retry(attempt)
        print(
            f"[distill-session] remember call failed after {max_retries + 1} attempts: {e}",
            file=sys.stderr,
        )
        return RememberOutcome(False, "failed")
    except Exception as e:
        print(f"[distill-session] remember call failed: {e}", file=sys.stderr)
        return RememberOutcome(False, "failed")


def _read_remember_reply(data: Any) -> RememberOutcome:
    if data.get("error"):
        print(f"[distill-session] remember error: {data['error']}", file=sys.stderr)
        return RememberOutcome(False, "failed")

    result = data.get("result", {})
    content = result.get("content", [])
    text = ""
    for item in content if isinstance(content, list) else []:
        if isinstance(item, dict) and item.get("type") == "text":
            text += item.get("text", "")
    print(f"[distill-session] {text}", file=sys.stderr)
    # A duplicate skip is a successful deterministic outcome, not a transient failure.
    if "skipped — duplicate" in text:
        return RememberOutcome(True, "duplicate")
    if "remembered" in text:
        return RememberOutcome(True, "remembered")
    return RememberOutcome(False, "failed")


def call_remember(title, body, origin, repo, tags, tools, concepts, claims, session_id="", sources=None):  # noqa: PLR0913
    """Call ohmyboring's remember MCP tool.

    Retries transient failures (5xx, connection errors, timeouts) a bounded number of times
    so a momentary engine hiccup does not drop the session. Permanent failures (4xx, MCP
    error, missing "remembered" ack) are not retried.
    """
    arguments = {
        "title": title,
        "body": body,
        "origin": origin,
        "repo": repo,
        "tags": tags,
        "tools": tools,
        "concepts": concepts,
        "claims": claims,
    }
    if sources:
        arguments["sources"] = sources
    if session_id:
        arguments["omb_session_id"] = session_id
    req = _remember_request(arguments)

    max_retries = int(os.environ.get("DISTILL_REMEMBER_RETRIES") or "2")
    timeout = int(os.environ.get("DISTILL_REMEMBER_TIMEOUT") or "45")

    for attempt in range(max_retries + 1):
        match _post_remember(req, timeout, attempt, max_retries):
            case _Retry():
                continue
            case RememberOutcome() as outcome:
                return outcome
            case reply:
                return _read_remember_reply(reply)

    return RememberOutcome(False, "failed")


def _judge_health(health: dict[str, Any]) -> Either[None, NotWritable]:
    if health.get("db_healthy") is False:
        return Err(NotWritable("drudge reports db_healthy=false — postgres is degraded, writes would fail"))
    if "db_healthy" in health and health.get("status") == "degraded":
        return Err(NotWritable(f"drudge reports status={health.get('status')!r} — writes would fail"))
    return Ok(None)
