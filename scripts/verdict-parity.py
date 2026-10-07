#!/usr/bin/env python3
"""판정 쓰기 대조 — 같은 씨앗의 사본 DB 두 벌에 같은 요청 묶음을 엔진(A)과 문의 파이썬 길(B)로 치고 비교한다.

E4-5a 계약 §② (가). 켜기(E4-5b) 전에 한 번 돌린다 — CI 가 아니다.

  A  엔진(:77xx)이 사본 A 를 물고 있다 — 요청은 엔진에 곧장(HTTP /handover·/consumption, MCP verdict).
  B  이 스크립트가 문(agents/door/door.py)을 같은 프로세스 안에서 DOOR_VERDICT_WRITER=python 으로
     띄워(FastAPI TestClient) 사본 B 에 같은 묶음을 친다. 파이썬 길의 사건(owner_supersede_refused)은
     문의 사건 싱크로 나가므로 --sink-url-b 에 사본 B 를 문 엔진 하나가 받아 줘야 event_log 에 쌓인다.

운영 DB·운영 엔진·운영 문은 모른다. 시작에 두 DSN 이 운영 DSN 과 다름을 단언하고 그 줄을 찍는다(2026-09-04
간선 5,222건 사고) — 단언이 깨지면 아무것도 치지 않고 종료코드 2.

요청 묶음은 운영 호출의 흔적에서 뽑는다. 이 세 면에는 요청 로그가 없고, 사본이 운영 스냅숏이라 그 간선이
곧 호출 모양이다 — 사본 A 의 session 간선을 세션별로 묶어 모양마다 최대 --sample 건(기본 5)을 결정적으로
(md5 순) 뽑아 다시 친다:
  handed/NULL                → POST /handover        (세션이 건넨 경로 그대로)
  used·contested/inferred    → POST /consumption judge=inferred (그 세션의 used·contested 목록 그대로)
  used·contested/NULL        → 건넨 간선이 있는 세션에 verdict-only POST /consumption 과 MCP verdict
  used·contested/owner       → POST /consumption judge=owner + 오너 토큰
  supersedes doc→doc         → [newer, older] 쌍과 그 간선의 judge 로 POST /consumption
  + 같은 요청 반복 · 검증 400 · 비오너의 오너 노트 supersedes(거절 사건 한 줄 — 사본의 실제 오너 노트를 겨눈
    파생 요청) · 중간 실패.
다시 칠 때는 새 세션 id(parity-<모양><번호>)로 친다 — 사본에 이미 있는 세션의 간선이면 두 번째 쓰기가 전부
ON CONFLICT 로 무동작이 돼 쓰기 길을 못 잰다. verdict-only 는 새 세션에 같은 경로를 먼저 건네 둔다. supersedes
쌍은 간선이 쌍 열쇠라 그대로 다시 쳐 「이미 있는 간선도 센다」 경로를 잰다. 지어낸 고정 묶음은
--fixture 로만(기본 꺼짐, 경로를 사본 document 에서 읽는다):
  handover recall_core.hand_over · card_effects._live_handover / consumption distill_core(judge=inferred) ·
  memory.retriever.record_verdict · card_effects._live_consumption(owner + 토큰) / mcp.verdict.

대조: node·edge·claim·event_log 행 수와 내용 + 응답(상태·바이트). 원래 다른 칸은 이름으로 뺀다 —
  node.label            observed_at(MCP verdict 는 now())
  edge.first_seen_at    칸 기본값 now()
  claim.superseded_at   새 노트가 claim 이 없을 때의 now() 대체 분기 → 묶음 시작 이후 값은 "NOW" 로 접는다
  event_log.id·observed_at·time_unix_nano   기본값·시각
  event_log 의 나머지는 (component, event_name, status, severity_text, door, targets) 만 본다.
면별 요청 건수를 따로 찍고 한 면이라도 0 건이면 PASS 를 안 낸다(그림자 0 은 쳐 본 칸의 0).

중간 실패는 묶음과 따로 친다: 두 사본에 같은 트리거(경로 하나에서 간선 쓰기를 터뜨림)를 임시로 깔고
엔진은 반쯤 쓰고(자동 커밋) 파이썬은 0 행이어야 한다 — 의도한 차이(한 요청 = 한 트랜잭션)라 따로 적는다.
422(serde 칸 종류) 본문은 엔진이 axum 평문이고 문은 JSON 이라 바이트가 다르다 — 상태만 대조하고 「expected
difference」로 따로 적는다. 실패 행(HTTP 500 {"error"} · MCP -32603)은 상태·`error` 칸 존재만 대조하고 메시지
글자는 뺀다(psycopg 대 sqlx 문구) — 검증 400 과 성공 행은 바이트 그대로다.

종료코드: 0 PASS · 1 차이 있음 · 2 잰 게 아니다(DSN 단언·씨앗 불일치·장비 불통).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OWNER_TOKEN_HEADER = "x-boring-owner-token"
GHOST = "/vault/wiki/__parity_ghost__.md"
BOOM = "/vault/wiki/__parity_boom__.md"
SURFACES = ("handover", "consumption", "mcp.verdict")
CORE_SHAPES = (
    "handed/NULL",
    "used·contested/inferred",
    "used·contested/NULL",
    "used·contested/owner",
    "supersedes",
)
NOW_MARK = "NOW"
OBSERVED_AT = "2026-10-07T01:02:03+09:00"
PRODUCTION_DSN_ENVS = ("DOOR_PG_DSN", "PG_DSN", "BORING_PG_DSN", "BORING_DATABASE_URL")
#: .env.example · docker-compose.yml 이 적은 운영 DSN — 환경변수가 없어도 이 모양은 운영이다.
PRODUCTION_DSN_DEFAULTS = (
    "postgresql://boring:boring@127.0.0.1:5432/boring",
    "postgresql://boring:boring@boring-postgres:5432/boring",
)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "", "boring-postgres"}

EXCLUDED_COLUMNS = (
    "node.label",
    "edge.first_seen_at",
    "claim.superseded_at (now() 대체 분기)",
    "event_log.id",
    "event_log.observed_at",
    "event_log.time_unix_nano",
    "응답 error 메시지 글자 (실패 행만: HTTP 5xx · MCP -32603)",
)


# ── 운영 DSN 단언 (순수) ────────────────────────────────────────────────────


def dsn_identity(dsn: str) -> tuple[str, str, str]:
    """(host, port, dbname) — 비밀번호·사용자는 신분에 안 든다. 로컬 별칭은 한 호스트로 접는다."""
    from psycopg.conninfo import conninfo_to_dict

    parts = conninfo_to_dict(dsn)
    host = str(parts.get("host") or "")
    host = "localhost" if host in LOCAL_HOSTS else host
    return host, str(parts.get("port") or "5432"), str(parts.get("dbname") or parts.get("user") or "")


def production_identities(env: dict[str, str]) -> set[tuple[str, str, str]]:
    candidates = [env[name] for name in PRODUCTION_DSN_ENVS if env.get(name)] + list(PRODUCTION_DSN_DEFAULTS)
    return {dsn_identity(dsn) for dsn in candidates}


def assert_scratch(dsn_a: str, dsn_b: str, env: dict[str, str]) -> tuple[bool, str]:
    """두 DSN 이 운영이 아니고 서로 다른 사본인지 — (통과?, 찍을 한 줄). 줄에 비밀번호는 안 든다."""
    a, b = dsn_identity(dsn_a), dsn_identity(dsn_b)
    production = production_identities(env)

    def show(ident: tuple[str, str, str]) -> str:
        return f"{ident[0]}:{ident[1]}/{ident[2]}"

    named = ", ".join(sorted(show(p) for p in production))
    if a in production or b in production:
        return False, f"DSN guard FAIL: A={show(a)} B={show(b)} is production ({named})"
    if a == b:
        return False, f"DSN guard FAIL: A and B are the same database ({show(a)})"
    return True, f"DSN guard: A={show(a)} B={show(b)} — neither equals production ({named}), A != B — ok"


# ── 정규화·대조 (순수) ──────────────────────────────────────────────────────


def normalize_claim(row: tuple, batch_start) -> tuple:
    """(subject, predicate, value, source_path, valid_from, kind, superseded_at) — 묶음 시작 이후에 찍힌
    superseded_at 은 now() 대체 분기일 수 있어 "NOW" 로 접는다(새 노트 claim 의 valid_from 은 과거다)."""
    *head, superseded_at = row
    folded = NOW_MARK if superseded_at is not None and superseded_at >= batch_start else superseded_at
    return (*head, folded)


def normalize_event(row: tuple) -> tuple:
    """(component, event_name, status, severity_text, door, targets-json) — id·시각 칸은 이미 안 읽는다."""
    component, event_name, status, severity_text, door, targets = row
    return (component, event_name, status, severity_text, door, json.dumps(targets, sort_keys=True))


def diff_rows(a: list, b: list) -> tuple[list, list]:
    """다중집합 차이 — (A 에만, B 에만). 같은 행이 두 번 있으면 두 번으로 센다."""
    only_a = Counter(a) - Counter(b)
    only_b = Counter(b) - Counter(a)
    return sorted(only_a.elements(), key=repr), sorted(only_b.elements(), key=repr)


@dataclass(frozen=True)
class Docs:
    owner_old: str
    owner_new: str
    old_a: str
    old_b: str
    new_a: str
    new_b: str


def pick_docs(rows: list[tuple[str, str, int]]) -> Docs | None:
    """(source_path, author, 현재 claim 수) → 묶음이 쓸 문서 여섯. 모자라면 None(씨앗이 작다).

    옛 노트는 현재 claim 이 있는 것을 먼저 골라 봉인이 실제로 움직이게 한다. 경로순이라 두 사본에서 같다."""
    owners = [r for r in rows if r[1] == "owner"]
    plain = [r for r in rows if r[1] != "owner"]
    with_claims = [r for r in plain if r[2] > 0]
    without = [r for r in plain if r[2] == 0]
    olds = (with_claims + without)[:2]
    news = [r for r in plain if r[0] not in {o[0] for o in olds}]
    if len(owners) < 2 or len(olds) < 2 or len(news) < 2:
        return None
    return Docs(owners[0][0], owners[1][0], olds[0][0], olds[1][0], news[0][0], news[1][0])


@dataclass(frozen=True)
class Req:
    surface: str
    label: str
    path: str
    body: dict
    owner: bool = False
    shape: str = ""


def build_batch(d: Docs) -> list[Req]:
    """세 면의 실제 호출 모양 — 위 docstring 의 출처. 같은 요청 반복과 검증 400 포함."""
    handover = Req(
        "handover",
        "recall hand_over",
        "/handover",
        {"session_id": "parity-s1", "observed_at": OBSERVED_AT, "paths": [d.old_a, d.old_b, GHOST]},
    )
    inferred = Req(
        "consumption",
        "distill inferred + supersedes",
        "/consumption",
        {
            "session_id": "parity-s2",
            "observed_at": OBSERVED_AT,
            "judge": "inferred",
            "used": [d.old_a],
            "contested": [d.old_b],
            "supersedes": [[d.new_a, d.old_a]],
        },
    )
    return [
        handover,
        handover,
        Req(
            "consumption",
            "retriever verdict-only (padded)",
            "/consumption",
            {"session_id": "parity-s1", "observed_at": OBSERVED_AT, "verdict": " used ", "judge": "inferred"},
        ),
        inferred,
        inferred,
        Req(
            "consumption",
            "card button owner + owner supersedes",
            "/consumption",
            {
                "session_id": "parity-s3",
                "observed_at": OBSERVED_AT,
                "judge": "owner",
                "used": [d.new_b],
                "supersedes": [[d.owner_new, d.owner_old]],
            },
            owner=True,
        ),
        Req(
            "consumption",
            "non-owner supersedes owner note (refused)",
            "/consumption",
            {
                "session_id": "parity-s4",
                "observed_at": OBSERVED_AT,
                "contested": [d.new_b],
                "supersedes": [[d.new_b, d.owner_old], [d.new_b, d.old_b]],
            },
        ),
        Req("mcp.verdict", "verdict used", "/mcp", {"session_id": "parity-s1", "verdict": "used"}),
        Req("mcp.verdict", "verdict contested", "/mcp", {"session_id": "parity-s1", "verdict": "contested"}),
        Req(
            "mcp.verdict", "verdict unknown session", "/mcp", {"session_id": "parity-none", "verdict": "used"}
        ),
        Req("mcp.verdict", "verdict missing argument", "/mcp", {"session_id": "parity-s1"}),
        Req(
            "consumption",
            "400 verdict + paths",
            "/consumption",
            {"session_id": "parity-s5", "observed_at": OBSERVED_AT, "verdict": "used", "used": [d.old_a]},
        ),
        Req(
            "consumption",
            "400 observed_at",
            "/consumption",
            {"session_id": "parity-s5", "observed_at": "yesterday", "used": [d.old_a]},
        ),
        Req(
            "consumption",
            "400 judge owner without token",
            "/consumption",
            {"session_id": "parity-s5", "observed_at": OBSERVED_AT, "judge": "owner", "used": [d.old_a]},
        ),
        Req(
            "consumption",
            "400 too many paths",
            "/consumption",
            {"session_id": "parity-s5", "observed_at": OBSERVED_AT, "contested": [d.old_a] * 201},
        ),
        Req(
            "handover",
            "400 blank session",
            "/handover",
            {"session_id": " ", "observed_at": OBSERVED_AT, "paths": [d.old_a]},
        ),
    ]


def midway_request(first: str, last: str) -> Req:
    """경로 하나(BOOM)에서 간선 쓰기가 터지는 요청 — 앞의 first 간선과 session 노드는 이미 쓰였다."""
    return Req(
        "consumption",
        "mid-way failure",
        "/consumption",
        {
            "session_id": "parity-midway",
            "observed_at": OBSERVED_AT,
            "judge": "inferred",
            "used": [first, BOOM, last],
        },
        shape="mid-way",
    )


# ── 운영 간선에서 뽑은 묶음 ─────────────────────────────────────────────────

MAX_LIST = 200  # 엔진 상한 — 더 긴 세션 목록은 앞 200 개만 다시 친다


@dataclass(frozen=True)
class Shapes:
    """사본 A 의 간선에서 읽은 호출 모양 — 세션 id 는 원본, 경로는 `doc:` 를 뗀 source_path."""

    handed: tuple[tuple[str, tuple[str, ...]], ...] = ()
    inferred: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = ()
    verdict_only: tuple[tuple[str, str, tuple[str, ...]], ...] = ()  # (세션, used|contested, 건넨 경로)
    owner: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = ()
    supersedes: tuple[tuple[str, str, str | None], ...] = ()  # (newer, older, judge)


@dataclass(frozen=True)
class Derived:
    owner_doc: str
    plain_a: str
    plain_b: str


def pick_derived(rows: list[tuple[str, str, int]]) -> Derived | None:
    """파생 요청(오너 노트 거절·검증 400·중간 실패)이 겨눌 실제 문서 — 오너 하나, 비오너 둘."""
    owners = [r[0] for r in rows if r[1] == "owner"]
    plain = [r[0] for r in rows if r[1] != "owner"]
    return Derived(owners[0], plain[0], plain[1]) if owners and len(plain) >= 2 else None


def _paths(items: tuple[str, ...]) -> list[str]:
    return list(items[:MAX_LIST])


def _stamp(body: dict) -> dict:
    return {"observed_at": OBSERVED_AT, **body}


def _shape_requests(shapes: Shapes) -> list[Req]:
    reqs: list[Req] = []
    for i, (_, paths) in enumerate(shapes.handed):
        body = {"session_id": f"parity-h{i}", "paths": _paths(paths)}
        reqs.append(Req("handover", f"handed/NULL #{i}", "/handover", _stamp(body), shape="handed/NULL"))
    for i, (_, used, contested) in enumerate(shapes.inferred):
        body = {
            "session_id": f"parity-i{i}",
            "judge": "inferred",
            "used": _paths(used),
            "contested": _paths(contested),
        }
        reqs.append(
            Req(
                "consumption", f"inferred #{i}", "/consumption", _stamp(body), shape="used·contested/inferred"
            )
        )
    for i, (_, verdict, handed) in enumerate(shapes.verdict_only):
        for suffix, surface, path in (("c", "consumption", "/consumption"), ("m", "mcp.verdict", "/mcp")):
            sid = f"parity-v{i}{suffix}"
            hand = {"session_id": sid, "paths": _paths(handed)}
            reqs.append(
                Req(
                    "handover",
                    f"verdict-only #{i}{suffix} hand",
                    "/handover",
                    _stamp(hand),
                    shape="handed/NULL",
                )
            )
            body = {"session_id": sid, "verdict": verdict}
            body = _stamp(body) if surface == "consumption" else body
            reqs.append(Req(surface, f"verdict-only #{i}{suffix}", path, body, shape="used·contested/NULL"))
    for i, (_, used, contested) in enumerate(shapes.owner):
        body = {
            "session_id": f"parity-o{i}",
            "judge": "owner",
            "used": _paths(used),
            "contested": _paths(contested),
        }
        reqs.append(
            Req(
                "consumption",
                f"owner #{i}",
                "/consumption",
                _stamp(body),
                owner=True,
                shape="used·contested/owner",
            )
        )
    for i, (newer, older, judge) in enumerate(shapes.supersedes):
        body = {
            "session_id": f"parity-p{i}",
            "supersedes": [[newer, older]],
            **({"judge": judge} if judge else {}),
        }
        reqs.append(
            Req(
                "consumption",
                f"supersedes #{i}",
                "/consumption",
                _stamp(body),
                owner=judge == "owner",
                shape="supersedes",
            )
        )
    return reqs


def _derived_requests(d: Derived) -> list[Req]:
    base = {"observed_at": OBSERVED_AT}
    return [
        Req(
            "consumption",
            "non-owner supersedes owner note (refused)",
            "/consumption",
            {
                **base,
                "session_id": "parity-d1",
                "contested": [d.plain_b],
                "supersedes": [[d.plain_b, d.owner_doc], [d.plain_b, d.plain_a]],
            },
            shape="derived",
        ),
        Req(
            "consumption",
            "400 verdict + paths",
            "/consumption",
            {**base, "session_id": "parity-d2", "verdict": "used", "used": [d.plain_a]},
            shape="derived",
        ),
        Req(
            "consumption",
            "400 observed_at",
            "/consumption",
            {"session_id": "parity-d2", "observed_at": "yesterday", "used": [d.plain_a]},
            shape="derived",
        ),
        Req(
            "consumption",
            "400 judge owner without token",
            "/consumption",
            {**base, "session_id": "parity-d2", "judge": "owner", "used": [d.plain_a]},
            shape="derived",
        ),
        Req(
            "consumption",
            "400 too many paths",
            "/consumption",
            {**base, "session_id": "parity-d2", "contested": [d.plain_a] * 201},
            shape="derived",
        ),
        Req(
            "handover",
            "400 blank session",
            "/handover",
            {**base, "session_id": " ", "paths": [d.plain_a]},
            shape="derived",
        ),
        Req("mcp.verdict", "400 missing argument", "/mcp", {"session_id": "parity-d2"}, shape="derived"),
    ]


def build_batch_from_shapes(shapes: Shapes, d: Derived) -> list[Req]:
    """운영 간선 모양 + 파생 요청. 첫 요청을 한 번 더 쳐 「같은 요청 반복」을 넣는다(시도 수 카운터)."""
    reqs = _shape_requests(shapes) + _derived_requests(d)
    repeat = next((r for r in reqs if r.shape in ("used·contested/inferred", "handed/NULL")), None)
    return reqs + ([repeat] if repeat is not None else [])


def shape_counts(requests: list[Req]) -> dict[str, int]:
    return dict(Counter(r.shape or "fixture" for r in requests))


def failure_kind(answer: Answer) -> tuple | None:
    """실패 행의 모양 — (http, status) 또는 (rpc, code). 실패가 아니면 None. 메시지 글자는 안 본다."""
    try:
        data = json.loads(answer.body)
    except ValueError:
        return None
    if answer.status >= 500 and isinstance(data, dict) and "error" in data:
        return ("http", answer.status)
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and error.get("code") == -32603:
        return ("rpc", -32603)
    return None


def answers_match(engine: Answer, python: Answer) -> bool:
    """성공·검증 400 은 바이트 그대로, 양쪽이 모두 실패 행이면 모양(상태·error 칸)만."""
    kinds = (failure_kind(engine), failure_kind(python))
    return kinds[0] == kinds[1] if all(kinds) else engine == python


def as_rpc(req: Req) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "verdict", "arguments": req.body},
    }


def wire_body(req: Req) -> dict:
    return as_rpc(req) if req.surface == "mcp.verdict" else req.body


@dataclass(frozen=True)
class Answer:
    status: int
    body: bytes


def surface_counts(requests: list[Req]) -> dict[str, int]:
    counts = Counter(r.surface for r in requests)
    return {s: counts.get(s, 0) for s in SURFACES}


@dataclass(frozen=True)
class Midway:
    engine_status: int
    python_status: int
    engine_rows: int
    python_rows: int


@dataclass
class Result:
    counts: dict[str, int]
    answered: dict[str, int]
    shapes: dict[str, int] = field(default_factory=dict)
    response_diffs: list[tuple[str, Answer, Answer]] = field(default_factory=list)
    table_diffs: dict[str, tuple[list, list]] = field(default_factory=dict)
    midway: Midway | None = None


def decide(result: Result) -> tuple[bool, list[str]]:
    """PASS 는 면별 건수가 모두 >0 이고 응답·표 차이가 0 이며 중간 실패가 의도한 모양일 때만."""
    problems: list[str] = []
    for surface, count in result.counts.items():
        if count == 0 or result.answered.get(surface, 0) == 0:
            problems.append(f"surface {surface}: 0 requests answered — a 0 is not a measurement")
    problems += [f"response differs: {label}" for label, _, _ in result.response_diffs]
    problems += [
        f"table differs: {name} (A-only {len(a)}, B-only {len(b)})"
        for name, (a, b) in result.table_diffs.items()
        if a or b
    ]
    mid = result.midway
    if mid is None:
        problems.append("mid-way failure not exercised")
    else:
        if mid.engine_status < 500 or mid.python_status < 500:
            problems.append(
                f"mid-way failure did not fail on both sides (engine {mid.engine_status}, python {mid.python_status})"
            )
        if mid.engine_rows == 0:
            problems.append("mid-way failure left no partial rows on the engine — the injection did not bite")
        if mid.python_rows != 0:
            problems.append(
                f"python path left {mid.python_rows} rows after a mid-way failure (one request = one transaction)"
            )
    return not problems, problems


# ── 장비: DB·엔진·문 ────────────────────────────────────────────────────────

SNAPSHOT_SQL = {
    "node": "SELECT id, kind, outcome FROM node",
    "edge": "SELECT src, dst, kind, judge FROM edge",
    "claim": (
        "SELECT subject, predicate, value, source_path, valid_from::text, kind, superseded_at FROM claim"
    ),
}
EVENT_SQL = (
    "SELECT component, event_name, status, severity_text, attributes->>'door', attributes->'targets'"
    " FROM event_log WHERE id > %s"
)
BOOM_SQL = (
    "CREATE OR REPLACE FUNCTION parity_boom() RETURNS trigger LANGUAGE plpgsql AS $$"
    f" BEGIN IF NEW.dst = 'doc:{BOOM}' THEN RAISE EXCEPTION 'parity boom'; END IF; RETURN NEW; END $$;"
)


class Unmeasured(Exception):
    """잰 게 아니다 — 종료코드 2."""


def snapshot(dsn: str, event_after: int, batch_start) -> dict[str, list]:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        out: dict[str, list] = {}
        for name, sql in SNAPSHOT_SQL.items():
            cur.execute(sql)
            rows = [tuple(r) for r in cur.fetchall()]
            out[name] = [normalize_claim(r, batch_start) for r in rows] if name == "claim" else rows
        cur.execute(EVENT_SQL, (event_after,))
        out["event_log"] = [normalize_event(tuple(r)) for r in cur.fetchall()]
        return out


def marks(dsn: str) -> tuple[int, object]:
    """(event_log 의 최대 id, 이 DB 의 now()) — 묶음 시작점."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0), now() FROM event_log")
        row = cur.fetchone()
        return int(row[0]), row[1]


#: 봉인·승격이 실제로 행을 옮기는 옛 노트 x — 사실 칸을 두 노트(x 가 현재, y 가 봉인됨)만 갖고 둘 다
#: 비오너·은퇴 전. x 를 은퇴시키면 x 의 현재 행이 봉인되고 y 의 행이 승격된다.
_PROMOTE_PAIR_SQL = (
    "WITH two AS (SELECT subject, predicate FROM claim WHERE kind = 'fact'"
    "             GROUP BY 1, 2 HAVING count(DISTINCT source_path) = 2),"
    " pair AS (SELECT cx.source_path AS x, cy.source_path AS y FROM two"
    "          JOIN claim cx ON (cx.subject, cx.predicate) = (two.subject, two.predicate)"
    "               AND cx.kind = 'fact' AND cx.superseded_at IS NULL"
    "          JOIN claim cy ON (cy.subject, cy.predicate) = (two.subject, two.predicate)"
    "               AND cy.source_path <> cx.source_path AND cy.superseded_at IS NOT NULL)"
    " SELECT x, y FROM pair"
    " JOIN document dx ON dx.source_path = x AND dx.author <> 'owner'"
    " JOIN document dy ON dy.source_path = y AND dy.author <> 'owner'"
    " WHERE NOT EXISTS (SELECT 1 FROM edge e WHERE e.kind = 'supersedes' AND e.dst IN ('doc:' || x, 'doc:' || y))"
    " ORDER BY md5(x || y) LIMIT 1"
)


def read_promote_pair(dsn: str) -> tuple[str, str] | None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(_PROMOTE_PAIR_SQL)
        row = cur.fetchone()
        return (row[0], row[1]) if row else None


def promote_request(newer: str, older: str) -> Req:
    return Req(
        "consumption",
        "fresh supersede (seal + promote move rows)",
        "/consumption",
        {"observed_at": OBSERVED_AT, "session_id": "parity-sp", "supersedes": [[newer, older]]},
        shape="derived",
    )


def read_docs(dsn: str) -> list[tuple[str, str, int]]:
    import psycopg

    sql = (
        "SELECT d.source_path, d.author,"
        " (SELECT count(*) FROM claim c WHERE c.source_path = d.source_path AND c.superseded_at IS NULL)"
        " FROM document d ORDER BY d.source_path"
    )
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return [(r[0], r[1], int(r[2])) for r in cur.fetchall()]


_SESSION_SAMPLE_SQL = (
    "SELECT src FROM edge WHERE src LIKE 'session:%%' AND kind = ANY(%s) AND {judge}"
    " GROUP BY src ORDER BY md5(src) LIMIT %s"
)
_JUDGE_CLAUSES = {
    "inferred": "judge = 'inferred'",
    "owner": "judge = 'owner'",
    "null": "judge IS NULL AND EXISTS (SELECT 1 FROM edge h WHERE h.src = edge.src AND h.kind = 'handed')",
}


def _strip(dst: str) -> str:
    return dst.removeprefix("doc:")


def _edge_lists(
    cur, sessions: list[str], kinds: list[str], judge_clause: str
) -> dict[str, dict[str, tuple[str, ...]]]:
    cur.execute(
        "SELECT src, kind, array_agg(DISTINCT dst ORDER BY dst) FROM edge"
        f" WHERE src = ANY(%s) AND kind = ANY(%s) AND {judge_clause} GROUP BY src, kind",
        (sessions, kinds),
    )
    out: dict[str, dict[str, tuple[str, ...]]] = {}
    for src, kind, dsts in cur.fetchall():
        out.setdefault(src, {})[kind] = tuple(_strip(d) for d in dsts)
    return out


def read_shapes(dsn: str, sample: int) -> Shapes:
    """사본 A 의 간선을 세션별로 묶어 모양마다 최대 sample 건(md5 순 — 두 번 읽어도 같다). 읽기 전용."""
    import psycopg

    judged = ["used", "contested"]
    with psycopg.connect(dsn, autocommit=True, options="-c default_transaction_read_only=on") as conn:
        with conn.cursor() as cur:
            cur.execute(_SESSION_SAMPLE_SQL.format(judge="judge IS NULL"), (["handed"], sample))
            handed_ids = [r[0] for r in cur.fetchall()]
            handed = _edge_lists(cur, handed_ids, ["handed"], "judge IS NULL")

            def sampled(key: str) -> dict[str, dict[str, tuple[str, ...]]]:
                clause = _JUDGE_CLAUSES[key]
                cur.execute(_SESSION_SAMPLE_SQL.format(judge=clause), (judged, sample))
                ids = [r[0] for r in cur.fetchall()]
                return _edge_lists(cur, ids, judged, clause.split(" AND ")[0])

            inferred, owner = sampled("inferred"), sampled("owner")
            null_judged = sampled("null")
            null_handed = _edge_lists(cur, sorted(null_judged), ["handed"], "judge IS NULL")
            cur.execute(
                "SELECT src, dst, judge FROM edge WHERE kind = 'supersedes' ORDER BY md5(src || dst) LIMIT %s",
                (sample,),
            )
            pairs = [(_strip(s), _strip(d), j) for s, d, j in cur.fetchall()]

    def listed(group: dict[str, dict[str, tuple[str, ...]]]) -> tuple:
        return tuple((s, g.get("used", ()), g.get("contested", ())) for s, g in sorted(group.items()))

    return Shapes(
        handed=tuple((s, g["handed"]) for s, g in sorted(handed.items())),
        inferred=listed(inferred),
        verdict_only=tuple(
            (s, "used" if "used" in g else "contested", null_handed[s]["handed"])
            for s, g in sorted(null_judged.items())
            if s in null_handed
        ),
        owner=listed(owner),
        supersedes=tuple(pairs),
    )


@contextlib.contextmanager
def boom_installed(dsns: tuple[str, str]):
    """두 사본에 같은 문서 한 줄과 간선 트리거를 임시로 깐다 — 끝에 지운다."""
    import psycopg

    def run(dsn: str, statements: list[str]) -> None:
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)

    install = [
        f"INSERT INTO document (source_path) VALUES ('{BOOM}') ON CONFLICT DO NOTHING",
        BOOM_SQL,
        "DROP TRIGGER IF EXISTS parity_boom ON edge",
        "CREATE TRIGGER parity_boom BEFORE INSERT ON edge FOR EACH ROW EXECUTE FUNCTION parity_boom()",
    ]
    remove = ["DROP TRIGGER IF EXISTS parity_boom ON edge", "DROP FUNCTION IF EXISTS parity_boom()"]
    for dsn in dsns:
        run(dsn, install)
    try:
        yield
    finally:
        for dsn in dsns:
            run(dsn, remove + [f"DELETE FROM document WHERE source_path = '{BOOM}'"])


def engine_answer(base: str, req: Req, token: str) -> Answer:
    headers = {"content-type": "application/json", "accept": "application/json"}
    if req.owner:
        headers[OWNER_TOKEN_HEADER] = token
    http = urllib.request.Request(
        base.rstrip("/") + req.path, data=json.dumps(wire_body(req)).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(http, timeout=60) as r:
            return Answer(r.status, r.read())
    except urllib.error.HTTPError as e:
        return Answer(e.code, e.read())
    except OSError as e:
        raise Unmeasured(f"engine {base} unreachable: {e}") from e


def door_answer(client, req: Req, token: str) -> Answer:
    headers = {OWNER_TOKEN_HEADER: token} if req.owner else {}
    r = client.post(req.path, json=wire_body(req), headers=headers)
    return Answer(r.status_code, r.content)


def open_door(dsn_b: str, sink_url_b: str, token: str, event_log_path: str):
    """문을 같은 프로세스에 — 사본 B 만 쓰고, 사건은 --sink-url-b 로, 스풀은 임시 파일로."""
    os.environ.update(
        DOOR_VERDICT_WRITER="python",
        DOOR_PG_DSN=dsn_b,
        BORING_OWNER_TOKEN=token,
        BORING_EVENT_SINK="db",
        BORING_EVENT_SINK_URL=sink_url_b,
        BORING_EVENT_SINK_TIMEOUT="5",
        BORING_EVENT_LOG=event_log_path,
    )
    for path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
        if path not in sys.path:
            sys.path.insert(0, path)
    import importlib

    from fastapi.testclient import TestClient

    door = importlib.import_module("agents.door.door")
    return TestClient(door.app)


def wait_for_events(dsn: str, after: int, want: int, timeout: float) -> int:
    """문의 사건은 백그라운드 큐로 나간다 — A 가 낸 개수에 닿거나 시간이 다할 때까지 B 를 읽는다."""
    import psycopg

    deadline = time.monotonic() + timeout
    seen = 0
    while time.monotonic() < deadline:
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM event_log WHERE id > %s", (after,))
            seen = int(cur.fetchone()[0])
        if seen >= want:
            return seen
        time.sleep(0.2)
    return seen


def row_total(snap: dict[str, list]) -> int:
    return sum(len(snap[name]) for name in ("node", "edge"))


def run_parity(args) -> tuple[Result, list[str]]:
    notes: list[str] = []
    rows_a, rows_b = read_docs(args.dsn_a), read_docs(args.dsn_b)
    if rows_a != rows_b:
        raise Unmeasured(
            "seed copies differ: document set (path, author, current claims) is not the same on A and B"
        )
    batch, midway_paths = batch_for(args, rows_a)
    event_a, start_a = marks(args.dsn_a)
    event_b, start_b = marks(args.dsn_b)
    before_a, before_b = snapshot(args.dsn_a, event_a, start_a), snapshot(args.dsn_b, event_b, start_b)
    if any(Counter(before_a[name]) != Counter(before_b[name]) for name in before_a):
        raise Unmeasured("seed copies differ before the batch: node/edge/claim rows are not identical")
    result = Result(
        counts=surface_counts(batch), answered=dict.fromkeys(SURFACES, 0), shapes=shape_counts(batch)
    )
    with tempfile.TemporaryDirectory() as tmp:
        client = open_door(args.dsn_b, args.sink_url_b, args.owner_token, str(Path(tmp) / "events.ndjson"))
        for req in batch:
            engine = engine_answer(args.engine_url, req, args.owner_token)
            python = door_answer(client, req, args.owner_token)
            result.answered[req.surface] += 1
            if not answers_match(engine, python):
                result.response_diffs.append((req.label, engine, python))
        after_a = snapshot(args.dsn_a, event_a, start_a)
        wait_for_events(args.dsn_b, event_b, len(after_a["event_log"]), args.event_wait)
        after_b = snapshot(args.dsn_b, event_b, start_b)
        result.table_diffs = {name: diff_rows(after_a[name], after_b[name]) for name in after_a}
        result.midway = run_midway(
            args, client, midway_paths, (event_a, start_a), (event_b, start_b), (after_a, after_b)
        )
        probe = Req(
            "consumption",
            "422 serde",
            "/consumption",
            {"session_id": "parity-s6", "observed_at": OBSERVED_AT, "used": "x"},
        )
        engine, python = (
            engine_answer(args.engine_url, probe, args.owner_token),
            door_answer(client, probe, args.owner_token),
        )
        notes.append(
            f"expected difference (422 serde, status-only compare, status {'same' if engine.status == python.status else 'DIFFERENT'}): engine {engine.status} {engine.body[:70]!r} / python {python.status} {python.body[:70]!r}"
        )
    return result, notes


def batch_for(args, rows: list[tuple[str, str, int]]) -> tuple[list[Req], tuple[str, str]]:
    """(묶음, 중간 실패가 쓸 두 문서). 기본은 사본 A 의 간선에서, --fixture 면 고정 묶음."""
    if args.fixture:
        docs = pick_docs(rows)
        if docs is None:
            raise Unmeasured("seed copy too small: need 2 owner notes and 4 non-owner notes")
        return build_batch(docs), (docs.old_a, docs.old_b)
    derived = pick_derived(rows)
    if derived is None:
        raise Unmeasured("seed copy too small: need 1 owner note and 2 non-owner notes")
    pair = read_promote_pair(args.dsn_a)
    if pair is None:
        raise Unmeasured("seed copy has no fact slot where a fresh supersede would seal and promote rows")
    older, reopened = pair
    newer = next(p for p in (derived.plain_b, derived.plain_a) if p not in pair)
    print(f"seal+promote probe: {newer} supersedes {older} (seals its current fact rows, reopens {reopened})")
    shapes = read_shapes(args.dsn_a, args.sample)
    batch = build_batch_from_shapes(shapes, derived) + [promote_request(newer, older)]
    return batch, (derived.plain_a, derived.plain_b)


def run_midway(args, client, paths, mark_a, mark_b, bases) -> Midway:
    base_a, base_b = bases
    req = midway_request(*paths)
    with boom_installed((args.dsn_a, args.dsn_b)):
        engine = engine_answer(args.engine_url, req, args.owner_token)
        python = door_answer(client, req, args.owner_token)
    end_a, end_b = snapshot(args.dsn_a, *mark_a), snapshot(args.dsn_b, *mark_b)
    return Midway(
        engine.status,
        python.status,
        row_total(end_a) - row_total(base_a),
        row_total(end_b) - row_total(base_b),
    )


def render(result: Result, notes: list[str]) -> list[str]:
    lines = [
        "surface counts (requests built / answered on both sides): "
        + ", ".join(f"{s}={result.counts[s]}/{result.answered[s]}" for s in SURFACES),
        "shape counts (requests): "
        + ", ".join(f"{name}={n}" for name, n in sorted(result.shapes.items()))
        + "".join(f", {name}=0 (none in copy A)" for name in CORE_SHAPES if name not in result.shapes),
        "replayed under fresh session ids (parity-<shape><n>) so sessions already in copy A never turn a "
        "replay into an ON CONFLICT no-op; supersedes pairs replay as-is (edge exists → attempt counted)",
        "excluded by name: " + "; ".join(EXCLUDED_COLUMNS),
    ]
    for name, (only_a, only_b) in result.table_diffs.items():
        lines.append(
            f"{name}: A-only {len(only_a)} B-only {len(only_b)} {'same' if not only_a and not only_b else 'DIFFERENT'}"
        )
        lines += [f"  A-only {r!r}" for r in only_a[:5]] + [f"  B-only {r!r}" for r in only_b[:5]]
    for label, engine, python in result.response_diffs:
        lines.append(
            f"response DIFFERENT [{label}]: engine {engine.status} {engine.body[:120]!r} / python {python.status} {python.body[:120]!r}"
        )
    if not result.response_diffs:
        lines.append(f"responses: {sum(result.answered.values())} compared byte for byte, 0 differ")
    mid = result.midway
    if mid is not None:
        lines.append(
            f"expected difference (mid-way failure, one request = one transaction): engine {mid.engine_status} left "
            f"{mid.engine_rows} node+edge rows (partial, auto-commit) / python {mid.python_status} left {mid.python_rows} rows"
        )
    return lines + notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="판정 쓰기 대조 — 사본 DB 두 벌에 같은 요청 묶음을 엔진(A)과 문의 파이썬 길(B)로 쳐서 비교한다 (E4-5a).",
        epilog="실행 예: python3 scripts/verdict-parity.py --dsn-a postgresql://…/parity_a --dsn-b postgresql://…/parity_b "
        "--engine-url http://127.0.0.1:7791 --sink-url-b http://127.0.0.1:7792/events",
    )
    ap.add_argument("--dsn-a", required=True, help="사본 A DSN — 엔진이 물고 있다(기준)")
    ap.add_argument("--dsn-b", required=True, help="사본 B DSN — 문의 파이썬 길이 쓴다(A 와 같은 씨앗)")
    ap.add_argument("--engine-url", required=True, help="사본 A 를 문 엔진 URL (요청은 엔진에 곧장)")
    ap.add_argument(
        "--sink-url-b", required=True, help="사본 B 를 문 엔진의 POST /events — 파이썬 길의 사건을 받는다"
    )
    ap.add_argument(
        "--owner-token",
        default="verdict-parity-token",
        help="두 엔진·문이 같이 쓰는 오너 토큰 (엔진 BORING_OWNER_TOKEN 과 같아야 한다)",
    )
    ap.add_argument(
        "--sample", type=int, default=5, help="모양마다 사본 A 에서 다시 칠 실제 세션·쌍 수 (기본 5)"
    )
    ap.add_argument(
        "--fixture", action="store_true", help="대체 경로: 간선에서 안 뽑고 고정 묶음을 쓴다 (기본 꺼짐)"
    )
    ap.add_argument("--event-wait", type=float, default=10.0, help="B 의 사건이 도착하길 기다리는 초")
    args = ap.parse_args(argv)

    ok, line = assert_scratch(args.dsn_a, args.dsn_b, dict(os.environ))
    print(line)
    if not ok:
        return 2
    try:
        result, notes = run_parity(args)
    except Unmeasured as e:
        print(f"잰 게 아니다: {e}", file=sys.stderr)
        return 2
    passed, problems = decide(result)
    print("\n".join(render(result, notes)))
    print("PASS" if passed else "NOT PASS: " + "; ".join(problems))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
