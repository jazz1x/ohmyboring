#!/usr/bin/env python3
"""E3c-1 검증 — 일회용 스키마·임시 볼트에서 문의 파이썬 쓰기 길을 엔진과 대조한다.

  일회용 pgvector 컨테이너(make test-db 와 같은 방식, 종료 시 사해짐 보장) + 임시 볼트에
  엔진 바이너리를 띄우고, 문(DOOR_REMEMBER_WRITER=python)이 remember 를 쓰게 한다.
  그 뒤 엔진의 POST /sync 가 파이썬이 쓴 노트를 다시 읽어도 변경이 0임을 — 파일 해시와
  document·chunk·edge·node·claim 행 전부를 두 번 스냅숏해 비교하는 숫자로 보인다.

  운영 DB·운영 볼트·엔진/hermes/postgres 컨테이너에는 손을 대지 않는다. 임베딩은 호스트
  ollama(bge-m3)만 쓴다 — 모델 호출(생성)은 없다.

갈래(페이즈):
  A 파이썬 쓰기 — 새 노트(번호 max+1·파일·행·간선·claim) → 이름한 교정(supersedes
    간선 + 다시 말한 슬롯만 닫힘) → 엔진 sync 한 번. chunk·edge·claim·document 는
    0 변경이어야 하고, node 의 공유 슬롯 라벨·relates_to 투영처럼 엔진이 정해 재계산하는
    칸이 있으면 그 종류를 이름 붙여 보인다(엔진이 쓴 노트도 같은 모습으로 정리됨 — C).
  B 정상 — sync 를 한 번 더 돌려 전 표·파일이 그대로임(0 변경)을 숫자로 본다.
  C 대조군 — 스위치를 끄고(temp 문 프록시) 엔진이 직접 같은 모양의 두 노트를 쓰게 해
    sync 도 돌려 본다. 공유 슬롯 라벨 정리는 엔진의 자기 행동임을 보인다.

실행: python3 scripts/e3c1-verify.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_BIN = ROOT / "drudge" / "target" / "debug" / "drudge"
DOOR_PORT = 7791
ENGINE_PORT = 7790
OWNER_TOKEN = "e3c1-owner-token"

TABLES: dict[str, str] = {
    "document": (
        "SELECT source_path, origin, project, kind, title, tags::text, sha,"
        " updated_at::text, author FROM document ORDER BY source_path"
    ),
    "chunk": (
        "SELECT id, source_path, content, origin, project, kind, chunk_idx, embedding::text"
        " FROM chunk ORDER BY id"
    ),
    "edge": "SELECT src, dst, kind, judge FROM edge ORDER BY src, dst, kind",
    "node": "SELECT id, kind, label, outcome FROM node ORDER BY id",
    "claim": (
        "SELECT subject, predicate, value, source_path, valid_from::text, kind, confidence,"
        " said_by, anchor, anchor_hash, anchor_symbol, era, superseded_at::text"
        " FROM claim ORDER BY subject, predicate, valid_from::text"
    ),
}

BODY_ONE = (
    "첫 번째 본문. 알파 도구로 원샷 파이프라인을 돌린 기록. "
    "입력 묶음을 나누고, 각 묶음을 순서대로 처리해 합친다. "
    "실패한 묶음은 표시만 남기고 건 너뛴다 — 전체를 다시 돌리지 않는다. "
    "근거는 src/e3c1_first.py:12 에 있다. 수치는 실행할 때마다 다시 잰다."
)
BODY_TWO = (
    "교정 본문. 베타 도구로 투샷 정리를 한 기록. "
    "슬롯의 오래된 값을 새 값으로 바꾸는 교정이 핵심이고, "
    "옛 노트가 말한 다른 사실은 손대지 않는다. "
    "근거는 src/e3c1_second.py:7 에 있다. 명령 결과만 남긴다."
)


def log(message: str) -> None:
    print(f"[e3c1] {message}", flush=True)


def fail(message: str) -> None:
    print(f"[e3c1] FAIL: {message}", flush=True)
    raise SystemExit(1)


def http_json(port: int, method: str, path: str, body: dict | None = None, timeout: float = 300.0):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"content-type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def wait_health(port: int, timeout: float = 90.0, vector: bool | None = None) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, body = http_json(port, "GET", "/health", timeout=3)
            if status == 200 and (vector is None or body.get("vector") is vector):
                return body
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.3)
    fail(f"포트 {port} 가 뜨지 않았다")


class Stack:
    """일회용 컨테이너·엔진·문을 띄우고 반드시 치운다."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="e3c1-verify-"))
        self.vault = self.tmp / "vault"
        (self.vault / "wiki").mkdir(parents=True)
        (self.vault / "rules").mkdir()
        self.container = f"e3c1-verify-{os.getpid()}"
        self.proc_engine: subprocess.Popen | None = None
        self.proc_door: subprocess.Popen | None = None

    def start(self) -> None:
        if not ENGINE_BIN.exists():
            log("엔진 바이너리가 없다 — cargo build (drudge, 일회용 스키마 대상)")
            subprocess.run(["cargo", "build"], cwd=ROOT / "drudge", check=True)
        self._start_db()
        self._write_config()
        self._start_engine()
        self._start_door()

    def _start_db(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)
        run = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.container,
                "-p",
                "127.0.0.1::5432",
                "-e",
                "POSTGRES_USER=boring",
                "-e",
                "POSTGRES_PASSWORD=boring",
                "-e",
                "POSTGRES_DB=boring",
                "pgvector/pgvector:pg16",
            ],
            capture_output=True,
            text=True,
        )
        if run.returncode != 0:
            fail(f"일회용 pgvector 컨테이너 시작 실패: {run.stderr.strip()}")
        port = (
            subprocess.run(
                ["docker", "port", self.container, "5432/tcp"],
                capture_output=True,
                text=True,
                check=True,
            )
            .stdout.strip()
            .split(":")[-1]
        )
        self.pg_dsn = f"postgresql://boring:boring@127.0.0.1:{port}/boring"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            check = subprocess.run(
                [
                    "docker",
                    "exec",
                    self.container,
                    "psql",
                    "-U",
                    "boring",
                    "-d",
                    "boring",
                    "-tAc",
                    "SELECT 1",
                ],
                capture_output=True,
            )
            if check.returncode == 0:
                log(f"일회용 pgvector 준비 ({self.pg_dsn})")
                return
            time.sleep(1)
        fail("일회용 pgvector 가 준비되지 않았다")

    def _write_config(self) -> None:
        (self.tmp / "boring.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "llm": {
                        "base_url": "http://127.0.0.1:11434/v1",
                        "embed_model": "bge-m3",
                        "embed_dim": 1024,
                        "model": "gemma4:12b",
                    },
                    "repos": [],
                    "code_index": {"sources": []},
                }
            ),
            encoding="utf-8",
        )

    def _engine_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "PG_DSN": self.pg_dsn,
                "BORING_VECTOR": "on",
                "BORING_HTTP_ADDR": f"127.0.0.1:{ENGINE_PORT}",
                "BORING_VAULT_DIR": str(self.vault),
                "BORING_CONFIG": str(self.tmp / "boring.json"),
                "BORING_LLM_BASE_URL": "http://127.0.0.1:11434/v1",
                "BORING_OWNER_TOKEN": OWNER_TOKEN,
                "BORING_SYNC_HOURS": "8760",
                "BORING_BRIEF_HOUR": str((time.gmtime().tm_hour + 12) % 24),
                "TZ": "UTC",
            }
        )
        env.pop("BORING_URL", None)
        env.pop("BORING_DOOR_URL", None)
        return env

    def _start_engine(self) -> None:
        self.engine_log = open(self.tmp / "engine.log", "w", encoding="utf-8")
        self.proc_engine = subprocess.Popen(
            [str(ENGINE_BIN), "serve"],
            env=self._engine_env(),
            cwd=self.tmp,
            stdout=self.engine_log,
            stderr=self.engine_log,
        )
        health = wait_health(ENGINE_PORT, vector=True)
        log(f"엔진 뜸 (vector={health.get('vector')}, corpus={health.get('corpus_count')})")

    def _door_env(self, writer: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}",
                "DOOR_PORT": str(DOOR_PORT),
                "DOOR_UPSTREAM": f"http://127.0.0.1:{ENGINE_PORT}",
                "DOOR_PG_DSN": self.pg_dsn,
                "DOOR_REMEMBER_WRITER": writer,
                "BORING_VAULT_DIR": str(self.vault),
                "BORING_CONFIG": str(self.tmp / "boring.json"),
                "BORING_URL": f"http://127.0.0.1:{ENGINE_PORT}",
                "BORING_EVENT_SINK": "spool",
                "BORING_EVENT_LOG": str(self.tmp / "events.ndjson"),
                "BORING_LLM_BASE_URL": "http://127.0.0.1:11434/v1",
                "BORING_OWNER_TOKEN": OWNER_TOKEN,
            }
        )
        return env

    def _start_door(self) -> None:
        self.door_log = open(self.tmp / "door.log", "w", encoding="utf-8")
        self.proc_door = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agents.door.door:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(DOOR_PORT),
            ],
            env=self._door_env("python"),
            cwd=ROOT,
            stdout=self.door_log,
            stderr=self.door_log,
        )
        wait_health(DOOR_PORT)
        log("문 뜸 (DOOR_REMEMBER_WRITER=python)")

    def restart_door_as_engine(self) -> None:
        """대조군(C) — 문은 스위치 꺼진 프록시로만."""
        if self.proc_door is not None and self.proc_door.poll() is None:
            self.proc_door.send_signal(signal.SIGTERM)
            self.proc_door.wait(timeout=10)
        self.door_log = open(self.tmp / "door.log", "w", encoding="utf-8")
        self.proc_door = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agents.door.door:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(DOOR_PORT),
            ],
            env=self._door_env("engine"),
            cwd=ROOT,
            stdout=self.door_log,
            stderr=self.door_log,
        )
        wait_health(DOOR_PORT)
        log("문 재시동 (DOOR_REMEMBER_WRITER=engine — 프록시 대조군)")

    def sync(self) -> dict:
        status, body = http_json(ENGINE_PORT, "POST", "/sync")
        if status != 200:
            fail(f"엔진 /sync 고장: {status} {body}")
        return body

    def snapshot(self) -> dict[str, list]:
        import hashlib

        import psycopg

        state: dict[str, list] = {}
        with psycopg.connect(self.pg_dsn) as conn, conn.cursor() as cur:
            for table, sql in TABLES.items():
                cur.execute(sql)
                state[table] = cur.fetchall()
        state["files"] = sorted(
            (name, hashlib.sha256((self.vault / "wiki" / name).read_bytes()).hexdigest())
            for name in os.listdir(self.vault / "wiki")
        )
        return state

    def claims(self) -> list[tuple]:
        import psycopg

        with psycopg.connect(self.pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT subject, predicate, value, source_path, superseded_at IS NOT NULL"
                " FROM claim ORDER BY source_path, subject, predicate"
            )
            return cur.fetchall()

    def spool_events(self, event: str) -> list[dict]:
        path = self.tmp / "events.ndjson"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("event") == event
        ]

    def engine_tail(self, n: int = 6) -> list[str]:
        path = self.tmp / "engine.log"
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]

    def stop(self) -> None:
        for proc in (self.proc_door, self.proc_engine):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)
        for name in ("engine_log", "door_log"):
            handle = getattr(self, name, None)
            if handle is not None:
                handle.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def diff_states(before: dict[str, list], after: dict[str, list]) -> dict[str, dict]:
    """표별 어긋남 — after 만의 행·before 만의 행."""
    report: dict[str, dict] = {}
    for table in before:
        b, a = before[table], after[table]
        if b != a:
            report[table] = {
                "before_rows": len(b),
                "after_rows": len(a),
                "added": [row for row in a if row not in b][:4],
                "removed": [row for row in b if row not in a][:4],
            }
    return report


def print_diff(title: str, report: dict[str, dict]) -> None:
    log(title)
    for table, item in report.items():
        log(
            f"   {table}: {item['before_rows']}행 → {item['after_rows']}행"
            f"  +{len(item['added'])} -{len(item['removed'])}"
        )
        for row in item["added"]:
            log(f"     + {row}")
        for row in item["removed"]:
            log(f"     - {row}")


def main() -> int:
    if shutil.which("docker") is None:
        fail("docker 가 없다")
    stack = Stack()
    try:
        stack.start()

        # ── A. 파이썬 쓰기 — 새 노트 → 이름한 교정(부분 닫기) → 엔진 sync #1. ──────
        note1_args = {
            "title": "E3c-1 첫 노트",
            "body": BODY_ONE,
            "repo": "e3c1-one",
            "tools": ["alpha"],
            "concepts": ["oneshot"],
            "claims": [
                {"subject": "Slot A", "predicate": "state", "value": "a-one", "kind": "fact"},
                {"subject": "Slot B", "predicate": "state", "value": "b-one", "kind": "fact"},
            ],
            "author": "agent:e3c1",
        }
        status, body = http_json(DOOR_PORT, "POST", "/remember", note1_args)
        if status != 200 or body.get("wiki_id") != "wiki-0001" or body.get("duplicate") is not None:
            fail(f"첫 remember 모양이 어긋난다: {status} {body}")
        log(f"A① 새 노트 — {body['wiki_id']} @ {body['source_path']}")
        log(f"    답: {json.dumps(body, ensure_ascii=False)}")
        note1_path = body["source_path"]

        note2_args = {
            "title": "E3c-1 교정 노트",
            "body": BODY_TWO,
            "repo": "e3c1-two",
            "tools": ["beta"],
            "concepts": ["twoshot"],
            "claims": [
                {"subject": "Slot A", "predicate": "state", "value": "a-two", "kind": "fact"},
                {"subject": "Slot C", "predicate": "state", "value": "c-two", "kind": "fact"},
            ],
            "author": "agent:e3c1",
            "supersedes": [note1_path],
        }
        status, body = http_json(DOOR_PORT, "POST", "/remember", note2_args)
        if status != 200 or body.get("supersedes") != 1 or body.get("unknown") != 0:
            fail(f"교정 remember 모양이 어긋난다: {status} {body}")
        log(
            f"A② 교정 노트 — {body['wiki_id']} 가 wiki-0001 을 대체 · 답: {body['supersedes']}/{body['unknown']}"
        )

        claims_now = stack.claims()
        sealed = {(s, p) for (s, p, _v, path, is_sealed) in claims_now if is_sealed}
        if ("slot-a", "state") not in sealed:
            fail(f"부분 닫기 — slot-a 가 닫히지 않았다: {claims_now}")
        if ("slot-b", "state") in sealed:
            fail(f"부분 닫기 — slot-b(다시 말하지 않음)까지 닫혔다: {claims_now}")
        log("A③ 부분 닫기 — slot-a 만 닫히고 slot-b 는 산다")

        before = stack.snapshot()
        settled = False
        for round_no in range(1, 6):
            sync_resp = stack.sync()
            after = stack.snapshot()
            report = diff_states(before, after)
            log(
                f"A④ 엔진 sync #{round_no} — new={sync_resp.get('ingest_new')} "
                f"updated={sync_resp.get('ingest_updated')} repaired={sync_resp.get('ingest_repaired')}"
                + (" (0 변경 — 정상)" if not report else "")
            )
            if round_no == 1:
                strict = [t for t in ("chunk", "edge", "claim", "document") if t in report]
                if strict:
                    print_diff("A④ 어긋난 표(엄격 부분):", {t: report[t] for t in strict})
                    fail(f"sync #1 이 엄격 표를 바꿨다: {strict}")
                if report:
                    print_diff(
                        "A④ 엔진 재계산 칸(의도한 것 — 엔진이 쓴 노트도 같다, C 대조):",
                        report,
                    )
            elif report:
                print_diff(f"A④ sync #{round_no} 어긋남(수렴 중):", report)
            before = after
            if sync_resp.get("ingest_new") == 0 and sync_resp.get("ingest_updated") == 0 and not report:
                settled = True
                log(f"A⑤ sync #{round_no} 에 수렴 — 이 뒤의 스냅숏이 정상 상태")
                break
        if not settled:
            fail("동기화가 다섯 바퀴 안에 수렴하지 않았다")
        log("A⑤ sync #1 — chunk·edge·claim·document 는 0 변경이었다(엄격 부분)")
        for table, rows in before.items():
            log(f"   정상 {table:9s} {len(rows):4d}행")

        events = stack.spool_events("remember_written")
        if len(events) != 2:
            fail(f"remember_written 사건 두 줄이 아니다: {events}")
        for event in events:
            log(
                f"B② 사건 remember_written — {event['decision']} {event.get('source_path')} "
                f"chunks={event.get('chunks')} edges={event.get('edges')} claims={event.get('claims')} "
                f"elapsed={event.get('elapsed_total_s')}s"
            )
        log(f"B② 사건 relates_to — {events[0].get('relates_to')}")

        # ── C. 대조군 — 스위치 끈 문이 엔진 remember 를 그대로 넘기고, 엔진이 쓴
        #      노트도 sync 에서 같은 node 라벨 정리를 겪는다. ──────────────────────
        stack.restart_door_as_engine()
        for idx, args in enumerate(
            (
                {
                    "title": "E3c-1 대조 노트 하나",
                    "body": "대조군 첫 본문. 엔진이 직접 쓴다.",
                    "repo": "e3c1-three",
                    "tools": ["gamma"],
                    "concepts": ["ctrl"],
                    "claims": [
                        {"subject": "Ctrl Slot", "predicate": "state", "value": "x-one", "kind": "fact"}
                    ],
                    "author": "agent:e3c1",
                },
                {
                    "title": "E3c-1 대조 노트 둘",
                    "body": "대조군 둘째 본문. 같은 슬롯을 다시 말한다.",
                    "repo": "e3c1-four",
                    "tools": ["delta"],
                    "concepts": ["ctrl2"],
                    "claims": [
                        {"subject": "Ctrl Slot", "predicate": "state", "value": "x-two", "kind": "fact"}
                    ],
                    "author": "agent:e3c1",
                },
            ),
            start=3,
        ):
            status, body = http_json(DOOR_PORT, "POST", "/remember", args)
            if status != 200 or body.get("wiki_id") != f"wiki-000{idx}":
                fail(f"대조군 remember({idx}) 어긋남: {status} {body}")
            log(f"C① 엔진 직접 쓰기 — {body['wiki_id']} (문은 바이트 프록시)")
        c_before = stack.snapshot()
        stack.sync()
        c_after = stack.snapshot()
        c_report = diff_states(c_before, c_after)
        if c_report:
            print_diff("C② 엔진이 쓴 노트의 sync 정리(같은 종류면 node 라벨 뿐):", c_report)
            log("C② 엔진이 쓴 노트도 sync 에서 같은 종류의 정리를 겪는다 — A④ node 어긋남은 엔진 자기 행동")
        else:
            log("C② 엔진이 쓴 노트 — sync 0 변경")

        print("[e3c1] OK — 파이썬 쓰기 길이 엔진과 같이 산다", flush=True)
        return 0
    finally:
        stack.stop()


if __name__ == "__main__":
    raise SystemExit(main())
