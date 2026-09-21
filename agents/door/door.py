#!/usr/bin/env python3
"""The door — a read-only FastAPI proxy in front of the Rust engine.

The migration to a Python engine opens one door at a time. This process
listens on DOOR_PORT (default 7710) and forwards exactly the five read-only
doors to the Rust engine at DOOR_UPSTREAM (default http://127.0.0.1:7700):
GET /health, /audit, /projects, /recall-label-stats and POST /mcp. Status
code, body bytes and content-type come back exactly as the engine sent them —
the door never re-serializes a payload. An engine error answer is passed
through as-is; an engine that cannot be reached is a 502 with a JSON body,
never a silent 200 with an empty one. Unregistered paths get FastAPI's
default 404: this is a door, not a catch-all proxy.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

_TIMEOUT = float(os.environ.get("DOOR_TIMEOUT", "20"))

app = FastAPI(title="oh-my-boring door")


def _upstream() -> str:
    return os.environ.get("DOOR_UPSTREAM", "http://127.0.0.1:7700").rstrip("/")


async def _proxy(request: Request, path: str) -> Response:
    query = request.url.query
    url = f"{_upstream()}{path}" + (f"?{query}" if query else "")
    body = await request.body()
    headers = {}
    if "content-type" in request.headers:
        headers["content-type"] = request.headers["content-type"]
    upstream_req = urllib.request.Request(
        url, data=body if body else None, headers=headers, method=request.method
    )
    try:
        with urllib.request.urlopen(upstream_req, timeout=_TIMEOUT) as upstream:
            return Response(
                content=upstream.read(),
                status_code=upstream.status,
                headers={"content-type": upstream.headers.get("content-type", "application/json")},
            )
    except urllib.error.HTTPError as e:
        # The engine answered; it just said no. A faithful door passes that through.
        return Response(
            content=e.read(),
            status_code=e.code,
            headers={"content-type": e.headers.get("content-type", "application/json")},
        )
    except OSError:
        # URLError, ConnectionRefusedError, timeout — one class: the engine is gone.
        return JSONResponse(
            {"error": "engine unreachable", "upstream": _upstream()},
            status_code=502,
        )


@app.get("/health")
async def health(request: Request) -> Response:
    return await _proxy(request, "/health")


@app.get("/audit")
async def audit(request: Request) -> Response:
    return await _proxy(request, "/audit")


@app.get("/projects")
async def projects(request: Request) -> Response:
    return await _proxy(request, "/projects")


@app.get("/recall-label-stats")
async def recall_label_stats(request: Request) -> Response:
    return await _proxy(request, "/recall-label-stats")


@app.post("/mcp")
async def mcp(request: Request) -> Response:
    return await _proxy(request, "/mcp")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DOOR_PORT", "7710")))
