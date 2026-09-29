#!/usr/bin/env python3
"""엔진에 나가는 쪽 한 벌 — ohmyboring 의 drudge 엔진을 부르는 HTTP 클라이언트.

Centralizes retries, timeouts, and JSON parsing so Python adapters (recall,
distillation, schedulers, diagnostics) stop duplicating urllib boilerplate.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import omb_env

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
class ConsumptionMarks:
    """`DrudgeClient.consumption` 의 선택 인자 묶음 — 필드 이름·기본값은 옛 키워드 인자와 같다."""

    used: list[str] | None = None
    contested: list[str] | None = None
    supersedes: list[list[str]] | None = None
    verdict: str | None = None
    judge: str | None = None


@dataclass(frozen=True)
class NoteProvenance:
    """`DrudgeClient.remember` 의 선택 인자 묶음 — 필드 이름·기본값은 옛 키워드 인자와 같다."""

    tags: list[str] | None = None
    supersedes: list[str] | None = None
    origin: str = "personal"
    repo: str | None = None
    author: str | None = None
    judge: str | None = None


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


class DrudgeNotWritableError(Exception):
    """drudge cannot accept writes right now — distillation must not run."""


class DrudgeClient:
    """Minimal drudge HTTP client. Silent failures are left to callers."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 5.0,
        retries: int = 1,
    ):
        self.base_url = (base_url or os.environ.get("BORING_URL") or omb_env.drudge_url()).rstrip("/")
        self.timeout = timeout
        self.retries = retries

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"content-type": "application/json"} if data is not None else {}
        headers.update(owner_headers(payload))
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _retry(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._request(method, path, payload, timeout)
            except urllib.error.HTTPError as e:
                last_err = e
                if 500 <= e.code < 600 and attempt < self.retries:
                    time.sleep(1 << attempt)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = e
                if attempt < self.retries:
                    time.sleep(1 << attempt)
                    continue
                raise
        raise last_err or RuntimeError("unexpected empty retry loop")

    def search(self, query: str, knobs: SearchKnobs = _DEFAULT_SEARCH_KNOBS) -> list[dict[str, Any]]:
        """POST /search and return the hits list. `related` > 0 asks for the older notes each
        of the first `related_heads` hits shares a concept with, under the hit's `related` key.
        `claims` > 0 asks each hit to hand over that many of the claims its note declares, under
        the hit's `claims` key — the record of what was settled, not the prose around it."""
        payload = {"query": query, "max_results": knobs.max_results, "max_tokens": knobs.max_tokens}
        if knobs.related:
            payload.update(related=knobs.related, related_heads=knobs.related_heads)
        if knobs.claims:
            payload["claims"] = knobs.claims
        data = self._retry("POST", "/search", payload)
        return data.get("hits", []) if isinstance(data, dict) else []

    def handover(self, session_id: str, observed_at: str, paths: list[str]) -> dict[str, Any]:
        """POST /handover — record what was handed to a session under its own name, so a later
        verdict-only `/consumption` has something to apply to. The engine answers with its
        `{session, handed, unknown}` summary."""
        payload = {"session_id": session_id, "observed_at": observed_at, "paths": paths}
        return self._retry("POST", "/handover", payload)

    def consumption(
        self,
        session_id: str,
        observed_at: str,
        marks: ConsumptionMarks,
    ) -> dict[str, Any]:
        """POST /consumption — what a session did with the notes it was handed, as graph edges.
        `supersedes` pairs are `[newer_path, older_path]`. A `verdict` (`used`|`contested`)
        travels alone: the engine applies it to everything it handed that session, and a
        payload listing paths beside a verdict is rejected. `judge` names who is judging
        ('owner', 'inferred', 'agent:<name>') and lands verbatim on every written edge —
        absent means the edges name nobody (NULL)."""
        payload: dict[str, Any] = {"session_id": session_id, "observed_at": observed_at}
        if marks.judge is not None:
            payload["judge"] = marks.judge
        if marks.verdict is not None:
            # Dropping the lists here would hide a caller bug behind a 200: the caller thinks
            # its paths were judged, the engine judged what it had handed. Refuse instead.
            if marks.used or marks.contested:
                raise ValueError("consumption: pass either a verdict or path lists, not both")
            payload["verdict"] = marks.verdict
        else:
            payload["used"] = marks.used or []
            payload["contested"] = marks.contested or []
        if marks.supersedes:
            payload["supersedes"] = marks.supersedes
        return self._retry("POST", "/consumption", payload)

    def remember(
        self,
        title: str,
        body: str,
        provenance: NoteProvenance = _DEFAULT_NOTE_PROVENANCE,
    ) -> dict[str, Any]:
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
        return self._retry("POST", "/remember", payload)

    def health(self) -> dict[str, Any]:
        """GET /health."""
        return self._retry("GET", "/health")

    def sync(self, timeout: float | None = None) -> dict[str, Any]:
        """POST /sync. The class default is sized for point reads; a whole-vault scan
        outgrows any constant, so callers that only need the engine to keep going pass
        their own deadline explicitly."""
        return self._retry("POST", "/sync", timeout=timeout)

    def audit(self) -> dict[str, Any]:
        """GET /audit."""
        return self._retry("GET", "/audit")

    def mcp_call(self, name: str, arguments: dict[str, Any], timeout: float = 45.0) -> dict[str, Any]:
        """POST /mcp with a JSON-RPC tools/call payload."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        return self._retry("POST", "/mcp", payload, timeout=timeout)

    def context(self, project: str | None = None, max_items: int = 5) -> dict[str, Any]:
        """POST /context and return the structured context card."""
        payload: dict[str, Any] = {"max_items": max_items}
        if project:
            payload["project"] = project
        return self._retry("POST", "/context", payload)


def check_drudge_writable(client: DrudgeClient | None = None) -> None:
    """Raise DrudgeNotWritableError unless drudge can accept a write right now.

    Collectors distill first and remember second, so a dead write door used to burn a
    full LLM pass per session and per cycle: the marker went back to retry and the next
    run re-distilled the same input. This is the cheap check that stops that loop.

    Reads GET /health. Blocks on an explicit ``db_healthy`` of false, or a ``degraded``
    status. A response without ``db_healthy`` is an engine running wiki-first (or an
    older build) and is allowed through — absence of the field is not evidence of
    failure. An unreachable engine blocks, since nothing can be written to it either.
    """
    client = client or DrudgeClient()
    try:
        health = client.health()
    except Exception as exc:  # noqa: BLE001 — any transport failure means "cannot write"
        raise DrudgeNotWritableError(f"drudge /health unreachable: {exc}") from exc

    if health.get("db_healthy") is False:
        raise DrudgeNotWritableError(
            "drudge reports db_healthy=false — postgres is degraded, writes would fail"
        )
    if "db_healthy" in health and health.get("status") == "degraded":
        raise DrudgeNotWritableError(f"drudge reports status={health.get('status')!r} — writes would fail")
