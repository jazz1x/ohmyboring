#!/usr/bin/env python3
"""The door — a FastAPI proxy in front of the Rust engine.

The route table is read at startup from the contract snapshot
(data/contract/engine-contract.json), never hand-listed: the contract changes
and the door follows, and a door narrower or wider than the contract fails its
tests. This process listens on DOOR_PORT (default 7710) and forwards the
snapshot's routes to the engine at DOOR_UPSTREAM (default http://127.0.0.1:7700).
Only the headers the engine reads travel on (content-type, accept,
mcp-session-id, x-request-id); the response carries the upstream content-type,
or none at all when the engine sent none — never a default. Status code and
body bytes come back exactly as sent. An answer whose content-type is
text/event-stream is relayed chunk by chunk as it arrives, never read to
completion first, and the upstream socket is closed when the client goes away —
an endless engine stream stays endless through the door. An engine answer, 4xx
included, passes through as-is; an engine that cannot be reached is a 502 JSON
body, never a silent 200 with an empty one. Unregistered paths get FastAPI's
404: this is a door, not a catch-all proxy. One route is the door's own,
registered outside the engine's table: GET /approved reads the graph store
(what the morning card's 「해」 judged) and never touches the upstream.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import approved

_TIMEOUT = float(os.environ.get("DOOR_TIMEOUT", "130"))

_CONTRACT = Path(__file__).resolve().parents[2] / "data" / "contract" / "engine-contract.json"

app = FastAPI(title="oh-my-boring door", docs_url=None, redoc_url=None, openapi_url=None)


def _upstream() -> str:
    return os.environ.get("DOOR_UPSTREAM", "http://127.0.0.1:7700").rstrip("/")


def _pass_through(status: int, body: bytes, content_type: str | None) -> Response:
    headers = {"content-type": content_type} if content_type is not None else {}
    return Response(content=body, status_code=status, headers=headers)


async def _stream_chunks(upstream: http.client.HTTPResponse) -> AsyncIterator[bytes]:
    try:
        while chunk := await asyncio.to_thread(upstream.read1, 1024):
            yield chunk
    finally:
        # A worker thread may still be blocked in read1; on macOS close() is
        # deferred until that recv returns, so shut the socket down first.
        dup = socket.fromfd(upstream.fileno(), socket.AF_INET, socket.SOCK_STREAM)
        try:
            dup.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            dup.close()
        upstream.close()


def _fetch(url: str, body: bytes, headers: dict[str, str], method: str) -> Response:
    upstream_req = urllib.request.Request(url, data=body if body else None, headers=headers, method=method)
    try:
        upstream = urllib.request.urlopen(upstream_req, timeout=_TIMEOUT)
    except urllib.error.HTTPError as e:
        # The engine answered; it just said no. A faithful door passes that through.
        return _pass_through(e.code, e.read(), e.headers.get("content-type"))
    content_type = upstream.headers.get("content-type")
    if content_type is not None and content_type.startswith("text/event-stream"):
        return StreamingResponse(
            _stream_chunks(upstream),
            status_code=upstream.status,
            headers={"content-type": content_type},
        )
    with upstream:
        return _pass_through(upstream.status, upstream.read(), content_type)


async def _proxy(request: Request) -> Response:
    query = request.url.query
    url = f"{_upstream()}{request.url.path}" + (f"?{query}" if query else "")
    body = await request.body()
    headers = {
        name: request.headers[name]
        for name in ("content-type", "accept", "mcp-session-id", "x-request-id")
        if name in request.headers
    }
    try:
        return await asyncio.to_thread(_fetch, url, body, headers, request.method)
    except OSError:
        # URLError, ConnectionRefusedError, timeout — one class: the engine is gone.
        return JSONResponse(
            {"error": "engine unreachable", "upstream": _upstream()},
            status_code=502,
        )


def _load_routes() -> dict[str, list[str]]:
    """Proxy table: the engine's http_routes only. door_routes are the door's own
    and are registered natively below — never forwarded upstream."""
    entries = json.loads(_CONTRACT.read_text(encoding="utf-8"))["http_routes"]
    by_path: dict[str, list[str]] = {}
    for entry in entries:
        method, path = entry.split(" ", 1)
        by_path.setdefault(path, []).append(method)
    return by_path


def _load_door_routes() -> dict[str, str]:
    """Snapshot door_routes as {path: method} — the native registration table."""
    contract = json.loads(_CONTRACT.read_text(encoding="utf-8"))
    pairs = (entry.split(" ", 1) for entry in contract.get("door_routes", []))
    return {path: method for method, path in pairs}


_EDGES_SQL = (
    "select src, dst, kind from edge where src like 'session:slack:%' and kind in ('used', 'contested')"
)


def _fetch_edges() -> list[tuple[str, str, str]]:
    with psycopg.connect(os.environ["DOOR_PG_DSN"]) as conn, conn.cursor() as cur:
        cur.execute(_EDGES_SQL)
        return cur.fetchall()


def _approved_payload(since_hours: int) -> dict:
    rows = _fetch_edges()
    selection = approved.select_approved(rows, since_hours, datetime.now(tz=approved.SEOUL))
    return {
        "since_hours": since_hours,
        "approved": [
            {"session": item.session, "note": item.note, "at": item.at.isoformat(timespec="seconds")}
            for item in selection.approved
        ],
        "contested": [
            {"session": item.session, "note": item.note, "at": item.at.isoformat(timespec="seconds")}
            for item in selection.contested
        ],
    }


async def _approved(request: Request) -> Response:
    raw = request.query_params.get("since_hours", "24")
    try:
        since_hours = int(raw)
        if since_hours < 0:
            raise ValueError
    except ValueError:
        return JSONResponse({"error": "since_hours must be a non-negative integer"}, status_code=400)
    if not os.environ.get("DOOR_PG_DSN"):
        return JSONResponse({"error": "store not configured"}, status_code=503)
    try:
        payload = await asyncio.to_thread(_approved_payload, since_hours)
    except psycopg.OperationalError as e:
        return JSONResponse({"error": "store unreachable", "detail": str(e)}, status_code=502)
    return JSONResponse(payload)


for _path, _methods in _load_routes().items():
    app.add_api_route(_path, _proxy, methods=_methods)

# The door's own routes, driven by the snapshot's door_routes key. A snapshot
# entry without a native handler here is a KeyError at startup, not a proxy hole.
_DOOR_HANDLERS = {"GET /approved": _approved}
for _path, _method in _load_door_routes().items():
    app.add_api_route(_path, _DOOR_HANDLERS[f"{_method} {_path}"], methods=[_method])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DOOR_PORT", "7710")))
