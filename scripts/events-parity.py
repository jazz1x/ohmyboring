#!/usr/bin/env python3
"""사건 길 대조 — 같은 씨앗의 사본 DB 두 벌에 같은 요청 묶음을 엔진(A)과 문의 파이썬 길(B)로 치고 비교한다.

E4-6a 계약. 켜기 전에 한 번 돌린다 — CI 가 아니다.

  A  엔진(:77xx)이 사본 A 를 물고 있다 — 요청은 엔진에 곧장(HTTP POST·GET /events, MCP events).
  B  이 스크립트가 문(agents/door/door.py)을 같은 프로세스 안에서 DOOR_EVENTS_OWNER=python 으로
     띄워(FastAPI TestClient) 사본 B 에 같은 묶음을 친다. 사건 길은 사건을 남기지 않아 싱크 엔진이
     필요 없다 — 스풀은 임시 파일로.

운영 DB·운영 엔진·운영 문은 모른다. 시작에 두 DSN 이 운영 DSN 과 다름을 단언하고 그 줄을 찍는다 —
단언이 깨지면 아무것도 치지 않고 종료코드 2.

요청 묶음은 사본 A 의 진짜 event_log 행에서 뽑는다: event_name 별로 최대 --sample 건(기본 5,
md5 순 — 두 번 읽어도 같다)을 골라 저장된 칸(otel 포함)을 그대로 다시 쳐서(재생 — 가림은
거듭핏잎이라 멱등) 쓰기 칸 대열 전체를 잰다. 여기에 고정 묶음을 얹는다:
  101개 묶음(400) · 빈 묶음({"accepted":0}) · 단일 객체 본문 · 배열 아닌 events 칸(본문 통째 한 사건)
  · 가림 탐침(sk-ant 토큰을 목록 속에 — 두 길의 가림이 같은지) · 중간 실패(같은 트리거를 두 사본에
    깔고 — 엔진은 반쯤 쓰고(자동 커밋) 파이썬은 0 행, 의도한 차이) · GET(여러 필터·limit·
    since_hours·비정수 limit) · MCP events(인자 규약·-32602·정렬).

대조: event_log 에 새로 생긴 행(내용 — id·observed_at 는 이름으로 뺀다) + 응답(상태·바이트).
성공·검증 400 은 바이트 그대로(문이 엔진 모양을 옮긴 것), 실패 행(HTTP 5xx · MCP -32603)은
상태·error 칸 존재만 본다(psycopg 대 sqlx 문구). GET·MCP 는 응답을 정규화해(엔트리의
id·observed_at·otel.observed_timestamp 를 뺀다) 비교한다 — 두 길이 넣은 시각 칸이라 원래 다른 값.
멈춰 한 면이라도 0 건이면 PASS 를 안 낸다(0 은 잰 게 아니다).

종료코드: 0 PASS · 1 차이 있음 · 2 잰 게 아니다(DSN 단언·씨앗 불일치·장비 불통).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SK_ANT = "sk-ant-abcdefghijklmnopqrstuvwxyz1234567890ABCDEF"
BOOM = "parity_boom"
SURFACES = ("post", "get", "mcp")
PRODUCTION_DSN_ENVS = ("DOOR_PG_DSN", "PG_DSN", "BORING_PG_DSN", "BORING_DATABASE_URL")
#: .env.example · docker-compose.yml 이 적은 운영 DSN — 환경변수가 없어도 이 모양은 운영이다.
PRODUCTION_DSN_DEFAULTS = (
    "postgresql://boring:boring@127.0.0.1:5432/boring",
    "postgresql://boring:boring@boring-postgres:5432/boring",
)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "", "boring-postgres"}

EXCLUDED = (
    "event_log.id",
    "event_log.observed_at (GET 엔트리·otel.observed_timestamp 도)",
    "응답 error 메시지 글자 (실패 행만: HTTP 5xx · MCP -32603)",
)

SEED_SQL = (
    "SELECT id, time_unix_nano, severity_text, severity_number, service_name,"
    " component, event_name, status, trace_id, span_id, run_id, session_id, workflow,"
    " workflow_node, workflow_outcome, body::text, attributes::text, resource::text"
    " FROM event_log ORDER BY id;"
)
ROW_SQL = (
    "SELECT id, observed_at, time_unix_nano, severity_text, severity_number, service_name,"
    " component, event_name, status, trace_id, span_id, run_id, session_id, workflow,"
    " workflow_node, workflow_outcome, body::text, attributes::text, resource::text"
    " FROM event_log WHERE event_name = %s ORDER BY md5(id::text) LIMIT %s;"
)
ADDED_SQL = (
    "SELECT time_unix_nano, severity_text, severity_number, service_name, component, event_name,"
    " status, trace_id, span_id, run_id, session_id, workflow, workflow_node, workflow_outcome,"
    " body::text, attributes::text, resource::text"
    " FROM event_log WHERE id > %s ORDER BY id;"
)
BOOM_SQL = (
    "CREATE OR REPLACE FUNCTION parity_boom() RETURNS trigger LANGUAGE plpgsql AS $$"
    f" BEGIN IF NEW.component = '{BOOM}' THEN RAISE EXCEPTION 'parity boom'; END IF; RETURN NEW; END $$;"
)
BOOM_TRIGGER = (
    "DROP TRIGGER IF EXISTS parity_event_boom ON event_log;"
    " CREATE TRIGGER parity_event_boom BEFORE INSERT ON event_log"
    " FOR EACH ROW EXECUTE FUNCTION parity_boom();"
)
BOOM_DROP = "DROP TRIGGER IF EXISTS parity_event_boom ON event_log"


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


# ── 재생 묶음 (순수) ────────────────────────────────────────────────────────


def replay_body(row: tuple) -> dict:
    """event_log 행(SELECT 칸 순서) → 그 칸 대열을 다시 내는 POST /events 본문.

    저장된 otel 칸을 그대로 싣는다 — 가림은 멱등이라 한 번 더 쳐도 같은 행이 생긴다(다만
    id·observed_at — 이름으로 뺀다). run_id 등은 언제나 윗칸에서만 읽으니 윗칸에 둔다."""
    (
        _id,
        _observed_at,
        time_unix_nano,
        severity_text,
        severity_number,
        _service_name,
        component,
        event_name,
        status,
        trace_id,
        span_id,
        run_id,
        session_id,
        workflow,
        workflow_node,
        workflow_outcome,
        body,
        attributes,
        resource,
    ) = row
    otel = {
        "event_name": event_name,
        "severity_text": severity_text,
        "severity_number": severity_number,
        "trace_id": trace_id,
        "span_id": span_id,
        "body": json.loads(body),
        "attributes": json.loads(attributes),
        "resource": json.loads(resource),
    }
    if time_unix_nano is not None:
        otel["time_unix_nano"] = time_unix_nano
    otel = {key: value for key, value in otel.items() if value is not None}
    out = {
        "component": component,
        "event": event_name,
        "status": status,
        "run_id": run_id,
        "session_id": session_id,
        "workflow": workflow,
        "workflow_node": workflow_node,
        "workflow_outcome": workflow_outcome,
        "otel": otel,
    }
    return {key: value for key, value in out.items() if value is not None}


@dataclass(frozen=True)
class Req:
    surface: str  # "post" | "get" | "mcp"
    label: str
    path: str
    body: dict


def build_batch(rows: list[tuple]) -> list[Req]:
    """사본 A 의 진짜 행 재생 + 고정 묶음(검증·가림·모양 규약)."""
    reqs = [Req("post", f"replay {row[7]} (id {row[0]})", "/events", replay_body(row)) for row in rows]
    reqs += [
        Req("post", "101 batch (400)", "/events", {"events": [{"event": "x", "i": i} for i in range(101)]}),
        Req("post", "empty batch", "/events", {"events": []}),
        Req(
            "post",
            "single object body",
            "/events",
            {"component": "door", "event": "parity_single", "status": "ok", "run_id": "parity-single"},
        ),
        Req(
            "post",
            "non-array events value",
            "/events",
            {"events": "not-an-array", "component": "door", "event": "parity_nonarray"},
        ),
        Req(
            "post",
            "redaction probe",
            "/events",
            {
                "component": "door",
                "event": "parity_redact",
                "status": "ok",
                "nested": {"list": ["clean", SK_ANT], "token": SK_ANT},
            },
        ),
        Req("get", "get default", "/events", {}),
        Req("get", "get limit=2", "/events?limit=2", {}),
        Req("get", "get limit=5000", "/events?limit=5000", {}),
        Req("get", "get limit=0 (clamp 1)", "/events?limit=0", {}),
        Req("get", "get filter component", "/events?component=door&limit=5", {}),
        Req("get", "get filter event", "/events?event=recall_shadow&limit=5", {}),
        Req("get", "get filter status", "/events?status=ok&limit=5", {}),
        Req("get", "get since_hours=0", "/events?since_hours=0", {}),
        Req("get", "get since_hours=-1 (400)", "/events?since_hours=-1", {}),
        Req("get", "get limit=abc (serde 400)", "/events?limit=abc", {}),
        Req("mcp", "mcp default", "/mcp", {}),
        Req("mcp", "mcp limit", "/mcp", {"limit": 3}),
        Req("mcp", "mcp non-integer limit", "/mcp", {"limit": "3"}),
        Req("mcp", "mcp trimmed args", "/mcp", {"component": " door ", "event": "recall_shadow", "limit": 2}),
        Req("mcp", "mcp since_hours=-1 (-32602)", "/mcp", {"since_hours": -1}),
        Req("mcp", "mcp since_hours string ignored", "/mcp", {"since_hours": "3", "limit": 2}),
    ]
    return reqs


def midway_request() -> Req:
    """component 하나에서 INSERT 가 터지는 세 사건 묶음 — 앞 사걸만 엔진에 남는다(자동 커밋)."""
    return Req(
        "post",
        "mid-way failure",
        "/events",
        {
            "events": [
                {"component": "parity-mid-a", "event": "mid"},
                {"component": BOOM, "event": "mid"},
                {"component": "parity-mid-b", "event": "mid"},
            ]
        },
    )


# ── 정규화·대조 (순수) ──────────────────────────────────────────────────────


def strip_entry(entry: dict) -> dict:
    """이름으로 빼는 칸 — 엔트리의 id·observed_at·otel.observed_timestamp."""
    stripped = dict(entry)
    stripped.pop("id", None)
    stripped.pop("observed_at", None)
    otel = stripped.get("otel")
    if isinstance(otel, dict):
        otel = dict(otel)
        otel.pop("observed_timestamp", None)
        stripped["otel"] = otel
    return stripped


def normalize_get(body: bytes) -> dict | None:
    """GET 봉투를 정규화 — entries 가 없는 모양(오류 봉투 등)은 None(정규화 대상 아님)."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return None
    return {
        "entries": [strip_entry(entry) for entry in data["entries"]],
        "limit_applied": data.get("limit_applied"),
        "maybe_truncated": data.get("maybe_truncated"),
    }


def normalize_mcp(body: bytes) -> dict | None:
    """성공 봉투를 정규화 — structuredContent 를 못 읽는 모양은 None. 오류 봉투는 rpc_error 가 본다."""
    try:
        wire = json.loads(body)
    except ValueError:
        return None
    if not isinstance(wire, dict) or not isinstance(wire.get("result"), dict):
        return None
    result = wire["result"]
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("entries"), list):
        structured = {**structured, "entries": [strip_entry(e) for e in structured["entries"]]}
    return {
        "id": wire.get("id"),
        "result": {"structuredContent": structured, "isError": result.get("isError")},
    }


@dataclass(frozen=True)
class Answer:
    status: int
    body: bytes


def failure_kind(answer: Answer) -> tuple | None:
    """실패 행의 모양 — (http, status) 또는 (rpc, code). 메시지 글자는 안 본다."""
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


def rpc_error(answer: Answer) -> dict | None:
    try:
        data = json.loads(answer.body)
    except ValueError:
        return None
    error = data.get("error") if isinstance(data, dict) else None
    return error if isinstance(error, dict) else None


def answers_match(engine: Answer, python: Answer, surface: str) -> bool:
    """면에 맞게 대조 — post 는 바이트(실패 행은 상태·error 칸), get·mcp 는 정규화한 값.
    -32603(표/저장소 고장)은 코드만 같으면 된다(문구는 psycopg 대 sqlx — 이름으로 뺀 칸),
    -32602(검증)는 메시지까지 같아야 한다."""
    if engine.status >= 500 or python.status >= 500:
        return failure_kind(engine) == failure_kind(python)
    if engine.body == python.body:
        return True
    if surface == "mcp":
        engine_err, python_err = rpc_error(engine), rpc_error(python)
        if engine_err is not None or python_err is not None:
            if not (engine_err and python_err):
                return False
            if engine_err.get("code") == -32603 and python_err.get("code") == -32603:
                return True
            return engine_err == python_err
        a, b = normalize_mcp(engine.body), normalize_mcp(python.body)
        return a is not None and a == b
    if surface == "get":
        a, b = normalize_get(engine.body), normalize_get(python.body)
        return a is not None and a == b
    return False  # post: 바이트가 다륾면 다르다


def diff_rows(a: list, b: list) -> tuple[list, list]:
    """다중집합 차이 — (A 에만, B 에만). 같은 행이 두 번 있으면 두 번으로 센다."""
    only_a = Counter(a) - Counter(b)
    only_b = Counter(b) - Counter(a)
    return sorted(only_a.elements(), key=repr), sorted(only_b.elements(), key=repr)


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
    response_diffs: list[tuple[str, Answer, Answer]] = field(default_factory=list)
    table_diffs: tuple[list, list] = ()
    midway: Midway | None = None


def decide(result: Result) -> tuple[bool, list[str]]:
    """PASS 는 면 건수가 모두 >0 이고 응답·표 차이가 0 이며 중간 실패가 의도한 모양일 때만."""
    problems: list[str] = []
    for surface, count in result.counts.items():
        if count == 0 or result.answered.get(surface, 0) == 0:
            problems.append(f"surface {surface}: 0 requests answered — a 0 is not a measurement")
    problems += [f"response differs: {label}" for label, _, _ in result.response_diffs]
    if result.table_diffs[0] or result.table_diffs[1]:
        problems.append(
            f"event_log differs: A-only {len(result.table_diffs[0])}, B-only {len(result.table_diffs[1])}"
        )
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


class Unmeasured(Exception):
    """잰 게 아니다 — 종료코드 2."""


def fetch_seed(dsn: str) -> list[tuple]:
    import psycopg

    with psycopg.connect(dsn, autocommit=True, options="-c default_transaction_read_only=on") as conn:
        with conn.cursor() as cur:
            cur.execute(SEED_SQL)
            return [tuple(r) for r in cur.fetchall()]


def sample_rows(dsn: str, sample: int) -> list[tuple]:
    """event_name 별로 최대 sample 건(md5 순 — 두 번 읽어도 같다). 읽기 전용."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True, options="-c default_transaction_read_only=on") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT event_name FROM event_log ORDER BY event_name;")
            names = [r[0] for r in cur.fetchall()]
            rows: list[tuple] = []
            for name in names:
                cur.execute(ROW_SQL, (name, sample))
                rows.extend(tuple(r) for r in cur.fetchall())
            return rows


def mark(dsn: str) -> int:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM event_log")
        return int(cur.fetchone()[0])


def added_rows(dsn: str, after: int) -> list[tuple]:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(ADDED_SQL, (after,))
        return [tuple(r) for r in cur.fetchall()]


@contextlib.contextmanager
def boom_installed(dsns: tuple[str, str]):
    """두 사본에 같은 INSERT 트리거를 임시로 깐다 — 끝에 지운다."""

    def run(dsn: str, statements: list[str]) -> None:
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)

    for dsn in dsns:
        run(dsn, [BOOM_SQL, BOOM_TRIGGER])
    try:
        yield
    finally:
        for dsn in dsns:
            run(dsn, [BOOM_DROP])


def wire_body(req: Req) -> dict:
    if req.surface == "mcp":
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "events", "arguments": req.body},
        }
    return req.body


def engine_answer(base: str, req: Req) -> Answer:
    if req.surface == "get":
        http = urllib.request.Request(base.rstrip("/") + req.path, method="GET")
    else:
        http = urllib.request.Request(
            base.rstrip("/") + req.path,
            data=json.dumps(wire_body(req)).encode(),
            headers={"content-type": "application/json", "accept": "application/json"},
            method="POST",
        )
    try:
        with urllib.request.urlopen(http, timeout=60) as r:
            return Answer(r.status, r.read())
    except urllib.error.HTTPError as e:
        return Answer(e.code, e.read())
    except OSError as e:
        raise Unmeasured(f"engine {base} unreachable: {e}") from e


def door_answer(client, req: Req) -> Answer:
    if req.surface == "get":
        r = client.get(req.path)
    else:
        r = client.post(req.path, json=wire_body(req))
    return Answer(r.status_code, r.content)


def open_door(dsn_b: str, event_log_path: str):
    """문을 같은 프로세스에 — 사본 B 만 보고, 사건은 스풀(임시 파일)로."""
    os.environ.update(
        DOOR_EVENTS_OWNER="python",
        DOOR_PG_DSN=dsn_b,
        BORING_EVENT_SINK="spool",
        BORING_EVENT_LOG=event_log_path,
    )
    for path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
        if path not in sys.path:
            sys.path.insert(0, path)
    import importlib

    from fastapi.testclient import TestClient

    door = importlib.import_module("agents.door.door")
    return TestClient(door.app)


def run_parity(args) -> Result:
    seed_a, seed_b = fetch_seed(args.dsn_a), fetch_seed(args.dsn_b)
    if seed_a != seed_b:
        raise Unmeasured("seed copies differ: event_log rows are not identical on A and B")
    rows = sample_rows(args.dsn_a, args.sample)
    if not rows:
        raise Unmeasured("seed copy has no event_log rows to replay")
    batch = build_batch(rows)
    result = Result(counts=surface_counts(batch), answered=dict.fromkeys(SURFACES, 0))
    mark_a, mark_b = mark(args.dsn_a), mark(args.dsn_b)
    with tempfile.TemporaryDirectory() as tmp:
        client = open_door(args.dsn_b, str(Path(tmp) / "events.ndjson"))
        for req in batch:
            engine = engine_answer(args.engine_url, req)
            python = door_answer(client, req)
            result.answered[req.surface] += 1
            if not answers_match(engine, python, req.surface):
                result.response_diffs.append((req.label, engine, python))
        added_a, added_b = added_rows(args.dsn_a, mark_a), added_rows(args.dsn_b, mark_b)
        result.table_diffs = diff_rows(added_a, added_b)
        result.midway = run_midway(args, client, mark_a, mark_b)
    return result


def run_midway(args, client, mark_a: int, mark_b: int) -> Midway:
    req = midway_request()
    base_a, base_b = added_rows(args.dsn_a, mark_a), added_rows(args.dsn_b, mark_b)
    with boom_installed((args.dsn_a, args.dsn_b)):
        engine = engine_answer(args.engine_url, req)
        python = door_answer(client, req)
    end_a, end_b = added_rows(args.dsn_a, mark_a), added_rows(args.dsn_b, mark_b)
    return Midway(
        engine.status,
        python.status,
        len(end_a) - len(base_a),
        len(end_b) - len(base_b),
    )


def render(result: Result) -> list[str]:
    lines = [
        "surface counts (requests built / answered on both sides): "
        + ", ".join(f"{s}={result.counts[s]}/{result.answered[s]}" for s in SURFACES),
        "excluded by name: " + "; ".join(EXCLUDED),
    ]
    only_a, only_b = result.table_diffs
    lines.append(
        f"event_log: A-only {len(only_a)} B-only {len(only_b)} {'same' if not only_a and not only_b else 'DIFFERENT'}"
    )
    lines += [f"  A-only {r!r}" for r in only_a[:5]] + [f"  B-only {r!r}" for r in only_b[:5]]
    for label, engine, python in result.response_diffs:
        lines.append(
            f"response DIFFERENT [{label}]: engine {engine.status} {engine.body[:120]!r} / python {python.status} {python.body[:120]!r}"
        )
    if not result.response_diffs:
        lines.append(f"responses: {sum(result.answered.values())} compared, 0 differ")
    mid = result.midway
    if mid is not None:
        lines.append(
            f"expected difference (mid-way failure, one request = one transaction): engine {mid.engine_status} left "
            f"{mid.engine_rows} event_log rows (partial, auto-commit) / python {mid.python_status} left {mid.python_rows} rows"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="사건 길 대조 — 사본 DB 두 벌에 같은 요청 묶음을 엔진(A)과 문의 파이썬 길(B)로 쳐서 비교한다 (E4-6a).",
        epilog="실행 예: python3 scripts/events-parity.py --dsn-a postgresql://…/parity_a --dsn-b postgresql://…/parity_b "
        "--engine-url http://127.0.0.1:7791",
    )
    ap.add_argument("--dsn-a", required=True, help="사본 A DSN — 엔진이 물고 있다(기준)")
    ap.add_argument("--dsn-b", required=True, help="사본 B DSN — 문의 파이썬 길이 쓴다(A 와 같은 씨앗)")
    ap.add_argument("--engine-url", required=True, help="사본 A 를 문 엔진 URL (요청은 엔진에 곧장)")
    ap.add_argument(
        "--sample", type=int, default=5, help="event_name 마다 사본 A 에서 다시 칠 행 수 (기본 5)"
    )
    args = ap.parse_args(argv)

    ok, line = assert_scratch(args.dsn_a, args.dsn_b, dict(os.environ))
    print(line)
    if not ok:
        return 2
    try:
        result = run_parity(args)
    except Unmeasured as e:
        print(f"잰 게 아니다: {e}", file=sys.stderr)
        return 2
    passed, problems = decide(result)
    print("\n".join(render(result)))
    print("PASS" if passed else "NOT PASS: " + "; ".join(problems))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
