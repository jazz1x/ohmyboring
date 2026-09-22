#!/usr/bin/env python3
"""Shared HTTP client for ohmyboring's drudge engine.

Centralizes retries, timeouts, and JSON parsing so Python adapters (recall,
distillation, schedulers, diagnostics) stop duplicating urllib boilerplate.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

import omb_env


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

    def search(
        self,
        query: str,
        max_results: int = 3,
        max_tokens: int = 1500,
        related: int = 0,
        related_heads: int = 2,
        claims: int = 0,
    ) -> list[dict[str, Any]]:
        """POST /search and return the hits list. `related` > 0 asks for the older notes each
        of the first `related_heads` hits shares a concept with, under the hit's `related` key.
        `claims` > 0 asks each hit to hand over that many of the claims its note declares, under
        the hit's `claims` key — the record of what was settled, not the prose around it."""
        payload = {"query": query, "max_results": max_results, "max_tokens": max_tokens}
        if related:
            payload.update(related=related, related_heads=related_heads)
        if claims:
            payload["claims"] = claims
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
        used: list[str] | None = None,
        contested: list[str] | None = None,
        supersedes: list[list[str]] | None = None,
        verdict: str | None = None,
    ) -> dict[str, Any]:
        """POST /consumption — what a session did with the notes it was handed, as graph edges.
        `supersedes` pairs are `[newer_path, older_path]`. A `verdict` (`used`|`contested`)
        travels alone: the engine applies it to everything it handed that session, and a
        payload listing paths beside a verdict is rejected."""
        payload: dict[str, Any] = {"session_id": session_id, "observed_at": observed_at}
        if verdict is not None:
            # Dropping the lists here would hide a caller bug behind a 200: the caller thinks
            # its paths were judged, the engine judged what it had handed. Refuse instead.
            if used or contested:
                raise ValueError("consumption: pass either a verdict or path lists, not both")
            payload["verdict"] = verdict
        else:
            payload["used"] = used or []
            payload["contested"] = contested or []
        if supersedes:
            payload["supersedes"] = supersedes
        return self._retry("POST", "/consumption", payload)

    def remember(
        self,
        title: str,
        body: str,
        *,
        tags: list[str] | None = None,
        supersedes: list[str] | None = None,
        origin: str = "personal",
        repo: str | None = None,
    ) -> dict[str, Any]:
        """POST /remember — a new note, optionally correcting older ones. `supersedes` names the
        source paths the new note replaces; the engine writes the supersede edges so the next
        recall sinks the old notes below the new one. Same body the MCP `remember` tool sends;
        the response is `{source_path, wiki_id, duplicate, supersedes, unknown}`."""
        payload: dict[str, Any] = {"title": title, "body": body, "origin": origin}
        if tags:
            payload["tags"] = tags
        if supersedes:
            payload["supersedes"] = supersedes
        if repo:
            payload["repo"] = repo
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
