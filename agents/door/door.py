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
404: this is a door, not a catch-all proxy. The door's own routes, registered
outside the engine's proxy table: GET /approved reads the graph store (what
the morning card's 「해」 judged), GET /claim-source resolves a subject to its
current claim's note path, GET /projects answers the engine's plain question
when called with no params but, with `active_days`, answers a DB-backed
question the engine cannot (which projects have had a document touched in
that window), and GET/POST /repairs/split-subjects lists and merges claim
subjects the engine's canon() folds into one form now but a note still spells
two ways (e.g. "foodspring front" vs "foodspring-front") — POST is the door's
first write route: it deletes the split rows, blanks the affected notes' sha
so the engine's own /sync rereads them under the merged form, and calls that
/sync itself. None of the GETs touches the upstream.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import approved, claim_source

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
    """Proxy table: the engine's http_routes, minus any path door_routes claims natively.
    /projects is the one overlap: the contract snapshot still lists it under http_routes
    (it is the engine's own route and stays documented there), but door_routes now also
    claims it for the active_days variant — that native handler must be the only one
    registered for it, or the proxy loop (registered first) would shadow it forever."""
    contract = json.loads(_CONTRACT.read_text(encoding="utf-8"))
    door_paths = {entry.split(" ", 1)[1] for entry in contract.get("door_routes", [])}
    by_path: dict[str, list[str]] = {}
    for entry in contract["http_routes"]:
        method, path = entry.split(" ", 1)
        if path in door_paths:
            continue
        by_path.setdefault(path, []).append(method)
    return by_path


def _load_door_routes() -> list[tuple[str, str]]:
    """Snapshot door_routes as [(method, path), ...] — the native registration table, one entry
    per method. A dict keyed by path would drop one when a path carries two methods (GET and
    POST /repairs/split-subjects, since this cycle)."""
    contract = json.loads(_CONTRACT.read_text(encoding="utf-8"))
    return [tuple(entry.split(" ", 1)) for entry in contract.get("door_routes", [])]


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
        since_hours = approved.check_since_hours(raw)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not os.environ.get("DOOR_PG_DSN"):
        return JSONResponse({"error": "store not configured"}, status_code=503)
    try:
        payload = await asyncio.to_thread(_approved_payload, since_hours)
    except psycopg.OperationalError as e:
        return JSONResponse({"error": "store unreachable", "detail": str(e)}, status_code=502)
    return JSONResponse(payload)


def _fetch_claim_source(subject: str) -> list:
    with psycopg.connect(os.environ["DOOR_PG_DSN"]) as conn, conn.cursor() as cur:
        cur.execute(claim_source.SQL, (subject,))
        return cur.fetchall()


#: active_days bounds — 1 day minimum (a shorter window is not "recently active"), 365 days
#: maximum (a year is not a "recent" filter any more, and an unbounded window is a full scan).
_MIN_ACTIVE_DAYS = 1
_MAX_ACTIVE_DAYS = 365

_ACTIVE_PROJECTS_SQL = (
    "select project, count(*) from document where project <> '' "
    "and updated_at > now() - make_interval(days => %s) group by 1 order by 2 desc"
)


def _check_active_days(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"active_days must be an integer {_MIN_ACTIVE_DAYS}..{_MAX_ACTIVE_DAYS}") from None
    if value < _MIN_ACTIVE_DAYS or value > _MAX_ACTIVE_DAYS:
        raise ValueError(f"active_days must be {_MIN_ACTIVE_DAYS}..{_MAX_ACTIVE_DAYS}, got {value}")
    return value


def _fetch_active_projects(active_days: int) -> list[tuple[str, int]]:
    with psycopg.connect(os.environ["DOOR_PG_DSN"]) as conn, conn.cursor() as cur:
        cur.execute(_ACTIVE_PROJECTS_SQL, (active_days,))
        return cur.fetchall()


def _active_projects_payload(active_days: int) -> dict:
    rows = _fetch_active_projects(active_days)
    return {
        "projects": [{"project": project, "documents": count} for project, count in rows],
        "active_days": active_days,
    }


async def _projects(request: Request) -> Response:
    """GET /projects, native. No `active_days` param → the engine's own plain /projects
    answer, unchanged (proxied here rather than in `_load_routes`, since that path is now
    claimed exclusively by this handler). With `active_days` → the door's own DB-backed
    answer: projects with at least one document touched in that window, newest-active
    first — the engine's own /projects has no notion of recency to filter by."""
    raw = request.query_params.get("active_days")
    if raw is None:
        return await _proxy(request)
    try:
        active_days = _check_active_days(raw)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not os.environ.get("DOOR_PG_DSN"):
        return JSONResponse({"error": "store not configured"}, status_code=503)
    try:
        payload = await asyncio.to_thread(_active_projects_payload, active_days)
    except psycopg.OperationalError as e:
        return JSONResponse({"error": "store unreachable", "detail": str(e)}, status_code=502)
    return JSONResponse(payload)


async def _claim_source(request: Request) -> Response:
    subject = request.query_params.get("subject", "")
    if not subject.strip():
        return JSONResponse({"error": "subject query param is required"}, status_code=400)
    if not os.environ.get("DOOR_PG_DSN"):
        return JSONResponse({"error": "store not configured"}, status_code=503)
    try:
        rows = await asyncio.to_thread(_fetch_claim_source, subject)
    except psycopg.OperationalError as e:
        return JSONResponse({"error": "store unreachable", "detail": str(e)}, status_code=502)
    payload = claim_source.pick_current(subject, rows)
    if payload is None:
        return JSONResponse({"error": "no current claim for subject"}, status_code=404)
    return JSONResponse(payload)


#: repair candidates per page — 3 default (the card's "오늘 할 일" shows the top 3), 1..50 range
#: (0 is not a page, an unbounded scan is not a "top N" any more).
_MIN_REPAIR_LIMIT = 1
_MAX_REPAIR_LIMIT = 50
_DEFAULT_REPAIR_LIMIT = 3

_SPLIT_SUBJECTS_SQL = "select subject, source_path from claim"
_SPLIT_DELETE_SQL = "delete from claim where subject = any(%s) and source_path = any(%s)"
_SPLIT_UPDATE_SQL = "update document set sha = '' where source_path = any(%s)"


def _canon_py(s: str) -> str:
    """Python mirror of drudge's `ingest::canon` (Rust): lowercase, fold whitespace/`_`/`-` runs
    into one `-`. Must track that Rust rule exactly — grouping here decides which notes the
    engine's own reread will fold together next."""
    lower = s.lower()
    out: list[str] = []
    prev_sep = False
    for ch in lower:
        if ch.isspace() or ch in "_-":
            prev_sep = True
            continue
        if prev_sep and out:
            out.append("-")
        prev_sep = False
        out.append(ch)
    return "".join(out)


def _group_split_subjects(rows: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """rows: every (subject, source_path) in `claim`. A canon bucket is a repair candidate only
    when it still holds 2+ distinct raw spellings — a single spelling is not split."""
    buckets: dict[str, dict[str, Any]] = {}
    for subject, source_path in rows:
        bucket = buckets.setdefault(_canon_py(subject), {"variant_rows": {}, "notes": set()})
        bucket["variant_rows"][subject] = bucket["variant_rows"].get(subject, 0) + 1
        bucket["notes"].add(source_path)
    groups = [
        {
            "subject": norm,
            "variants": sorted(bucket["variant_rows"]),
            "rows": sum(bucket["variant_rows"].values()),
            "notes": len(bucket["notes"]),
        }
        for norm, bucket in buckets.items()
        if len(bucket["variant_rows"]) >= 2
    ]
    groups.sort(key=lambda g: g["rows"], reverse=True)
    return groups


def _count_variants(rows: list[tuple[str, str]], target: str) -> int:
    return len({subject for subject, _ in rows if _canon_py(subject) == target})


def _split_subjects_connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DOOR_PG_DSN"])


def _fetch_split_subject_rows() -> list[tuple[str, str]]:
    with _split_subjects_connect() as conn, conn.cursor() as cur:
        cur.execute(_SPLIT_SUBJECTS_SQL)
        return cur.fetchall()


def _emit_subject_merged_event(
    subject: str, deleted_rows: int, reread_notes: int, remaining_variants: int
) -> str:
    """POST straight to the engine's /events (the same wire contract as agents/shared/event_log,
    reused inline: the door container does not ship agents/shared, and this is one write).
    F3 (2026-09-22): a failed delivery used to be silent past a stdout print — no status
    check on the response, so the merge response never said whether the headline's "어제 합친
    행" would actually be there tomorrow. Now checked both ways (a non-2xx answer, or the
    request never landing at all) and reported both ways: one stderr line naming the subject
    and what happened, and a status string the caller folds into the POST response."""
    payload = {
        "ts": datetime.now(tz=UTC).isoformat(),
        "component": "door",
        "event": "subject_merged",
        "status": "ok",
        "subject": subject,
        "deleted_rows": deleted_rows,
        "reread_notes": reread_notes,
        "remaining_variants": remaining_variants,
    }
    try:
        response = _fetch(
            f"{_upstream()}/events",
            json.dumps(payload).encode(),
            {"content-type": "application/json"},
            "POST",
        )
    except OSError as e:
        print(f"[door] subject_merged event not delivered for {subject!r}: {e}", file=sys.stderr, flush=True)
        return f"failed: {e}"
    if 200 <= response.status_code < 300:
        return "delivered"
    print(
        f"[door] subject_merged event not delivered for {subject!r}: engine answered {response.status_code}",
        file=sys.stderr,
        flush=True,
    )
    return f"failed: {response.status_code}"


def _call_engine_sync() -> dict[str, Any]:
    """POST the engine's own /sync — the route this process already proxies, called inline
    rather than through a second HTTP hop into itself. Failure is reported, never swallowed."""
    try:
        response = _fetch(f"{_upstream()}/sync", b"", {}, "POST")
    except OSError as e:
        return {"ok": False, "error": f"engine unreachable: {e}"}
    body = response.body
    try:
        parsed = json.loads(body) if body else {}
    except json.JSONDecodeError:
        parsed = {"raw": body.decode("utf-8", "replace")}
    if 200 <= response.status_code < 300:
        return {"ok": True, "summary": parsed}
    return {"ok": False, "error": parsed}


def _merge_split_subject(subject: str) -> dict[str, Any] | None:
    """The POST body of the repair: locate the group, delete its split rows, blank the affected
    notes' sha, commit, hand the reread to the engine's /sync, then recount. `None` means
    `subject` names no live 2+-variant group — the door's 404."""
    rows = _fetch_split_subject_rows()
    groups = _group_split_subjects(rows)
    match = next((g for g in groups if g["subject"] == subject), None)
    if match is None:
        return None
    variants = match["variants"]
    notes = sorted({path for raw_subject, path in rows if raw_subject in variants})

    conn = _split_subjects_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_SPLIT_DELETE_SQL, (variants, notes))
            deleted_rows = cur.rowcount
            cur.execute(_SPLIT_UPDATE_SQL, (notes,))
            reread_notes = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    sync_result = _call_engine_sync()
    if not sync_result["ok"]:
        return {
            "subject": subject,
            "deleted_rows": deleted_rows,
            "reread_notes": reread_notes,
            "remaining_variants": None,
            "sync": {"error": sync_result["error"]},
        }

    rows_after = _fetch_split_subject_rows()
    remaining_variants = _count_variants(rows_after, subject)
    event_status = _emit_subject_merged_event(subject, deleted_rows, reread_notes, remaining_variants)
    return {
        "subject": subject,
        "deleted_rows": deleted_rows,
        "reread_notes": reread_notes,
        "remaining_variants": remaining_variants,
        "sync": sync_result["summary"],
        "event": event_status,
    }


async def _repairs_split_subjects_get(request: Request) -> Response:
    raw_limit = request.query_params.get("limit", str(_DEFAULT_REPAIR_LIMIT))
    try:
        limit = int(raw_limit)
    except ValueError:
        limit = None
    if limit is None or not (_MIN_REPAIR_LIMIT <= limit <= _MAX_REPAIR_LIMIT):
        return JSONResponse(
            {"error": f"limit must be an integer {_MIN_REPAIR_LIMIT}..{_MAX_REPAIR_LIMIT}"}, status_code=400
        )
    if not os.environ.get("DOOR_PG_DSN"):
        return JSONResponse({"error": "store not configured"}, status_code=503)
    try:
        rows = await asyncio.to_thread(_fetch_split_subject_rows)
    except psycopg.OperationalError as e:
        return JSONResponse({"error": "store unreachable", "detail": str(e)}, status_code=502)
    groups = _group_split_subjects(rows)
    return JSONResponse({"groups": groups[:limit], "total_groups": len(groups)})


async def _repairs_split_subjects_post(request: Request) -> Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    subject = body.get("subject") if isinstance(body, dict) else None
    if not subject:
        return JSONResponse({"error": "subject is required"}, status_code=400)
    if not os.environ.get("DOOR_PG_DSN"):
        return JSONResponse({"error": "store not configured"}, status_code=503)
    try:
        result = await asyncio.to_thread(_merge_split_subject, subject)
    except psycopg.OperationalError as e:
        return JSONResponse({"error": "store unreachable", "detail": str(e)}, status_code=502)
    if result is None:
        return JSONResponse({"error": "no split-subject group for that subject"}, status_code=404)
    if result["sync"].get("error") is not None:
        return JSONResponse(result, status_code=502)
    return JSONResponse(result)


for _path, _methods in _load_routes().items():
    app.add_api_route(_path, _proxy, methods=_methods)

# The door's own routes, driven by the snapshot's door_routes key. A snapshot
# entry without a native handler here is a KeyError at startup, not a proxy hole.
_DOOR_HANDLERS = {
    "GET /approved": _approved,
    "GET /claim-source": _claim_source,
    "GET /projects": _projects,
    "GET /repairs/split-subjects": _repairs_split_subjects_get,
    "POST /repairs/split-subjects": _repairs_split_subjects_post,
}
for _method, _path in _load_door_routes():
    app.add_api_route(_path, _DOOR_HANDLERS[f"{_method} {_path}"], methods=[_method])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DOOR_PORT", "7710")))
