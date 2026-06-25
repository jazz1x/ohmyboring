#!/usr/bin/env python3
"""Shared HTTP client for ohmyboring's drudge engine.

Centralizes retries, timeouts, and JSON parsing so Python adapters (recall,
distillation, schedulers, diagnostics) stop duplicating urllib boilerplate.
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Optional

import omb_env


class DrudgeClient:
    """Minimal drudge HTTP client. Silent failures are left to callers."""

    def __init__(
        self,
        base_url: Optional[str] = None,
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
        payload: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
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
        payload: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                return self._request(method, path, payload, timeout)
            except urllib.error.HTTPError as e:
                last_err = e
                if 500 <= e.code < 600 and attempt < self.retries:
                    time.sleep(1 << attempt)
                    continue
                raise
            except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
                last_err = e
                if attempt < self.retries:
                    time.sleep(1 << attempt)
                    continue
                raise
        raise last_err or RuntimeError("unexpected empty retry loop")

    def search(self, query: str, max_results: int = 3, max_tokens: int = 1500) -> list[dict[str, Any]]:
        """POST /search and return the hits list."""
        data = self._retry(
            "POST",
            "/search",
            {"query": query, "max_results": max_results, "max_tokens": max_tokens},
        )
        return data.get("hits", []) if isinstance(data, dict) else []

    def health(self) -> dict[str, Any]:
        """GET /health."""
        return self._retry("GET", "/health")

    def sync(self) -> dict[str, Any]:
        """POST /sync."""
        return self._retry("POST", "/sync")

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
