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
travels byte-for-byte. A POST /mcp tools/call `remember` (and POST /remember)
travels byte-for-byte — after the answer leaves, a background shadow runs
the same request through the python write path without writing and asks whether
python would make the same decision the engine made (PII gate block/mask, the
duplicate gate's five branches and the replace scoring, owner refusal), then —
only when a note was actually stored — compares that note field-by-field with
the note the engine wrote, leaving one remember_shadow event whose every
mismatch carries a reason that sorts it into python-defect / engine-difference /
unknown (E3a-2; a skipped request is compared by decision only — nothing was
written, so there are no fields to match; the response never waits on the shadow
and a shadow failure cannot change the answer — one log line and a status=error
event). The seven register reads get the same treatment one migration step later
(E4-1): POST /decisions·/risks·/next_actions·/stalled·/recurrences·/context·/status
and the MCP tools decisions·risks·next_actions·stalled·recurrences·context·
project_status pass through byte-for-byte as today — after the answer leaves, a
read shadow recomputes the same request through the python read path
(ohmyboring.registers, a read-only session) and compares the response field by
field (register answer text·sources·items·totals, recurrence rows, the context
card's five sections, status sources plus the empty-path message — the generated
status text itself is not compared), leaving one read_shadow event per request
whose every mismatch carries the remember vocabulary: (가) python-defect,
(나) engine-differs-from-intent (today: plan-dependent tie order),
(다) unknown (unreadable engine answer, python compute failure, or a race the
one rerun confirms); the response never waits on the shadow. The register read
switch: DOOR_REGISTER_READER=python hands these seven reads to the python read
path inside the door (default engine leaves every byte untouched; turning it on
is the orchestrator's call once production shadow numbers show 가=0·다=0). The
one switch on this behavior: DOOR_REMEMBER_WRITER=python hands the
remember decision·write·answer to the python write path inside the door
(E3c-1, ohmyboring.remember.writer — the same decision chain the shadow
validates, writing for real through a writable DOOR_PG_DSN; default engine
leaves every byte above untouched; the python path hands its two events,
dedup_decision·remember_written, to a background dispatcher in receive order so
a slow engine /events sink never stretches the write — E3c-2). The duplicate scan's vault read runs off an in-process note index
(E3b-2): prefilled in the background at startup (the log line quotes how long
the warm-up took), refreshed per write by a directory listing and stat only,
re-reading and re-parsing just the changed notes; until the prefill finishes the
shadow falls back to a full disk scan — the same decision either way, and the
event's elapsed columns (total, embedding, DB, vault, parse, nearest, event)
sum to the total — embedding covers chunk+claim embeds, DB covers connect+commit,
vault covers numbering+file write+the duplicate scan, nearest is the whole
nearest-document probe, event is the event-sink time). The recall answer gets a
shadow too (E4-2): after the answer leaves, ohmyboring.recall recomputes the same
recall (wiki first, vector only when the wiki is empty, read-only session) and
compares its text with the engine text as it was before the numbered-note blocks
were prepended, leaving one recall_shadow event (status ok|tie|mismatch|error, reader, path,
line counts, first differing line, skipped wiki files — never the query); the answer stays the engine's.
The recall read switch: DOOR_RECALL_READER=python (compose default) answers POST /mcp tools/call
recall from inside the door — same numbered-note blocks, the engine's JSON-RPC errors (-32602/-32603),
and the session_id handover written once by the python path; the shadow then runs the other way, a
background call to the engine without session_id, event reader=python (engine = rollback).
The verdict write switch: DOOR_VERDICT_WRITER=python (compose default engine) answers POST /handover, POST
/consumption and MCP tools/call verdict from inside the door (E4-5, ohmyboring.verdict) — the engine's validation
wording and status codes, response field order and counters, one DB transaction per request, the owner
supersede refusal as one owner_supersede_refused event; a body that is not a JSON object still goes to the engine
(engine = rollback). An answer whose content-type is
text/event-stream is relayed chunk by chunk as it arrives, never read to
completion first, and the upstream socket is closed when the client goes away —
an endless engine stream stays endless through the door. The parked read behind
each open stream lives on its own executor, never on asyncio's default pool
that short requests draw from. An engine answer, 4xx
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
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, StrictInt, StrictStr, ValidationError, field_validator
from slack_sdk.errors import SlackClientError

from ohmyboring import config as boring_config
from ohmyboring.adapters import embed as embed_adapter
from ohmyboring.adapters import events as event_log
from ohmyboring.adapters import vault as vault_notes
from ohmyboring.entrypoints.http import mcp_recall
from ohmyboring.gap import parse as gap_parse
from ohmyboring.gap import pg as gap_pg
from ohmyboring.recall import answer as recall_answer
from ohmyboring.recall import named as recall_named
from ohmyboring.recall import shadow as recall_shadow
from ohmyboring.recall import wiki as recall_wiki
from ohmyboring.registers import pg as registers_pg
from ohmyboring.registers import shadow as registers_shadow
from ohmyboring.remember import dedup as remember_dedup
from ohmyboring.remember import graph as remember_graph
from ohmyboring.remember import index as remember_index
from ohmyboring.remember import pii as remember_pii
from ohmyboring.remember import shadow as remember_shadow
from ohmyboring.remember import writer as remember_writer
from ohmyboring.result import Either, Err, Ok
from ohmyboring.search import hits as search_hits
from ohmyboring.search import pg as search_pg
from ohmyboring.search import retriever as search_retriever
from ohmyboring.verdict import parse as verdict_parse
from ohmyboring.verdict import run as verdict_run

from ..shared import vault_note
from . import approved, claim_source, rules

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "slack"))
import card_advice  # noqa: E402
import card_types  # noqa: E402
import card_view  # noqa: E402

_TIMEOUT = float(os.environ.get("DOOR_TIMEOUT", "130"))

# 라이브 기본 풀 22(CPU 18+4)를 MCP 스트림 22개가 다 채웠다(2026-10-02).
_STREAM_WORKERS = int(os.environ.get("DOOR_STREAM_WORKERS", "64"))
_STREAM_EXECUTOR = ThreadPoolExecutor(max_workers=_STREAM_WORKERS, thread_name_prefix="door-stream")

_CONTRACT = Path(__file__).resolve().parents[2] / "data" / "contract" / "engine-contract.json"


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """기동 시 노트 색인을 백그라운드에서 미리 채운다(E3b-2) — 문은 자주 재기동하므로
    빈 색인으로 받은 첫 쓰기가 볼트 통째 파싱(수 초)을 그대로 낼 수 없다. 채우는 동안
    들어온 그림자 쓰기는 디스크 훑기로 떨어지고(sync() → None), 그 결정이 엔진과 같은
    결정이라는 계약은 그대로다. 응답은 채우기를 기다리지 않는다."""
    index = remember_index.NoteIndex(
        remember_index.DiskSeams(
            vault_dir=_vault_dir(),
            list_notes=lambda: _list_wiki_notes(_vault_dir()),
            read_note=vault_notes.read_note,
            split_frontmatter=vault_note.split_frontmatter,
        )
    )
    threading.Thread(target=_prefill_note_index, args=(index,), name="note-index", daemon=True).start()
    app.state.note_index = index
    yield


def _prefill_note_index(index: remember_index.NoteIndex) -> None:
    index.prefill()
    if index.usable:
        print(
            f"[door] note index prefilled: {index.prefill_count} notes in {index.prefill_s:.2f}s",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            "[door] note index prefill failed — shadow scans the vault directly",
            file=sys.stderr,
            flush=True,
        )


app = FastAPI(title="oh-my-boring door", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)


def _upstream() -> str:
    return os.environ.get("DOOR_UPSTREAM", "http://127.0.0.1:7700").rstrip("/")


def _pass_through(status: int, body: bytes, content_type: str | None) -> Response:
    headers = {"content-type": content_type} if content_type is not None else {}
    return Response(content=body, status_code=status, headers=headers)


async def _stream_chunks(upstream: http.client.HTTPResponse) -> AsyncIterator[bytes]:
    loop = asyncio.get_running_loop()
    try:
        while chunk := await loop.run_in_executor(_STREAM_EXECUTOR, upstream.read1, 1024):
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


def _engine_headers(request: Request) -> dict[str, str]:
    return {
        name: request.headers[name]
        for name in ("content-type", "accept", "mcp-session-id", "x-request-id", _OWNER_TOKEN_HEADER)
        if name in request.headers
    }


async def _proxy(request: Request, background_tasks: BackgroundTasks) -> Response:
    query = request.url.query
    url = f"{_upstream()}{request.url.path}" + (f"?{query}" if query else "")
    body = await request.body()
    # 쓰기 주인 스위치(E3c-1): python 이면 remember 는 엔진에 안 넘기고 문 안에서 결정·쓰고 답한다.
    # 엔진(기본)이면 아래 분기는 통과해 바이트 그대로 프록시+그림자가 탄다.
    if request.method == "POST" and request.url.path == "/mcp" and _remember_writer() == _WRITER_PYTHON:
        if (arguments := _mcp_remember_arguments(body)) is not None:
            return await _remember_python(request, body, arguments, "mcp")
    if request.method == "POST" and request.url.path == "/remember" and _remember_writer() == _WRITER_PYTHON:
        if (arguments := _http_remember_arguments(body)) is not None:
            return await _remember_python(request, body, arguments, "remember")
    # 판정 쓰기 주인 스위치(E4-5): python 이면 /handover·/consumption·MCP verdict 를 문 안에서 쓰고 답한다.
    if request.method == "POST" and _verdict_writer() == _WRITER_PYTHON:
        if (answered := await _verdict_python(request, body)) is not None:
            return answered
    # 빈자리 도구는 스위치와 무관하게 늘 문 안에서 답한다 — 엔진엔 gap 이 없다.
    if request.method == "POST" and request.url.path == "/mcp":
        if (call := _mcp_tools_call(body)) is not None and call[0] == gap_parse.TOOL["name"]:
            rpc = _http_json_object(body) or {}
            return await _gap_python(rpc.get("id"), call[1], "mcp")
    # 읽기 주인 스위치(E4-1): python 이면 일곱 레지스터 읽기를 엔진에 넘기지 않고 문 안에서 답한다.
    if request.method == "POST" and _register_reader() == _READER_PYTHON:
        if request.url.path in _REGISTER_HTTP_PATHS:
            if (data := _http_json_object(body)) is not None:
                return await _register_python(
                    request, None, data, _REGISTER_HTTP_PATHS[request.url.path], "http"
                )
        elif request.url.path == "/mcp":
            if (call := _mcp_tools_call(body)) is not None and call[0] in _REGISTER_MCP_TOOLS:
                rpc = _http_json_object(body) or {}
                return await _register_python(
                    request, rpc.get("id"), call[1], _REGISTER_MCP_TOOLS[call[0]], "mcp"
                )
    headers = _engine_headers(request)
    # 회상 읽기 주인 스위치(E4-2): python 이면 recall 은 엔진에 안 넘기고 문 안에서 답한다.
    if request.method == "POST" and request.url.path == "/mcp" and _recall_reader() == _READER_PYTHON:
        if (call := _mcp_tools_call(body)) is not None and call[0] == "recall":
            rpc = _http_json_object(body) or {}
            return await _recall_answer(rpc, call[1], url, headers, body, background_tasks)
    try:
        response = await asyncio.to_thread(_fetch, url, body, headers, request.method)
    except OSError:
        # URLError, ConnectionRefusedError, timeout — one class: the engine is gone.
        return JSONResponse(
            {"error": "engine unreachable", "upstream": _upstream()},
            status_code=502,
        )
    if request.method == "POST" and request.url.path == "/mcp":
        engine_response = response
        response = _augment_mcp_recall(body, response)
        response = _augment_tools_list(body, response)
        # 회상 그림자는 번호 블록을 붙이기 전의 엔진 텍스트와 대조한다(E4-2).
        if not isinstance(engine_response, StreamingResponse) and (call := _mcp_tools_call(body)) is not None:
            if call[0] == "recall":
                background_tasks.add_task(
                    _recall_shadow_task, call[1], engine_response.status_code, engine_response.body
                )
        # remember 는 회상과 달리 응답을 안 고친다 — 바이트 그대로 본 뒤 그림자만 태운다(E3a-2).
        if not isinstance(response, StreamingResponse) and (arguments := _mcp_remember_arguments(body)):
            background_tasks.add_task(
                _remember_shadow_task,
                "mcp",
                arguments,
                response.status_code,
                response.body,
                _standing(request.headers.get(_OWNER_TOKEN_HEADER)) == Standing.OWNER,
            )
        # 일곱 레지스터 도구도 같은 규약 — 응답 뒤 읽기 그림자가 칸별 대조 사건을 남긴다(E4-1).
        if not isinstance(response, StreamingResponse) and (call := _mcp_tools_call(body)) is not None:
            if call[0] in _REGISTER_MCP_TOOLS:
                background_tasks.add_task(
                    _register_shadow_task,
                    _REGISTER_MCP_TOOLS[call[0]],
                    "mcp",
                    call[1],
                    response.status_code,
                    response.body,
                )
        return response
    if request.method == "POST" and request.url.path in _REGISTER_HTTP_PATHS:
        if not isinstance(response, StreamingResponse) and (data := _http_json_object(body)) is not None:
            background_tasks.add_task(
                _register_shadow_task,
                _REGISTER_HTTP_PATHS[request.url.path],
                "http",
                data,
                response.status_code,
                response.body,
            )
        return response
    if request.method == "POST" and request.url.path == "/remember":
        if not isinstance(response, StreamingResponse) and (arguments := _http_remember_arguments(body)):
            background_tasks.add_task(
                _remember_shadow_task,
                "remember",
                arguments,
                response.status_code,
                response.body,
                _standing(request.headers.get(_OWNER_TOKEN_HEADER)) == Standing.OWNER,
            )
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


def _augment_tools_list(body: bytes, response: Response) -> Response:
    """POST /mcp 의 tools/list 가 엔진 2xx application/json 이면 gap 도구를 맨 끝에 덧붙인다.
    엔진 도구는 그대로 두고, 그 밖의 모양(다른 메서드·비 2xx·JSON 아님·tools 목록 없음)은
    받은 바이트 그대로."""
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
    if not isinstance(request_json, dict) or request_json.get("method") != "tools/list":
        return response
    result = response_json.get("result") if isinstance(response_json, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return response
    result["tools"] = [*tools, gap_parse.TOOL]
    return _pass_through(
        response.status_code, json.dumps(response_json, ensure_ascii=False).encode("utf-8"), content_type
    )


def _mcp_remember_arguments(body: bytes) -> dict | None:
    """POST /mcp 본문이 tools/call remember 이면 그 인자 dict, 아니면 None.

    회상의 그림자는 회상만 건드리는 문과 달리 remember 에만 탠다 — 회상·다른 도구·
    JSON 아닌 본문은 여기서 걸러져 그림자 없이 바이트만 지나간다."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("method") != "tools/call":
        return None
    params = data.get("params")
    if not isinstance(params, dict) or params.get("name") != "remember":
        return None
    arguments = params.get("arguments")
    return arguments if isinstance(arguments, dict) else {}


def _http_remember_arguments(body: bytes) -> dict | None:
    """POST /remember 본문 자체가 인자 — JSON 객천 것만 그림자를 탠다."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _omb_session_id(arguments: dict) -> str | None:
    raw = arguments.get("omb_session_id")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _list_wiki_notes(vault_dir: str) -> list[str]:
    """볼트 wiki 디렉토리의 note_id 목록 — 그림자의 중복 문 스캔이 읽는 것(쓰기 없음)."""
    names = os.listdir(os.path.join(vault_dir, "wiki"))
    return sorted(name[: -len(".md")] for name in names if name.endswith(".md"))


#: 임베딩 갈래의 최근접 문서 질의 — drudge store.rs:2274 nearest_document 그대로(읽기 전용
#: 세션). 엔진이 방금 쓴 새 노트는 그림자가 후보에서 빼게끔 exclude 경로를 받는다.
_NEAREST_DOC_SQL = (
    "SELECT d.source_path, MIN(c.embedding <=> %(vec)s)::float8 AS dist"
    " FROM document d JOIN chunk c ON c.source_path = d.source_path"
    " WHERE c.embedding IS NOT NULL AND d.source_path <> %(exclude)s"
    " GROUP BY d.source_path"
    " HAVING MIN(c.embedding <=> %(vec)s) <= %(max)s"
    " ORDER BY dist ASC LIMIT 1"
)
_NEAREST_DOC_SQL_SELF = (
    "SELECT d.source_path, MIN(c.embedding <=> %(vec)s)::float8 AS dist"
    " FROM document d JOIN chunk c ON c.source_path = d.source_path"
    " WHERE c.embedding IS NOT NULL"
    " GROUP BY d.source_path"
    " HAVING MIN(c.embedding <=> %(vec)s) <= %(max)s"
    " ORDER BY dist ASC LIMIT 1"
)


def _nearest_document(text: str, exclude_path: str | None) -> Either[str | None, str]:
    """제목+본문을 임베딩해 코사인 거리 0.07 이하의 최근접 문서를 읽는다(쓰기 없음).

    DSN 이 없거나 임베딩 서버가 불응이면 Err — 그림자 사건이 그 사유를 남기고 결정 대조는
    (다) 로 남는다. 실패가 요청에 영향을 주는 일은 없다(응답 뒤 백그라운드다)."""
    dsn = os.environ.get("DOOR_PG_DSN")
    if not dsn:
        return Err("DOOR_PG_DSN unset — embedding branch unchecked")
    match embed_adapter.embed(text):
        case Err(failure):
            return Err(f"embed: {failure}")
        case Ok(vec):
            pass
    sql = _NEAREST_DOC_SQL if exclude_path else _NEAREST_DOC_SQL_SELF
    params = {"vec": search_pg._vector_literal(vec), "max": remember_dedup.DUPLICATE_MAX_DIST}
    if exclude_path:
        params["exclude"] = exclude_path
    try:
        with (
            psycopg.connect(dsn, options="-c default_transaction_read_only=on") as conn,
            conn.cursor() as cur,
        ):
            cur.execute(sql, params)
            row = cur.fetchone()
    except psycopg.Error as e:
        return Err(f"pg: {e}")
    return Ok(row[0] if row else None)


#: 그림자의 그래프 대조(E3b)가 읽는 세 장 — 문이 지난 노트가 실제로 남긴 간선·문서·claim
#: 행의 끝 상태다. 전부 읽기 전용 세션(임베딩 프로브와 같은 연결 모양).
_GRAPH_EDGES_SQL = (
    "SELECT e.src, e.kind, e.dst FROM edge e"
    " WHERE e.src = ANY(%(docs)s) OR e.dst = ANY(%(docs)s)"
    "    OR e.src IN (SELECT dst FROM edge WHERE src = ANY(%(docs)s) AND kind = 'claims')"
)
_GRAPH_DOCUMENTS_SQL = "SELECT source_path FROM document WHERE source_path = ANY(%(paths)s)"
_GRAPH_CLAIMS_SQL = (
    "SELECT source_path, subject, predicate, kind, superseded_at IS NOT NULL"
    " FROM claim WHERE source_path = ANY(%(paths)s)"
)


def _read_graph(paths: tuple[str, ...]) -> remember_graph.GraphSnapshot | None:
    """그림자가 정한 문서 경로들의 그래프 끝 상태를 읽는다 — 읽기 전용, 쓰기 없음.

    DSN 부재·pg 고장이면 None — 그림자 사건이 graph-unchecked (다) 로 남긴다. 임베딩
    프로브와 마찬가지로 응답 뒤 백그라운드라, 실패가 요청에 닿는 일은 없다."""
    dsn = os.environ.get("DOOR_PG_DSN")
    if not dsn:
        return None
    docs = [f"doc:{path}" for path in paths]
    try:
        with (
            psycopg.connect(dsn, options="-c default_transaction_read_only=on") as conn,
            conn.cursor() as cur,
        ):
            cur.execute(_GRAPH_EDGES_SQL, {"docs": docs})
            edges = frozenset((row[0], row[1], row[2]) for row in cur.fetchall())
            cur.execute(_GRAPH_DOCUMENTS_SQL, {"paths": list(paths)})
            documents = frozenset(row[0] for row in cur.fetchall())
            cur.execute(_GRAPH_CLAIMS_SQL, {"paths": list(paths)})
            claims = tuple(
                remember_graph.ClaimRow(
                    source_path=row[0],
                    subject=row[1],
                    predicate=row[2],
                    kind=row[3],
                    sealed=row[4],
                )
                for row in cur.fetchall()
            )
    except psycopg.Error as e:
        print(f"[door] remember_shadow graph read failed: {e}", file=sys.stderr, flush=True)
        return None
    return remember_graph.GraphSnapshot(edges=edges, documents=documents, claims=claims)


def _remember_shadow_task(route: str, arguments: dict, status: int, body: bytes, is_owner: bool) -> None:
    """응답을 본 뒤에 도는 그림자 — 같은 요청을 파이썬 쓰기 경로에 「쓰지 않고」 태워
    엔진이 한 결정(PII 게이트·중복 문의 걸러둘·대체·저장, owner 자격 거절)과 같은 결정을
    낼지, 저장·대체에서는 실제 노트와 칸별로, 읽기 전용 세션으로 실제 그래프의 간선·claim
    봉인까지 대조하고 사건(remember_shadow) 한 줄을 남긴다(E3a-2, E3b). 걸러진
    요청은 결정만 비교한다. 실패는 전부 여기서 접는다: 문 로그 한 줄 + 사건 status=error —
    응답은 이미 간 뒤라 어떤 예외도 클라이언트에 닿지 않는다."""
    vault_dir = _vault_dir()
    match remember_pii.load_from_vault(vault_dir):
        case Err(reason):
            event_log.try_append_event(
                "door",
                remember_shadow.EVENT_NAME,
                "error",
                reason=f"pii rules: {reason}",
                omb_session_id=_omb_session_id(arguments),
            )
            return
        case Ok(scanner):
            pass
    try:
        event = remember_shadow.run_shadow(
            remember_shadow.ShadowRequest(
                route=route,
                arguments=arguments,
                engine_status=status,
                engine_body=body,
                vault_dir=vault_dir,
                read_note=vault_notes.read_note,
                split_frontmatter=vault_note.split_frontmatter,
                list_notes=lambda: _list_wiki_notes(vault_dir),
                pii_scanner=scanner,
                is_owner=is_owner,
                nearest_document=_nearest_document,
                read_graph=_read_graph,
                note_index=getattr(app.state, "note_index", None),
            )
        )
    except Exception as e:  # noqa: BLE001 — 그림자는 응답 뒤라, 어떤 예외든 이 한 자리에서 접는다
        print(
            f"[door] remember_shadow failed: {type(e).__name__}: {e}",
            file=sys.stderr,
            flush=True,
        )
        event_log.try_append_event(
            "door",
            remember_shadow.EVENT_NAME,
            "error",
            reason=f"{type(e).__name__}: {e}",
            omb_session_id=_omb_session_id(arguments),
        )
        return
    event_log.try_append_event(
        "door", remember_shadow.EVENT_NAME, event.status, **remember_shadow.event_payload(event)
    )


#: remember 쓰기 주인 스위치(E3c-1) — engine(기본)이면 프록시+그림자 그대로, python 이면
#: 문 안의 파이썬 쓰기 길(ohmyboring.remember.writer)이 결정·쓰고 답한다. 운영 기본은
#: engine — 이 조각에서 켜지 않는다(켜기는 오케스트레이터가 검증 뒤에 한다).
_REMEMBER_WRITER_ENV = "DOOR_REMEMBER_WRITER"
_WRITER_PYTHON = "python"

#: 파이썬 쓰기 길의 사건 전달 — dedup_decision·remember_written 을 응답을 기다리지 않게
#: 자료 순서대로 백그라운드에서 싣는다(E3c-2). 쓰기 안의 사건 기록은 싱크가 느려도(엔진이
#: 바쁘면 /events 가 0.5s 한도까지 미뤄진다) 쓰기 시간에 안 잡히게 — 칸은 여전히 사건
#: 칸에 재며, 기록 자체의 실패 규약은 그대로다(싱크가 거부하면 spool 으로 떨어진다).
_EVENT_SINK_LOCK = threading.Lock()
_EVENT_SINK_QUEUE: queue.Queue | None = None


def _enqueue_writer_event(component: str, event: str, status: str, **fields: Any) -> bool:
    """사건 한 건을 자료 큐에 넣고 즉시 돌아온다 — 전달은 스레드 한 개가 순서대로 한다."""
    global _EVENT_SINK_QUEUE
    with _EVENT_SINK_LOCK:
        if _EVENT_SINK_QUEUE is None:
            _EVENT_SINK_QUEUE = queue.Queue()
            threading.Thread(target=_dispatch_writer_events, name="event-sink", daemon=True).start()
    _EVENT_SINK_QUEUE.put((component, event, status, fields))
    return True


def _dispatch_writer_events() -> None:
    """자료 큐의 사건들을 받은 순서대로 진짜 싱크에 싣는다 — 실패는 싱크 몫(스레드는 산다)."""
    assert _EVENT_SINK_QUEUE is not None
    while True:
        component, event, status, fields = _EVENT_SINK_QUEUE.get()
        try:
            event_log.try_append_event(component, event, status, **fields)
        except Exception as e:  # noqa: BLE001 — try_append_event 이 못 잡는 고장도 큐를 막지 않는다
            print(f"[door] writer event not dispatched ({event}): {e}", file=sys.stderr, flush=True)


def _remember_writer() -> str:
    """쓰기 주인 — python 만 켬, 그 밖의 값·부재는 전부 engine(실수로 켜지지 않게)."""
    return _WRITER_PYTHON if os.environ.get(_REMEMBER_WRITER_ENV) == _WRITER_PYTHON else "engine"


async def _remember_python(request: Request, body: bytes, arguments: dict, route: str) -> Response:
    """스위치 켬 — remember 를 엔진에 넘기지 않고 파이썬이 결정·쓰고 엔진 답 모양으로 답한다.

    쓰기 길은 응답을 기다리는 주 요청 안에서 돈다(그림자와 달리). 사건 기록 실패는 응답을
    바꾸지 않는다 — 그림자와 같은 규약. PII 규칙을 못 읽으면 쓰지 않고 닫는다(엔진도 규칙
    깨진 볼트에서는 서빙을 못 선다 — serve.rs:1351)."""
    if not os.environ.get("DOOR_PG_DSN"):
        return JSONResponse({"error": "store not configured"}, status_code=503)
    vault_dir = _vault_dir()
    match remember_pii.load_from_vault(vault_dir):
        case Err(reason):
            print(f"[door] remember writer: pii rules unreadable: {reason}", file=sys.stderr, flush=True)
            outcome: remember_writer.WriteOutcome = remember_writer.Refused(-32603, f"pii rules: {reason}")
        case Ok(scanner):
            deps = _writer_deps(request, vault_dir, scanner)
            try:
                outcome = await asyncio.to_thread(
                    remember_writer.run_write,
                    remember_writer.WriteRequest(
                        route=route, arguments=arguments, omb_session_id=_omb_session_id(arguments)
                    ),
                    deps,
                )
            except Exception as e:  # noqa: BLE001 — 예상 못한 고장도 응답은 준다(엔진 -32603 과 같이)
                print(f"[door] remember writer failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                outcome = remember_writer.Refused(-32603, f"{type(e).__name__}: {e}")
    return _remember_outcome_response(body, route, outcome)


def _writer_deps(
    request: Request, vault_dir: str, scanner: remember_pii.PiiScanner | None
) -> remember_writer.WriterDeps:
    """쓰기 길에 싣는 면 — 문의 진짜(쓰기 가능 DSN·어댑터·색인). 사건 기록은 자료 큐에
    넣는 껍질로 싣는다 — 싱크가 느려도 쓰기 시간이 불지 않게(E3c-2, 문서 위 함수 참조).
    시험은 문 경계를 목으로 둔다."""
    return remember_writer.WriterDeps(
        vault_dir=vault_dir,
        connect=lambda: psycopg.connect(os.environ["DOOR_PG_DSN"]),
        read_note=vault_notes.read_note,
        split_frontmatter=vault_note.split_frontmatter,
        list_notes=lambda: _list_wiki_notes(vault_dir),
        pii_scanner=scanner,
        is_owner=_standing(request.headers.get(_OWNER_TOKEN_HEADER)) == Standing.OWNER,
        nearest_document=_nearest_document,
        embed=remember_writer.embed_via_adapter,
        append_event=_enqueue_writer_event,
        note_index=getattr(app.state, "note_index", None),
        embed_dim=boring_config.embed_dim(),
    )


def _remember_outcome_response(body: bytes, route: str, outcome: remember_writer.WriteOutcome) -> Response:
    """쓰기 길의 값을 엔진 답 모양으로 입힌다 — MCP 는 JSON-RPC 봉투(HTTP 200), HTTP 는
    RememberResp 다섯 칸(400/500 은 -32602/-32603 — serve/http.rs:682-704)."""
    request_id = None
    if route == "mcp":
        try:
            request_id = json.loads(body).get("id")  # 본문은 이미 tools/call remember 로 본 것
        except (ValueError, UnicodeDecodeError, AttributeError):
            request_id = None
    if isinstance(outcome, remember_writer.Written):
        if route == "mcp":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": outcome.message}], "isError": False},
                }
            )
        return JSONResponse(
            {
                "source_path": outcome.source_path,
                "wiki_id": outcome.wiki_id,
                "duplicate": outcome.duplicate,
                "supersedes": outcome.supersedes,
                "unknown": outcome.unknown,
            }
        )
    if route == "mcp":
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": outcome.code, "message": outcome.message},
            }
        )
    return JSONResponse({"error": outcome.message}, status_code=400 if outcome.code == -32602 else 500)


#: 판정 쓰기 주인 스위치(E4-5) — engine(기본)이면 프록시 그대로, python 이면 문 안의 판정 쓰기 길
#: (ohmyboring.verdict)이 POST /handover·POST /consumption·MCP tools/call verdict 를 한 트랜잭션으로 쓰고
#: 엔진 답 모양으로 답한다. 운영 기본은 engine — 켜기는 사본 DB 대조(scripts/verdict-parity.py) 뒤 오케스트레이터가 한다.
_VERDICT_WRITER_ENV = "DOOR_VERDICT_WRITER"
_VERDICT_HTTP_PATHS = ("/consumption", "/handover")


def _verdict_route(path: str) -> tuple[Any, Any]:
    """HTTP 경로 → (본문 파서, 쓰기 길) — 부를 때 풀어서 시험이 목으로 바꿔 끼울 수 있다."""
    match path:
        case "/consumption":
            return verdict_parse.parse_consumption, verdict_run.consume
        case _:
            return verdict_parse.parse_handover, verdict_run.hand_over


def _verdict_writer() -> str:
    """판정 쓰기 주인 — python 만 켬, 그 밖의 값·부재는 전부 engine(실수로 켜지지 않게)."""
    return _WRITER_PYTHON if os.environ.get(_VERDICT_WRITER_ENV) == _WRITER_PYTHON else "engine"


def _verdict_deps(request: Request) -> verdict_run.Deps:
    dsn = os.environ.get("DOOR_PG_DSN")
    return verdict_run.Deps(
        connect=(lambda: psycopg.connect(dsn)) if dsn else None,
        append_event=_enqueue_writer_event,
        is_owner=_standing(request.headers.get(_OWNER_TOKEN_HEADER)) == Standing.OWNER,
        now=lambda: datetime.now(tz=UTC).isoformat(),
    )


async def _verdict_run(
    run: Any, req: Any, request: Request
) -> Either[Any, verdict_parse.Rejected | verdict_run.Failed | verdict_run.StoreOff]:
    """쓰기 길 한 바퀴 — 예상 못한 고장도 Failed 값으로(엔진 500 / -32603 과 같이)."""
    try:
        return await asyncio.to_thread(run, req, _verdict_deps(request))
    except Exception as e:  # noqa: BLE001 — 한 자리에서 값으로 접는다
        print(f"[door] verdict writer failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return Err(verdict_run.Failed(f"{type(e).__name__}: {e}"))


def _verdict_http_failure(
    failure: verdict_parse.Rejected | verdict_run.Failed | verdict_run.StoreOff,
) -> Response:
    match failure:
        case verdict_parse.Rejected(message, status):
            return JSONResponse({"error": message}, status_code=status)
        case verdict_run.Failed(message) | verdict_run.StoreOff(message):
            return JSONResponse({"error": message}, status_code=500)


async def _verdict_http(path: str, data: dict, request: Request) -> Response:
    parse, run = _verdict_route(path)
    match parse(data):
        case Err(rejected):
            return _verdict_http_failure(rejected)
        case Ok(req):
            pass
    match await _verdict_run(run, req, request):
        case Err(failure):
            return _verdict_http_failure(failure)
        case Ok(payload):
            return JSONResponse(payload)


async def _verdict_mcp(request_id: Any, arguments: dict, request: Request) -> Response:
    match verdict_parse.parse_verdict_call(arguments):
        case Err(rejected):
            return _mcp_error(request_id, -32602, rejected.message)
        case Ok(call):
            pass
    match await _verdict_run(verdict_run.verdict, call, request):
        case Err(verdict_run.StoreOff(message)):
            return _mcp_error(request_id, -32603, message)
        case Err(failure):
            return _mcp_error(request_id, -32603, f"verdict: {failure.message}")
        case Ok(payload):
            return _mcp_result(payload, request_id)


async def _verdict_python(request: Request, body: bytes) -> Response | None:
    """스위치 켬 — 이 요청이 판정 쓰기 면이면 문이 답하고, 아니면 None(그대로 프록시 길로).

    본문이 JSON 객체가 아니면 None: 엔진이 자기 말로 거절한다(레지스터·remember 스위치와 같은 규약)."""
    path = request.url.path
    if path == "/mcp":
        if (call := _mcp_tools_call(body)) is None or call[0] != "verdict":
            return None
        rpc = _http_json_object(body) or {}
        return await _verdict_mcp(rpc.get("id"), call[1], request)
    if path not in _VERDICT_HTTP_PATHS or (data := _http_json_object(body)) is None:
        return None
    return await _verdict_http(path, data, request)


#: ── E4-1 일곱 레지스터 읽기 — 읽기 그림자 + 읽기 주인 스위치 ────────────────
#: HTTP 경로 표면 → 표면 이름 / MCP 도구 이름 → 표면 이름. 문서(docstring) 위에도 쓴다.
_REGISTER_HTTP_PATHS = {
    "/decisions": "decisions",
    "/risks": "risks",
    "/next_actions": "next_actions",
    "/stalled": "stalled",
    "/recurrences": "recurrences",
    "/context": "context",
    "/status": "status",
}
_REGISTER_MCP_TOOLS = {
    "decisions": "decisions",
    "risks": "risks",
    "next_actions": "next_actions",
    "stalled": "stalled",
    "recurrences": "recurrences",
    "context": "context",
    "project_status": "status",
}

#: 읽기 주인 스위치(E4-1) — engine(기본)이면 프록시+그림자 그대로, python 이면 문 안의 읽기
#: 길(ohmyboring.registers)이 답한다. 켜기는 오케스트레이터가 운영 그림자 숫자를 보고 한다.
_REGISTER_READER_ENV = "DOOR_REGISTER_READER"
_READER_PYTHON = "python"


def _register_reader() -> str:
    """읽기 주인 — python 만 켬, 그 밖의 값·부재는 전부 engine(실수로 켜지지 않게)."""
    return _READER_PYTHON if os.environ.get(_REGISTER_READER_ENV) == _READER_PYTHON else "engine"


def _http_json_object(body: bytes) -> dict | None:
    """POST 본문이 JSON 객천 것만 — 레지스터 그림자·스위치가 다루는 인자의 모양."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _mcp_tools_call(body: bytes) -> tuple[str, dict] | None:
    """POST /mcp 본문이 tools/call 이면 (도구 이름, 인자 dict), 아니면 None.

    인자가 객체가 아니면 빈 dict — 엔진은 as_str/as_u64 로 필드를 읽으니 비객체 인자도
    기본값으로 답하고, 파이썬 길의 mcp_args 가 똑같이 받는다."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("method") != "tools/call":
        return None
    params = data.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return None
    arguments = params.get("arguments")
    return params["name"], arguments if isinstance(arguments, dict) else {}


def _policy_origins() -> tuple[str, ...]:
    """엔진 config.rs origins_excluded_by_policy — allow_company_origin 이면 빈 집합."""
    return () if boring_config.load().get("allow_company_origin") else ("company",)


def _embed_text(text: str):
    match embed_adapter.embed(text):
        case Ok(vec):
            return Ok(vec)
        case Err(failure):
            return Err(str(failure))


def _register_answer(args: dict, surface: str, transport: str) -> Either[dict, str]:
    """읽기 길 한 바퀴 — 읽기 전용 연결(그림자·켬 응답 길 같음). 실패는 message 값으로."""
    try:
        conn = psycopg.connect(os.environ["DOOR_PG_DSN"], options="-c default_transaction_read_only=on")
    except psycopg.Error as e:
        return Err(f"pg connect: {e}")
    with conn:
        ctx = registers_pg.AnswerCtx(
            policy_origins=_policy_origins(),
            lang=boring_config.note_lang(),
            embed=_embed_text,
            transport=transport,
        )
        return registers_pg.answer(conn, surface, args, ctx)


def _mcp_result(payload: dict, request_id: Any) -> Response:
    """Structured 도구 답 봉투 — 엔진 ToolOut::Structured 모양(mcp.rs:494-509)."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "structuredContent": payload,
                "isError": False,
            },
        }
    )


def _mcp_error(request_id: Any, code: int, message: str) -> Response:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _register_rejection(request_id: Any, transport: str, message: str) -> Response:
    """인자 거절 — HTTP 는 400(serde 422 계열은 422), MCP 는 -32602 봉투(HTTP 200)."""
    if transport == "mcp":
        return _mcp_error(request_id, -32602, message)
    status = 422 if message == "unprocessable entity" else 400
    return JSONResponse({"error": message}, status_code=status)


def _register_failure(request_id: Any, transport: str, message: str) -> Response:
    """계산 고장 — HTTP 는 500, MCP 는 -32603 봉투(엔진의 map_err 계열과 같다)."""
    if transport == "mcp":
        return _mcp_error(request_id, -32603, message)
    return JSONResponse({"error": message}, status_code=500)


async def _register_python(
    request: Request, request_id: Any, arguments: dict, surface: str, transport: str
) -> Response:
    """스위치 켬 — 일곱 읽기를 엔진에 넘기지 않고 문 안의 읽기 길로 답한다.

    켬 응답 길은 주 요청 안에서 돈다(그림자와 달리). 사건 기록은 없다 — 켬에서는 비교할
    엔진 답이 없고, 답 자체는 읽기라 기록할 사건 이며: 응답은 엔진 모양 그대로 준다."""
    if not os.environ.get("DOOR_PG_DSN"):
        return JSONResponse({"error": "store not configured"}, status_code=503)
    parse = registers_pg.http_args if transport == "http" else registers_pg.mcp_args
    match parse(surface, arguments):
        case Err(rejected):
            return _register_rejection(request_id, transport, rejected.message)
        case Ok(args):
            pass
    try:
        result = await asyncio.to_thread(_register_answer, args, surface, transport)
    except Exception as e:  # noqa: BLE001 — 예상 못한 고장도 엔진 -32603/500 과 같이
        print(f"[door] register reader failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        result = Err(f"{type(e).__name__}: {e}")
    match result:
        case Err(message):
            return _register_failure(request_id, transport, message)
        case Ok(payload):
            pass
    if transport == "mcp":
        return _mcp_result(payload, request_id)
    return JSONResponse(payload)


def _register_shadow_query(
    surface: str, transport: str, arguments: dict
) -> Either[dict, registers_shadow.PyFailure]:
    """그림자의 파이썬 길 — 인자 거절은 대조 가능한 거절로, 계산 고장은 (다) 로 남긴다."""
    parse = registers_pg.http_args if transport == "http" else registers_pg.mcp_args
    match parse(surface, arguments):
        case Err(rejected):
            return Err(registers_shadow.PyFailure(rejected.message, rejection=True))
        case Ok(args):
            pass
    match _register_answer(args, surface, transport):
        case Err(message):
            return Err(registers_shadow.PyFailure(message, rejection=False))
        case Ok(payload):
            return Ok(payload)


def _register_shadow_task(surface: str, transport: str, arguments: dict, status: int, body: bytes) -> None:
    """읽기 그림자(E4-1) — 응답을 본 뒤 같은 요청을 읽기 전용으로 다시 계산해 엔진 답과
    칸별로 대조하고 read_shadow 사건 한 줄을 남긴다. 실패는 전부 여기서 접는다: 문 로그
    한 줄 + 사건 status=error — 응답은 이미 간 뒤라 어떤 예외도 클라이언트에 닿지 않는다."""
    try:
        event = registers_shadow.run_shadow(
            registers_shadow.ShadowRequest(
                surface=surface,
                transport=transport,
                arguments=arguments,
                engine_status=status,
                engine_body=body,
                query=lambda: _register_shadow_query(surface, transport, arguments),
                rerun=lambda: _register_shadow_query(surface, transport, arguments),
            )
        )
    except Exception as e:  # noqa: BLE001 — 그림자는 응답 뒤라, 어떤 예외든 이 한 자리에서 접는다
        print(f"[door] read_shadow failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        event_log.try_append_event(
            "door",
            registers_shadow.EVENT_NAME,
            "error",
            surface=surface,
            transport=transport,
            reason=f"{type(e).__name__}: {e}",
        )
        return
    event_log.try_append_event(
        "door",
        registers_shadow.EVENT_NAME,
        event.status,
        surface=surface,
        **registers_shadow.event_payload(event),
    )


_RECALL_WIKI_INDEX = recall_wiki.WikiIndex(vault_note.split_frontmatter)


def _recall_python(arguments: dict) -> recall_shadow.PythonAnswer:
    """그림자의 파이썬 길 — 읽기 전용 연결, 볼트는 엔진과 같은 /vault/wiki. 연결 못 정하면 값으로."""
    dsn = os.environ.get("DOOR_PG_DSN")
    if not dsn:
        return Err(recall_answer.Failed("DOOR_PG_DSN not set"))
    return recall_answer.run(
        arguments,
        dsn=dsn,
        wiki_dir=Path(_vault_dir()) / "wiki",
        index=_RECALL_WIKI_INDEX,
        now_ns=time.time_ns(),
    )


def _recall_shadow_task(arguments: dict, status: int, body: bytes, reader: str = "engine") -> None:
    """회상 그림자(E4-2) — 응답이 간 뒤 같은 인자로 파이썬 회상을 계산해 엔진 텍스트와 대조하고
    recall_shadow 사건 한 줄을 남긴다. 예외는 여기서 문 로그 한 줄 + 사건 status=error 로 접는다."""
    try:
        event = recall_shadow.run_shadow(status, body, lambda: _recall_python(arguments))
    except Exception as e:  # noqa: BLE001 — 그림자는 응답 뒤라, 어떤 예외든 이 한 자리에서 접는다
        print(f"[door] recall_shadow failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        event_log.try_append_event(
            "door", recall_shadow.EVENT_NAME, "error", reader=reader, reason=f"{type(e).__name__}: {e}"
        )
        return
    event_log.try_append_event(
        "door", recall_shadow.EVENT_NAME, event.status, reader=reader, **recall_shadow.event_payload(event)
    )


_RECALL_READER_ENV = "DOOR_RECALL_READER"


def _recall_reader() -> str:
    """회상 읽기 주인 — python 만 켬, 그 밖의 값·부재는 전부 engine(실수로 켜지지 않게)."""
    return _READER_PYTHON if os.environ.get(_RECALL_READER_ENV) == _READER_PYTHON else "engine"


def _recall_session_id(arguments: dict) -> str | None:
    session = arguments.get("session_id")
    return session.strip() or None if isinstance(session, str) else None


def _recall_handover(session_id: str, recalled: recall_answer.Recalled) -> None:
    """엔진 mcp_recall 과 같은 정책 — 쓰기 실패는 문 로그 한 줄이고 회상은 성공한다."""
    if not recalled.shown:
        return
    match _search_handover(session_id, list(recalled.shown)):
        case Err(detail):
            print(
                f"[door] recall handover for session {session_id} failed: {detail}",
                file=sys.stderr,
                flush=True,
            )
        case Ok(_):
            pass


def _recall_answer_text(arguments: dict) -> recall_shadow.PythonAnswer:
    """파이썬 회상 한 번 + (session_id 가 오면) handover 한 벌. 예상 못한 고장은 Failed 값으로."""
    try:
        answered = _recall_python(arguments)
        session = _recall_session_id(arguments)
        match answered:
            case Ok(recalled) if session is not None:
                _recall_handover(session, recalled)
            case _:
                pass
    except Exception as e:  # noqa: BLE001 — 엔진 -32603 과 같이 한 자리에서 값으로 접는다
        print(f"[door] recall reader failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return Err(recall_answer.Failed(f"{type(e).__name__}: {e}"))
    return answered


def _recall_without_session(body: bytes) -> bytes:
    """엔진 그림자 호출 본문 — handover 는 파이썬 길이 이미 썼으니 session_id 를 뺀다(한 벌)."""
    rpc = json.loads(body)
    rpc["params"]["arguments"] = {k: v for k, v in rpc["params"]["arguments"].items() if k != "session_id"}
    return json.dumps(rpc, ensure_ascii=False).encode("utf-8")


def _recall_reverse_shadow_task(
    url: str, headers: dict[str, str], body: bytes, answered: recall_shadow.PythonAnswer
) -> None:
    """켠 동안의 그림자 — 파이썬이 답한 뒤 같은 호출을 엔진에 보내 텍스트를 대조하고
    recall_shadow 사건(reader=python)을 남긴다. 엔진 불통·예외는 문 로그 한 줄 + status=error."""
    try:
        engine = _fetch(url, _recall_without_session(body), headers, "POST")
        event = recall_shadow.compare(engine.status_code, engine.body, answered)
    except OSError as e:
        print(f"[door] recall_shadow engine unreachable: {e}", file=sys.stderr, flush=True)
        event_log.try_append_event(
            "door", recall_shadow.EVENT_NAME, "error", reader=_READER_PYTHON, reason="engine unreachable"
        )
        return
    except Exception as e:  # noqa: BLE001 — 그림자는 응답 뒤라, 어떤 예외든 이 한 자리에서 접는다
        print(f"[door] recall_shadow failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        event_log.try_append_event(
            "door",
            recall_shadow.EVENT_NAME,
            "error",
            reader=_READER_PYTHON,
            reason=f"{type(e).__name__}: {e}",
        )
        return
    event_log.try_append_event(
        "door",
        recall_shadow.EVENT_NAME,
        event.status,
        reader=_READER_PYTHON,
        **recall_shadow.event_payload(event),
    )


async def _recall_answer(
    rpc: dict,
    arguments: dict,
    url: str,
    headers: dict[str, str],
    body: bytes,
    background_tasks: BackgroundTasks,
) -> Response:
    """스위치 켬 — recall 을 엔진에 넘기지 않고 문 안에서 답한다. 번호 노트 블록은 엔진 답과
    같은 함수(_augment_mcp_recall)를 지난다. 답 뒤 배경에서 엔진에 같은 호출을 보내 대조한다."""
    answered = await asyncio.to_thread(_recall_answer_text, arguments)
    background_tasks.add_task(_recall_reverse_shadow_task, url, headers, body, answered)
    request_id = rpc.get("id")
    match answered:
        case Err(recall_answer.Rejected(message)):
            return _mcp_error(request_id, -32602, message)
        case Err(recall_answer.Failed(detail)):
            return _mcp_error(request_id, -32603, detail)
        case Ok(recalled):
            envelope = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": recalled.text}], "isError": False},
            }
            response = _pass_through(
                200, json.dumps(envelope, ensure_ascii=False).encode("utf-8"), "application/json"
            )
            return _augment_mcp_recall(body, response)


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
        return await _proxy(request, BackgroundTasks())
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

#: The programs the run routes execute — the same tools launchd used to fire, unchanged,
#: plus the 이름 맞추기 판정기: 판정과 합치기는 다른 실행이라 카드와 같은 {ok, exit, tail}
#: 모양으로 문이 돌린다.
_CARD_PROGRAMS = {
    "morning-card": Path(__file__).resolve().parents[2] / "agents" / "slack" / "card.py",
    "weekly-card": Path(__file__).resolve().parents[2] / "agents" / "slack" / "weekly_card.py",
    "repair-judge": Path(__file__).resolve().parents[2] / "agents" / "slack" / "card_repair_judge.py",
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


async def _run_repair_judge(request: Request) -> Response:
    return await _run_card_route(request, "repair-judge")


async def _run_weekly_card(request: Request) -> Response:
    return await _run_card_route(request, "weekly-card")


#: repair candidates per page — 3 default (the card's "오늘 할 일" shows the top 3), 1..50 range
#: (0 is not a page, an unbounded scan is not a "top N" any more).
_MIN_REPAIR_LIMIT = 1
_MAX_REPAIR_LIMIT = 50
_DEFAULT_REPAIR_LIMIT = 3

_SAMPLE_LIMIT = 5
_SAMPLE_MAX_VARIANTS = 10
_SAMPLE_VALUE_CHARS = 160
_SPLIT_SAMPLES_SQL = (
    "select predicate, count(*), (array_agg(left(value, %s) order by length(value) desc, value))[1] "
    "from claim where subject = any(%s) "
    "group by predicate order by 2 desc, 1 limit %s"
)


def _fetch_split_samples(variants: list[str]) -> list[tuple[str, int, str]]:
    with _split_subjects_connect() as conn, conn.cursor() as cur:
        cur.execute(_SPLIT_SAMPLES_SQL, (_SAMPLE_VALUE_CHARS, variants, _SAMPLE_LIMIT))
        return cur.fetchall()


async def _repairs_split_subjects_samples(request: Request) -> Response:
    variants = [v for v in request.query_params.getlist("variant") if v.strip()]
    if not 1 <= len(variants) <= _SAMPLE_MAX_VARIANTS:
        return JSONResponse(
            {"error": f"variant must be given 1..{_SAMPLE_MAX_VARIANTS} times"}, status_code=400
        )
    if not os.environ.get("DOOR_PG_DSN"):
        return JSONResponse({"error": "store not configured"}, status_code=503)
    try:
        rows = await asyncio.to_thread(_fetch_split_samples, variants)
    except psycopg.OperationalError as e:
        return JSONResponse({"error": "store unreachable", "detail": str(e)}, status_code=502)
    return JSONResponse({"samples": [{"predicate": p, "rows": n, "value": v} for p, n, v in rows]})


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


def _gap_record(args: gap_parse.GapArgs) -> Either[gap_pg.GapReport, str]:
    """The seam the tests stub — 연결·쓰기 실패는 Err 값 하나로."""
    observed_at = datetime.now(tz=UTC).isoformat()
    match search_pg.connect(os.environ["DOOR_PG_DSN"]):
        case Err(failure):
            return Err(failure.detail)
        case Ok(conn):
            with contextlib.closing(conn):
                match gap_pg.record_gap(conn, args, observed_at):
                    case Err(failure):
                        return Err(failure.detail)
                    case Ok(report):
                        return Ok(report)


async def _gap_python(request_id: Any, arguments: dict, transport: str) -> Response:
    """빈자리 한 건 기록 — HTTP·MCP 가 같은 인자·같은 저장 호출. 거절 400/-32602, 저장 실패 502/-32603."""
    if not os.environ.get("DOOR_PG_DSN"):
        if transport == "mcp":
            return _mcp_error(request_id, -32603, "store not configured")
        return JSONResponse({"error": "store not configured"}, status_code=503)
    match gap_parse.parse(arguments):
        case Err(rejected):
            return _register_rejection(request_id, transport, rejected.message)
        case Ok(args):
            pass
    match await asyncio.to_thread(_gap_record, args):
        case Err(detail):
            message = f"gap failed: {detail}"
            if transport == "mcp":
                return _mcp_error(request_id, -32603, message)
            return JSONResponse({"error": message}, status_code=502)
        case Ok(report):
            pass
    if transport == "mcp":
        return _mcp_result(report.payload(), request_id)
    return JSONResponse(report.payload())


async def _gap(request: Request) -> Response:
    """POST /gap — 본문은 JSON 객체여야 한다."""
    data = _http_json_object(await request.body())
    if data is None:
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    return await _gap_python(None, data, "http")


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
    "GET /repairs/split-subjects/samples": _repairs_split_subjects_samples,
    "POST /gap": _gap,
    "POST /repairs/split-subjects": _repairs_split_subjects_post,
    "POST /run/morning-card": _run_morning_card,
    "POST /run/repair-judge": _run_repair_judge,
    "POST /run/weekly-card": _run_weekly_card,
    "POST /search": _search,
    "GET /rules": _rules,
}
for _method, _path in _load_door_routes():
    app.add_api_route(_path, _DOOR_HANDLERS[f"{_method} {_path}"], methods=[_method])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DOOR_PORT", "7710")))
