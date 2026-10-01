#!/usr/bin/env python3
"""The door — a FastAPI proxy in front of the Rust engine.

The route table is read at startup from the contract snapshot
(data/contract/engine-contract.json), never hand-listed: the contract changes
and the door follows, and a door narrower or wider than the contract fails its
tests. This process listens on DOOR_PORT (default 7710) and forwards the
snapshot's routes to the engine at DOOR_UPSTREAM (default http://127.0.0.1:7700).
Only the headers the engine reads travel on (content-type, accept,
mcp-session-id, x-request-id, x-boring-owner-token — carried as sent, checked
only by the engine); the response carries the upstream content-type,
or none at all when the engine sent none — never a default. Status code and
body bytes come back exactly as sent — one named exception: a POST /mcp
tools/call `recall` answer, 2xx application/json, gets numbered-note blocks
prepended (query 안의 wiki-NNNN 마다 볼트 노트 블록이 content 앞에 — 엔진 recall 은
검색이라 번호를 못 풀어 문이 이름을 푼다; see _augment_mcp_recall). Anything that is
not exactly that gate — no numbers, another tool, isError, non-JSON, a stream —
travels byte-for-byte. An answer whose content-type is
text/event-stream is relayed chunk by chunk as it arrives, never read to
completion first, and the upstream socket is closed when the client goes away —
an endless engine stream stays endless through the door. An engine answer, 4xx
included, passes through as-is; an engine that cannot be reached is a 502 JSON
body, never a silent 200 with an empty one. Unregistered paths get FastAPI's 404: this is a door, not a catch-all proxy. The one search route the
door answers itself is POST /search (E2a, E2c): the python ranking pipeline (ohmyboring.search —
RRF fusion, verdict feedback, budget, in-set order, and the related graph expansion) answers directly
and stamps the response x-boring-search: python — every /search answer, related or not. The door's
own routes, registered
outside the engine's proxy table: GET /approved reads the graph store (what
the morning card's 「해」 judged), GET /claim-source resolves a subject to its
current claim's note path (GET /claim-sources: every subject's current claim for one
predicate, the same pick per subject), GET /rules groups the owner's standing
corrections (kind='rule' claims: the 'rule' sentence + its trigger words per
subject) for the UserPromptSubmit trigger hook, GET /projects answers the engine's plain question
when called with no params but, with `active_days`, answers a DB-backed
question the engine cannot (which projects have had a document touched in
that window), and GET/POST /repairs/split-subjects lists and merges claim
subjects the engine's canon() folds into one form now but a note still spells
two ways (e.g. "foodspring front" vs "foodspring-front") — POST is the door's
first write route: it deletes the split rows (an owner-written note's only with the
owner token — otherwise that note is skipped and named in `owner_held`), blanks the affected notes' sha
so the engine's own /sync rereads them under the merged form, and calls that
/sync itself — one reread at a time, merges landing meanwhile queue for the next; a body
`row: {channel, card_ts, idx}` names the card row the door settles when its reread ends. Of the GETs, only /projects without active_days reaches the upstream.
And POST /run/morning-card · POST /run/weekly-card run the card programs
themselves (agents/slack/card.py · weekly_card.py as a subprocess with the door's
own env): hermes cron owns the schedule and asks the door to fire the tool, one run
per route at a time (a second call while one runs is 409), answering {ok, exit,
posted_ts?, tail} — a non-zero exit is ok:false with the code, never hidden. No
auth header: the door binds 127.0.0.1 on the host and the compose-internal bridge
otherwise, the hermes script env carries no token to send, and the tool's own
once-per-morning / once-per-week guard makes a duplicate trigger exit 0 rather
than post twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import hmac
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, StrictInt, StrictStr, ValidationError, field_validator
from slack_sdk.errors import SlackClientError

from ohmyboring import config as boring_config
from ohmyboring.adapters import vault as vault_notes
from ohmyboring.entrypoints.http import mcp_recall
from ohmyboring.recall import named as recall_named
from ohmyboring.result import Either, Err, Ok
from ohmyboring.search import hits as search_hits
from ohmyboring.search import pg as search_pg
from ohmyboring.search import retriever as search_retriever

from ..shared import vault_note
from . import approved, claim_source, rules

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "slack"))
import card_advice  # noqa: E402
import card_types  # noqa: E402
import card_view  # noqa: E402

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


_OWNER_TOKEN_HEADER = "x-boring-owner-token"


async def _proxy(request: Request) -> Response:
    query = request.url.query
    url = f"{_upstream()}{request.url.path}" + (f"?{query}" if query else "")
    body = await request.body()
    headers = {
        name: request.headers[name]
        for name in ("content-type", "accept", "mcp-session-id", "x-request-id", _OWNER_TOKEN_HEADER)
        if name in request.headers
    }
    try:
        response = await asyncio.to_thread(_fetch, url, body, headers, request.method)
    except OSError:
        # URLError, ConnectionRefusedError, timeout — one class: the engine is gone.
        return JSONResponse(
            {"error": "engine unreachable", "upstream": _upstream()},
            status_code=502,
        )
    if request.method == "POST" and request.url.path == "/mcp":
        return _augment_mcp_recall(body, response)
    return response


#: 볼트 마운트 — compose 가 읽기 전용 /vault 를 싣고, 호스트·테스트는 BORING_VAULT_DIR 로
#: 바꾼다(카드 프로그램이 쓰는 환경과 같은 escape hatch).
_VAULT_ENV = "BORING_VAULT_DIR"
_DEFAULT_VAULT_DIR = "/vault"


def _vault_dir() -> str:
    return os.environ.get(_VAULT_ENV) or _DEFAULT_VAULT_DIR


def _note_block_for(note_id: str) -> str:
    """번호 하나의 블록 — 볼트에서 노트 텍스트를 읽어(없으면 None) 공용 블록 렌더에 넘긴다.
    볼트를 못 읽는 OSError 는 여기서 삼키지 않는다 — 호출자가 회상 자체를 죽이지 않게 받는다."""
    text = vault_notes.read_note(_vault_dir(), note_id)
    return recall_named.note_block(note_id, text, vault_note.split_frontmatter)


def _augment_mcp_recall(body: bytes, response: Response) -> Response:
    """POST /mcp 만 여기 지난다: 엔진이 2xx application/json 으로 성공한 tools/call recall
    답이면 번호 노트 블록을 앞에 붙여 돌려준다. 나머지(본문이 JSON 객체가 아님·비 2xx·
    JSON 아닌 content-type·번호 없음·recall 아닌 도구·isError)는 받은 바이트 그대로 — GET
    /mcp 스트림은 애초에 이 분기에 안 들어온다."""
    if not 200 <= response.status_code < 300:
        return response
    content_type = response.headers.get("content-type")
    if content_type is None or not content_type.startswith("application/json"):
        return response
    try:
        request_json = json.loads(body)
        response_json = json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        return response
    if not isinstance(request_json, dict) or not isinstance(response_json, dict):
        return response
    try:
        augmented = mcp_recall.augment(request_json, response_json, _note_block_for)
    except OSError as e:
        # 볼트 불응은 회상을 죽이지 않는다 — hermes 플러그인과 같은 정책: 로그 한 줄, 엔진 답 그대로.
        print(f"[door] recall note block skipped: vault unreadable: {e}", file=sys.stderr, flush=True)
        return response
    if augmented is response_json:
        return response
    return _pass_through(
        response.status_code, json.dumps(augmented, ensure_ascii=False).encode("utf-8"), content_type
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


def _fetch_claim_sources(predicate: str) -> list:
    with psycopg.connect(os.environ["DOOR_PG_DSN"]) as conn, conn.cursor() as cur:
        cur.execute(claim_source.LIST_SQL, (predicate,))
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


async def _claim_sources(request: Request) -> Response:
    predicate = request.query_params.get("predicate", "")
    if not predicate.strip():
        return JSONResponse({"error": "predicate query param is required"}, status_code=400)
    if not os.environ.get("DOOR_PG_DSN"):
        return JSONResponse({"error": "store not configured"}, status_code=503)
    try:
        rows = await asyncio.to_thread(_fetch_claim_sources, predicate)
    except psycopg.OperationalError as e:
        return JSONResponse({"error": "store unreachable", "detail": str(e)}, status_code=502)
    return JSONResponse({"predicate": predicate, "claims": claim_source.group_current(rows)})


def _fetch_rules() -> list:
    with psycopg.connect(os.environ["DOOR_PG_DSN"]) as conn, conn.cursor() as cur:
        cur.execute(rules.SQL, ("rule",))
        return cur.fetchall()


async def _rules(request: Request) -> Response:
    """GET /rules — the owner's standing corrections, grouped by subject for the
    trigger hook: each subject's 'rule' sentence + its trigger words. A subject
    holding only one of the pair is counted in `incomplete`, never served half."""
    if not os.environ.get("DOOR_PG_DSN"):
        return JSONResponse({"error": "store not configured"}, status_code=503)
    try:
        rows = await asyncio.to_thread(_fetch_rules)
    except psycopg.OperationalError as e:
        return JSONResponse({"error": "store unreachable", "detail": str(e)}, status_code=502)
    return JSONResponse(rules.group_rules(rows))


#: how long one card run may hold its route before the door gives up on it. Generous on
#: purpose: the morning card's gemma4 calls and the weekly's own 300s child each fit inside.
_RUN_CARD_TIMEOUT_S = float(os.environ.get("DOOR_RUN_CARD_TIMEOUT", "900"))

#: The programs the two run routes execute — the same tools launchd used to fire, unchanged.
_CARD_PROGRAMS = {
    "morning-card": Path(__file__).resolve().parents[2] / "agents" / "slack" / "card.py",
    "weekly-card": Path(__file__).resolve().parents[2] / "agents" / "slack" / "weekly_card.py",
}

#: One lock per route: a card takes minutes (model calls), and a second concurrent run is not
#: a second opinion but a double post waiting to happen — the second caller gets a 409 instead.
_RUN_LOCKS: dict[str, asyncio.Lock] = {}

#: The tool prints "[card] posted ts=<slack ts>" (or "already posted today (ts=…)"); the newest
#: match on stdout becomes the response's posted_ts.
_POSTED_TS_RE = re.compile(r"ts=([0-9]+\.[0-9]+)")


def _run_card_subprocess(script: Path, timeout_s: float) -> subprocess.CompletedProcess[str]:
    """The subprocess seam the tests stub. Inherits the door's env — compose supplies the
    Slack token/channel, the vault dir, the engine URL, OLLAMA_HOST."""
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def _card_run_body(name: str, completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """{ok, exit, posted_ts?, tail} — a non-zero exit is ok:false with the code, never hidden."""
    merged = "\n".join(part for part in (completed.stderr.strip(), completed.stdout.strip()) if part)
    tail = "\n".join(merged.splitlines()[-5:])[-1000:]
    body: dict[str, Any] = {"ok": completed.returncode == 0, "exit": completed.returncode, "tail": tail}
    matches = _POSTED_TS_RE.findall(completed.stdout)
    if matches:
        body["posted_ts"] = matches[-1]
    return body


async def _run_card_route(request: Request, name: str) -> Response:
    lock = _RUN_LOCKS.setdefault(name, asyncio.Lock())
    if lock.locked():
        return JSONResponse({"error": f"{name} already running"}, status_code=409)
    async with lock:
        try:
            completed = await asyncio.to_thread(
                _run_card_subprocess, _CARD_PROGRAMS[name], _RUN_CARD_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            return JSONResponse(
                {
                    "ok": False,
                    "exit": None,
                    "tail": "",
                    "error": f"timeout after {_RUN_CARD_TIMEOUT_S:.0f}s",
                }
            )
        except OSError as e:
            return JSONResponse({"ok": False, "exit": None, "tail": "", "error": f"spawn failed: {e}"})
        return JSONResponse(_card_run_body(name, completed))


async def _run_morning_card(request: Request) -> Response:
    return await _run_card_route(request, "morning-card")


async def _run_weekly_card(request: Request) -> Response:
    return await _run_card_route(request, "weekly-card")


#: repair candidates per page — 3 default (the card's "오늘 할 일" shows the top 3), 1..50 range
#: (0 is not a page, an unbounded scan is not a "top N" any more).
_MIN_REPAIR_LIMIT = 1
_MAX_REPAIR_LIMIT = 50
_DEFAULT_REPAIR_LIMIT = 3

_SPLIT_SUBJECTS_SQL = "select subject, source_path from claim"
_SPLIT_DELETE_SQL = "delete from claim where subject = any(%s) and source_path = any(%s)"
_SPLIT_UPDATE_SQL = "update document set sha = '' where source_path = any(%s)"
_OWNER_NOTES_SQL = "select source_path from document where author = 'owner' and source_path = any(%s)"


class Standing(enum.Enum):
    OWNER = "owner"
    NOT_OWNER = "not_owner"


def _standing(presented: str | None) -> Standing:
    configured = os.environ.get("BORING_OWNER_TOKEN")
    verified = bool(configured) and presented is not None and hmac.compare_digest(configured, presented)
    return Standing.OWNER if verified else Standing.NOT_OWNER


def _owner_held(cur: psycopg.Cursor, standing: Standing, notes: list[str]) -> list[str]:
    """The owner-written notes among `notes` a request without the owner token must leave as
    they are: their claim rows are not deleted and their sha is not blanked."""
    match standing:
        case Standing.OWNER:
            return []
        case Standing.NOT_OWNER:
            cur.execute(_OWNER_NOTES_SQL, (notes,))
            return sorted(path for (path,) in cur.fetchall())


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
    subject: str, deleted_rows: int, reread_notes: int, remaining_variants: int, owner_held: list[str]
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
        "owner_held": owner_held,
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


@dataclass(frozen=True)
class _Merged:
    """A committed merge waiting for the engine to reread it; `row` is the card row to settle."""

    subject: str
    deleted_rows: int
    reread_notes: int
    owner_held: list[str]
    row: card_types.RowRef | None


#: One reread runs at a time: /sync is a whole-vault pass, and three overlapping ones timed out
#: two of themselves (2026-09-30 08:04). A merge that lands while it runs waits in the queue;
#: the worker then runs once more, which covers every merge queued meanwhile.
_REREAD_LOCK = threading.Lock()
_REREAD_QUEUE: list[_Merged] = []
_REREAD_WORKER: threading.Thread | None = None
_ROW_LOCK = threading.Lock()


def _slack_client() -> Any | None:
    token = os.environ.get("SLACK_BOT_TOKEN", "").split(",")[0].strip()
    if not token:
        return None
    from slack_sdk import WebClient

    return WebClient(token=token)


def _row_after(messages: list[dict], row: card_types.RowRef, outcome: card_types.Outcome, lang: str):
    message = next((m for m in messages if m.get("ts") == row.card_ts), None)
    if message is None:
        return card_types.Rejected(reason=f"message {row.card_ts} not in the history read")
    return card_view.mark_progress(message.get("blocks") or [], row.idx, outcome, lang=lang)


def _show_row(row: card_types.RowRef, outcome: card_types.Outcome, lang: str) -> None:
    """Rewrite one row of the card message from the message as Slack holds it now, so the
    other rows keep whatever the plugin or an earlier settle put there."""
    client = _slack_client()
    if client is None:
        print(f"[door] no SLACK_BOT_TOKEN — card row {row.idx} not settled", file=sys.stderr, flush=True)
        return
    try:
        with _ROW_LOCK:
            history = client.conversations_history(
                channel=row.channel, latest=row.card_ts, inclusive=True, limit=1
            )
            marked = _row_after(history.get("messages") or [], row, outcome, lang)
            match marked:
                case card_types.Rejected(reason=reason):
                    print(f"[door] card row {row.idx} not settled — {reason}", file=sys.stderr, flush=True)
                case _:
                    client.chat_update(channel=row.channel, ts=row.card_ts, blocks=marked)
    except (OSError, SlackClientError) as e:
        print(f"[door] card row {row.idx} not settled — {e}", file=sys.stderr, flush=True)


def _record_merged(merged: _Merged, rows: list[tuple[str, str]], lang: str) -> card_types.Outcome:
    remaining = _count_variants(rows, merged.subject)
    _emit_subject_merged_event(
        merged.subject, merged.deleted_rows, merged.reread_notes, remaining, merged.owner_held
    )
    done = card_types.RepairDone(
        subject=merged.subject,
        deleted_rows=merged.deleted_rows,
        reread_notes=merged.reread_notes,
        remaining_variants=remaining,
        owner_held=merged.owner_held,
    )
    return card_view.merged_outcome(done, lang=lang)


def _reread_outcomes(
    batch: list[_Merged], sync_result: dict[str, Any], lang: str
) -> list[card_types.Outcome]:
    """One outcome per merge in `batch`, all from the one reread that covered them."""
    if not sync_result["ok"]:
        error = sync_result["error"]
        for merged in batch:
            print(
                f"[door] reread after merging {merged.subject!r} failed: {error}", file=sys.stderr, flush=True
            )
        return [card_view.reread_failed_outcome(error, lang=lang) for _ in batch]
    try:
        rows = _fetch_split_subject_rows()
    except psycopg.Error as e:
        print(f"[door] recount after the reread failed: {e}", file=sys.stderr, flush=True)
        return [card_view.reread_failed_outcome(e, lang=lang) for _ in batch]
    return [_record_merged(merged, rows, lang) for merged in batch]


def _reread_batch(batch: list[_Merged]) -> None:
    """The engine's /sync is a whole-vault pass (minutes on the live corpus, 2026-09-29) and the
    merges are already committed before it starts, so nobody waits on it: a failure is one
    stderr line and a failed row, a success recounts, records `subject_merged` and settles the
    row — each only once the reread that covers the merge has ended."""
    lang = card_advice.resolve_lang(boring_config.note_lang())
    outcomes = _reread_outcomes(batch, _call_engine_sync(), lang)
    for merged, outcome in zip(batch, outcomes, strict=True):
        match merged.row:
            case None:
                pass
            case row:
                _show_row(row, outcome, lang)


def _take_batch() -> list[_Merged]:
    global _REREAD_WORKER
    with _REREAD_LOCK:
        batch = list(_REREAD_QUEUE)
        _REREAD_QUEUE.clear()
        if not batch:
            _REREAD_WORKER = None
        return batch


def _fail_batch(batch: list[_Merged], error: Exception) -> None:
    """A batch whose reread raised settles as failed, so no row stays ⏳ and the merges queued
    behind it still get their own reread."""
    print(f"[door] reread batch failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    lang = card_advice.resolve_lang(boring_config.note_lang())
    outcome = card_view.reread_failed_outcome(error, lang=lang)
    for merged in batch:
        match merged.row:
            case None:
                pass
            case row:
                _show_row(row, outcome, lang)


def _reread_worker() -> None:
    global _REREAD_WORKER
    try:
        while batch := _take_batch():
            try:
                _reread_batch(batch)
            except Exception as e:  # noqa: BLE001 — the batch's rows must settle either way
                _fail_batch(batch, e)
    except BaseException:
        # Whatever raised, the next merge must be able to start a worker: a stuck handle would
        # queue merges that no reread ever covers.
        with _REREAD_LOCK:
            _REREAD_WORKER = None
        raise


def _reread_in_background(merged: _Merged) -> None:
    global _REREAD_WORKER
    with _REREAD_LOCK:
        _REREAD_QUEUE.append(merged)
        if _REREAD_WORKER is not None:
            return
        _REREAD_WORKER = threading.Thread(target=_reread_worker, name="reread", daemon=True)
        _REREAD_WORKER.start()


def _merge_split_subject(
    subject: str, standing: Standing, row: card_types.RowRef | None = None
) -> dict[str, Any] | None:
    """The POST body of the repair: locate the group, delete its split rows, blank the affected
    notes' sha, commit, then start the reread without waiting for it. Without the owner
    token, owner-written notes are skipped and named in `owner_held`, so the merge of everyone
    else's rows still happens and the leftover spelling stays countable afterwards.
    `None` means `subject` names no live 2+-variant group — the door's 404."""
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
            owner_held = _owner_held(cur, standing, notes)
            touched = [path for path in notes if path not in owner_held]
            cur.execute(_SPLIT_DELETE_SQL, (variants, touched))
            deleted_rows = cur.rowcount
            cur.execute(_SPLIT_UPDATE_SQL, (touched,))
            reread_notes = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    _reread_in_background(_Merged(subject, deleted_rows, reread_notes, owner_held, row))
    return {
        "subject": subject,
        "deleted_rows": deleted_rows,
        "reread_notes": reread_notes,
        "remaining_variants": None,
        "owner_held": owner_held,
        "sync": "started",
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
    try:
        raw_row = body.get("row")
        row = None if raw_row is None else card_types.RowRef.model_validate(raw_row)
    except ValidationError as e:
        return JSONResponse(
            {"error": "row must be {channel, card_ts, idx}", "detail": str(e)}, status_code=400
        )
    if not os.environ.get("DOOR_PG_DSN"):
        return JSONResponse({"error": "store not configured"}, status_code=503)
    try:
        result = await asyncio.to_thread(
            _merge_split_subject, subject, _standing(request.headers.get(_OWNER_TOKEN_HEADER)), row
        )
    except psycopg.OperationalError as e:
        return JSONResponse({"error": "store unreachable", "detail": str(e)}, status_code=502)
    if result is None:
        return JSONResponse({"error": "no split-subject group for that subject"}, status_code=404)
    return JSONResponse(result, status_code=202)


class _SearchBody(BaseModel):
    """POST /search 본문 — 경계에서 한 번 파싱한다. StrictInt: bool·문자열·실수는 정수가
    아니고(옛 _search_int 계약과 같음), ge=0: 음수는 부호 없는 Rust 필드(usize/u32)와 어긋난다.
    기본값 5/2000/0 은 이 모형이 갖고(related_heads 는 Rust serde 기본값 2 — serve.rs:809),
    상한 클램프는 리트리버 몫(SSOT) — 문은 파싱된 값을 그대로 통과시킨다."""

    query: StrictStr
    max_results: StrictInt = Field(default=5, ge=0)
    max_tokens: StrictInt = Field(default=2000, ge=0)
    claims: StrictInt = Field(default=0, ge=0)
    related: StrictInt = Field(default=0, ge=0)
    related_heads: StrictInt = Field(default=2, ge=0)
    since_hours: StrictInt | None = None
    project: StrictStr | None = None
    session_id: StrictStr | None = None

    @field_validator("query")
    @classmethod
    def _query_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query is required")
        return value


def _search_execute(retriever: search_retriever.PgRetriever, query: str):
    """The seam the tests stub — Either 를 그대로 돌려준다."""
    return retriever.search(query)


def _search_handover(session_id: str, paths: list[str]) -> Either[search_pg.HandoverReport, str]:
    """보여준 hit 마다 handed 간선 (store.rs:3028). 실패는 Err — 핸들러가 502 로 접는다.
    Rust 는 여기서 eprintln 만 하고 성공 응답을 줬다 — E2a 가 일부러 닫은 자리(조용한
    폭락 금지)라 주석이 아니라 코드가 갈라진다."""
    observed_at = datetime.now(tz=UTC).isoformat()
    match search_pg.connect(os.environ["DOOR_PG_DSN"]):
        case Err(failure):
            return Err(failure.detail)
        case Ok(conn):
            with contextlib.closing(conn):
                return search_pg.record_handover(conn, session_id, observed_at, paths)


def _search_log_query(query: str, hits: list[dict], started: float) -> None:
    """query_log 한 줄 — label-recall 이 표본으로 읽는 표 (store.rs:2349, 열 그대로).
    실패는 stderr 한 줄로만 남긴다 — Rust spawn_query_log 의 eprintln 과 같은 자리. connect 와
    log_query 는 실패를 Either 값으로 돌려주니, 여기서 튀는 예외는 버그 — 남을 게 아니라 튄다."""
    latency_ms = int((time.monotonic() - started) * 1000)
    logged = [(hit["source_path"], hit.get("dist"), hit.get("dist_kind")) for hit in hits]
    snippet = hits[0]["snippet"][:200] if hits else ""
    match search_pg.connect(os.environ["DOOR_PG_DSN"]):
        case Err(failure):
            print(f"[door] query_log failed: {failure.detail}", file=sys.stderr, flush=True)
        case Ok(conn):
            with contextlib.closing(conn):
                row = search_pg.QueryLogRow(
                    endpoint="search",
                    query=query,
                    logged_hits=tuple(logged),
                    sources=[],
                    answer_snippet=snippet,
                    latency_ms=latency_ms,
                )
                match search_pg.log_query(conn, row):
                    case Err(failure):
                        print(
                            f"[door] query_log failed: {failure.detail}",
                            file=sys.stderr,
                            flush=True,
                        )


async def _search(request: Request) -> Response:
    """POST /search — 문이 파이썬 순위(ohmyboring.search — RRF 융합·판정 넛지·집합 안 순서·
    예산·related 그래프 확장)로 직접 답한다. 응답엔 늘 x-boring-search: python 이 붙는다.
    본문은 경계에서 _SearchBody 로 한 번 파싱한다(타입·부호·blank — 400 은 전부 거기서).
    임베딩·판정·related 읽기·handover 실패는 전부 502 JSON — drudge 가 조용히 넘어가던 자리를
    문은 보이게 접는다 (계약 divergence 목록)."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    try:
        params = _SearchBody.model_validate(body)
    except ValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not os.environ.get("DOOR_PG_DSN"):
        return JSONResponse({"error": "store not configured"}, status_code=503)
    retriever = search_retriever.PgRetriever(
        dsn=os.environ["DOOR_PG_DSN"],
        max_results=params.max_results,
        max_tokens=params.max_tokens,
        project=params.project,
        since_hours=params.since_hours,
        claims=params.claims,
        related=params.related,
        related_heads=params.related_heads,
    )
    started = time.monotonic()
    match await asyncio.to_thread(_search_execute, retriever, params.query):
        case Ok(documents):
            want_claims = params.claims > 0
            hits = [search_hits.document_to_hit(doc, claims_requested=want_claims) for doc in documents]
        case Err(failure):
            return JSONResponse({"error": f"search failed: {failure.detail}"}, status_code=502)
    session = params.session_id.strip() if params.session_id is not None else ""
    if session:
        paths = [hit["source_path"] for hit in hits]
        match await asyncio.to_thread(_search_handover, session, paths):
            case Ok(_):
                pass
            case Err(detail):
                return JSONResponse({"error": f"handover failed: {detail}"}, status_code=502)
    await asyncio.to_thread(_search_log_query, params.query, hits, started)
    return JSONResponse({"hits": hits}, headers={"x-boring-search": "python"})


for _path, _methods in _load_routes().items():
    app.add_api_route(_path, _proxy, methods=_methods)

# The door's own routes, driven by the snapshot's door_routes key. A snapshot
# entry without a native handler here is a KeyError at startup, not a proxy hole.
_DOOR_HANDLERS = {
    "GET /approved": _approved,
    "GET /claim-source": _claim_source,
    "GET /claim-sources": _claim_sources,
    "GET /projects": _projects,
    "GET /repairs/split-subjects": _repairs_split_subjects_get,
    "POST /repairs/split-subjects": _repairs_split_subjects_post,
    "POST /run/morning-card": _run_morning_card,
    "POST /run/weekly-card": _run_weekly_card,
    "POST /search": _search,
    "GET /rules": _rules,
}
for _method, _path in _load_door_routes():
    app.add_api_route(_path, _DOOR_HANDLERS[f"{_method} {_path}"], methods=[_method])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DOOR_PORT", "7710")))
