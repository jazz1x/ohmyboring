#!/usr/bin/env python3
"""remember 쓰기 대조 — 같은 씨앗의 사본 DB·볼트 두 벌에 같은 요청 묶음을 엔진(A)과 문의 파이썬 길(B)로 치고 비교한다.

C-PORT3 2단계. 넘김(DOOR_REMEMBER_WRITER=engine) 전에 한 번 돌린다 — CI 가 아니다.

  A  엔진(호스트 바이너리, 이 워크트리 빌드)이 사본 A·볼트 사본 A 를 물고 있다. 요청은 엔진에 곧장.
  B  문(uvicorn, DOOR_REMEMBER_WRITER=python)이 사본 B·볼트 사본 B 를 쓴다. 요청은 문에 곧장. 문은 JSON 객체가
     아닌 본문을 엔진으로 넘기는 설계라(door.py _http_remember_arguments) 그 갈래의 상류만 엔진 A 로 둔다
     (거절만 하고 아무것도 안 쓴다 — 표의 해당 줄에 적는다).

쓰는 재료는 verdict-parity.py 에서(사본 DSN 단언·다중집합 차이·표 정규화·종료코드 규약), e3c1-verify.py 에서
(일회용 pgvector 컨테이너·엔진/문 기동·health 기다림·임시 boring.json 을 가져왔다. 운영 DB 는 pg_dump 읽기만
한다. 볼트는 --live-vault 를 읽기만 하고 복사본에만 쓴다. 만든 컨테이너·임시 디렉터리는 이 파일 안에서
이름으로 치운다(--keep 이 아니면 성공이든 실패든).

일회용 DB 안에 boring_seed 틀을 하나 두어(--reuse DIR) 고칠 때마다 덤프·복원을 다시 하지 않게 한다.

종료코드: 0 PASS · 1 차이 있음 · 2 잰 게 아니다(장비 불통·갈래 0건·운영 DSN).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OWNER_TOKEN = "p3p-owner-token"
OWNER_HEADER = "x-boring-owner-token"
NAMES = {"a": "p3p-a", "b": "p3p-b"}
ENGINE, DOOR, NETWORK = "p3p-eng", "p3p-door", "p3p-net"
OLLAMA = "http://host.docker.internal:11434/v1"
PG_IMAGE = "pgvector/pgvector:pg16"
EVENTS = ("dedup_decision", "owner_supersede_refused")

EXCLUDED = (
    "known differences (C-PORT3 draft): remember_written event (python only); relates_to / anchor_hash / anchor_symbol "
    "(engine fills right after the write via project_note, python defers) and what they change: the note-file relates_to "
    "lines, document.sha, edges of kind relates_to; plus one emitter habit documented in render.py:6-9: python quotes the "
    "frontmatter date (date: '2026-10-27'), the engine does not - counted in its own column, YAML reads both as the same string"
)
NORMALISED = (
    "time columns folded to 'T' when >= the run start read from each DB's now(): document.updated_at, claim.valid_from, "
    "claim.superseded_at, edge.first_seen_at; node.label has ISO timestamps replaced by 'T'; events drop ts/otel/run_id and null-valued keys (the door sink drops nulls, the engine stores them); "
    "chunk.content and claim/chunk embeddings compared as md5"
)


class Unmeasured(Exception):
    """잰 게 아니다 — 종료코드 2."""


def log(msg: str) -> None:
    print(f"[remember-parity] {msg}", flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ── 요청 묶음 ───────────────────────────────────────────────────────────────


@dataclass
class Case:
    cid: str
    path: str  # http | mcp
    branch: str  # 표 첫 칸
    args: object
    owner: bool = False
    raw: bytes | None = None  # 본문 글자 그대로 (잘못된 몸)
    supersedes_of: str | None = None  # 이 케이스가 쓴 노트를 교정 대상으로 쓴다 (case id)
    title: str = ""


def _words(topic: str, n: int) -> str:
    """주제마다 겹치지 않는 낱말 — 케이스끼리 토큰 겹침·임베딩 근접으로 뜻밖의 중복이 안 나게 한다."""
    return " ".join("w" + hashlib.md5(f"{topic}{i}".encode()).hexdigest()[:6] for i in range(n))


def _body(topic: str, n: int = 3) -> str:
    return "\n".join(
        f"## {topic} {i}\n{_words(topic + str(i), 14)} src/{topic}_{i}.py:{i + 7}" for i in range(n)
    )


def build_cases() -> list[Case]:
    cases: list[Case] = []
    for p in ("http", "mcp"):

        def add(cid, branch, args, gate=False, p=p, **kw):
            if isinstance(args, dict) and not gate and "claims" not in args and "repo" not in args:
                args = {**args, "repo": "oh-my-boring"}  # 증류 게이트: 프로젝트나 claim 이 있어야 쓴다
            title = args.get("title", "") if isinstance(args, dict) else ""
            cases.append(Case(f"{p}.{cid}", p, branch, args, title=title, **kw))

        def claim(i, said_by=None, p=p):
            return {
                "subject": f"p3p-{p}-{i}",
                "predicate": f"pred{i}",
                "value": _words(f"{p}claim{i}", 3),
                **({"said_by": said_by} if said_by else {}),
            }

        sess = f"p3p-sess-{p}"
        tools = ["alpha-tool", "beta-tool", "gamma-tool", "delta-tool"]
        concepts = ["strangler", "parity", "dedupe"]
        add(
            "new",
            "new note",
            {
                "title": f"p3p {p} new {_words(p + 'new', 2)}",
                "body": _body(f"new{p}"),
                "tools": ["zeta-tool"],
                "concepts": ["fresh"],
                "claims": [claim(0)],
                "repo": "oh-my-boring",
            },
        )
        add(
            "same_seed",
            "new note",
            {
                "title": f"p3p {p} seed {_words(p + 'seed', 3)}",
                "body": _body(f"seed{p}", 2),
                "omb_session_id": sess,
                "omb_session_since": "100",
                "tools": tools,
                "concepts": concepts,
                "claims": [claim(1)],
            },
        )
        add(
            "same_skip",
            "same_session",
            {
                "title": f"p3p {p} skip {_words(p + 'skip', 3)}",
                "body": _words(p + "skipbody", 5),
                "omb_session_id": sess,
                "omb_session_since": "100",
                "claims": [claim(2)],
            },
        )
        add(
            "same_replace",
            "same_session",
            {
                "title": f"p3p {p} replace {_words(p + 'replace', 3)}",
                "body": _body(f"replace{p}", 6) + "\n명령: pytest -q 통과 근거 https://example.org/e",
                "omb_session_id": sess,
                "omb_session_since": "100",
                "tools": tools + ["eps-tool"],
                "concepts": concepts,
                "claims": [claim(i) for i in range(3, 7)],
            },
        )
        add(
            "continued",
            "continued session",
            {
                "title": _words(p + "conttitle", 4),
                "body": _body(f"cont{p}", 2),
                "omb_session_id": sess,
                "omb_session_since": "200",
                "tools": [_words(p + "ct", 1)],
                "concepts": [_words(p + "cc", 1)],
                "claims": [
                    {
                        "subject": _words(p + "cs", 1),
                        "predicate": _words(p + "cp", 1),
                        "value": _words(p + "cv", 3),
                    }
                ],
            },
        )
        psess = f"p3p-probable-{p}"
        pbody = _words(p + "probable", 30)
        pargs = {"tools": tools, "concepts": concepts, "claims": [claim(8)]}
        add(
            "probable_seed",
            "new note",
            {
                "title": f"p3p probable {_words(p + 'ptitle', 4)}",
                "body": pbody,
                "omb_session_id": psess + "-1",
                **pargs,
            },
        )
        add(
            "probable_dup",
            "probable_session",
            {
                "title": f"p3p probable {_words(p + 'ptitle', 4)} extra",
                "body": pbody + " extra",
                "omb_session_id": psess + "-2",
                **pargs,
            },
        )
        add(
            "exact_seed",
            "new note",
            {"title": f"p3p {p} exact {_words(p + 'exact', 3)}", "body": _body(f"exact{p}", 2)},
        )
        add(
            "exact_dup",
            "exact_title",
            {"title": f"p3p {p} exact {_words(p + 'exact', 3)}", "body": _body(f"exactzz{p}", 2)},
        )
        add(
            "supersedes",
            "supersedes",
            {
                "title": f"p3p {p} correct {_words(p + 'correct', 3)}",
                "body": _body(f"corr{p}", 2),
                "claims": [claim(0)],
            },
            supersedes_of=f"{p}.new",
        )
        add(
            "supersedes_unknown",
            "supersedes unknown",
            {
                "title": f"p3p {p} unknown {_words(p + 'unknown', 3)}",
                "body": _body(f"unk{p}", 2),
                "supersedes": ["wiki-99999999.md"],
            },
        )
        add(
            "owner_seed",
            "new note",
            {
                "title": f"p3p {p} owner {_words(p + 'owner', 3)}",
                "body": _body(f"owner{p}", 2),
                "author": "owner",
                "claims": [claim(9, "owner")],
            },
            owner=True,
        )
        add(
            "owner_refused",
            "supersedes owner note (refused)",
            {"title": f"p3p {p} refuse {_words(p + 'refuse', 3)}", "body": _body(f"refuse{p}", 2)},
            supersedes_of=f"{p}.owner_seed",
        )
        add(
            "gate_refused",
            "gate refusal (no project, no claims)",
            {"title": f"p3p {p} gate {_words(p + 'gate', 3)}", "body": _body(f"gate{p}", 1)},
            gate=True,
        )
        add(
            "empty_body",
            "empty note refusal",
            {"title": f"p3p {p} empty {_words(p + 'empty', 3)}", "body": ""},
        )
        add("empty_title", "empty note refusal", {"title": "", "body": _body(f"et{p}", 1)})
        if p == "http":
            add("bad_syntax", "bad body", {}, raw=b"{not json")
            add("bad_array", "bad body", {}, raw=b'["x"]')
        else:
            add("bad_args", "bad body", "not-an-object")
            add("bad_origin", "bad body", {"title": f"p3p {p} origin", "body": "본문.", "origin": "mars"})
    return cases


# ── 장비 ────────────────────────────────────────────────────────────────────


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_call(base: str, case: Case, args: object, rpc_id: int, owner: bool) -> tuple[int, bytes, float]:
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    if owner:
        headers[OWNER_HEADER] = OWNER_TOKEN
    if case.raw is not None:
        url, data = base + "/remember", case.raw
    elif case.path == "http":
        url, data = base + "/remember", json.dumps(args).encode()
    else:
        rpc = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": "remember", "arguments": args},
        }
        url, data = base + "/mcp", json.dumps(rpc).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        body, status = e.read(), e.code
    except OSError as e:
        raise Unmeasured(f"{base} unreachable: {e}") from e
    return status, body, time.perf_counter() - t0


def written_path(case: Case, body: bytes) -> str | None:
    """응답에서 쓴 노트의 source_path — HTTP 는 칸, MCP 는 본문 글자의 /vault/wiki/… 모양."""
    try:
        if case.path == "http":
            return json.loads(body).get("source_path")
        text = json.loads(body)["result"]["content"][0]["text"]
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None
    m = re.search(r"(?:/vault/)?wiki/wiki-\d+\.md|wiki-\d+\.md", text)
    return m.group(0) if m else None


class Rig:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.reuse = Path(args.reuse).resolve() if args.reuse else None
        self.work = self.reuse or Path(tempfile.mkdtemp(prefix="p3p-", dir=args.work_root or None))
        self.dsn: dict[str, str] = {}

    # 컨테이너 ---------------------------------------------------------------
    def psql(self, side: str, sql: str, db: str = "postgres") -> subprocess.CompletedProcess:
        return run(["docker", "exec", NAMES[side], "psql", "-U", "boring", "-d", db, "-Atc", sql])

    def start_db(self, side: str) -> None:
        if self.reuse is None:
            run(["docker", "rm", "-f", NAMES[side]])
            run(["docker", "network", "create", NETWORK])
            r = run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    NAMES[side],
                    "--network",
                    NETWORK,
                    "-p",
                    "127.0.0.1::5432",
                    "-e",
                    "POSTGRES_USER=boring",
                    "-e",
                    "POSTGRES_PASSWORD=boring",
                    "-e",
                    "POSTGRES_DB=boring",
                    PG_IMAGE,
                ]
            )
            if r.returncode != 0:
                raise Unmeasured(f"pgvector {side}: {r.stderr.strip()}")
        port = run(["docker", "port", NAMES[side], "5432/tcp"]).stdout.strip().split(":")[-1]
        self.dsn[side] = f"postgresql://boring:boring@127.0.0.1:{port}/boring"
        for _ in range(90):
            if (
                run(
                    [
                        "docker",
                        "exec",
                        NAMES[side],
                        "psql",
                        "-U",
                        "boring",
                        "-d",
                        "boring",
                        "-tAc",
                        "SELECT 1",
                    ]
                ).returncode
                == 0
            ):
                return
            time.sleep(1)
        raise Unmeasured(f"pgvector {side} not ready")

    def assert_scratch(self) -> None:
        live_port = (
            run(["docker", "port", self.args.live_container, "5432/tcp"]).stdout.strip().split(":")[-1]
        )
        ports = {s: d.rsplit(":", 1)[-1].split("/")[0] for s, d in self.dsn.items()}
        bad = [s for s, p in ports.items() if p == live_port or p == "5432"]
        if bad or ports["a"] == ports["b"]:
            raise Unmeasured(f"DSN guard FAIL: A={ports['a']} B={ports['b']} live={live_port}")
        log(
            f"DSN guard: A=127.0.0.1:{ports['a']} B=127.0.0.1:{ports['b']} — neither is production ({live_port}), A != B — ok"
        )

    def seed(self) -> None:
        dump = self.work / "live.dump"
        if self.reuse is None:
            with dump.open("wb") as f:
                r = subprocess.run(
                    [
                        "docker",
                        "exec",
                        self.args.live_container,
                        "pg_dump",
                        "-U",
                        "boring",
                        "-d",
                        "boring",
                        "-Fc",
                    ],
                    stdout=f,
                    stderr=subprocess.PIPE,
                )
            if r.returncode != 0:
                raise Unmeasured(f"pg_dump: {r.stderr.decode()[:200]}")
            log(f"pg_dump {dump.stat().st_size // 1_000_000} MB")
            for side in NAMES:
                with dump.open("rb") as f:
                    r = subprocess.run(
                        [
                            "docker",
                            "exec",
                            "-i",
                            NAMES[side],
                            "pg_restore",
                            "-U",
                            "boring",
                            "-d",
                            "boring",
                            "--no-owner",
                            "--no-privileges",
                        ],
                        stdin=f,
                        capture_output=True,
                        text=True,
                    )
                log(f"pg_restore {side}: rc={r.returncode} {r.stderr.strip().splitlines()[-1:]}")
                self.psql(side, "CREATE DATABASE boring_seed TEMPLATE boring")
            seedv = self.work / "seedvault"
            for sub in ("wiki", "rules"):
                shutil.copytree(Path(self.args.live_vault) / sub, seedv / sub)
        else:
            for side in NAMES:
                self.psql(side, "DROP DATABASE IF EXISTS boring WITH (FORCE)")
                self.psql(side, "CREATE DATABASE boring TEMPLATE boring_seed")
        for side in NAMES:
            vault = self.work / f"vault-{side}"
            shutil.rmtree(vault, ignore_errors=True)
            shutil.copytree(self.work / "seedvault", vault)
        docs = [self.psql(s, "SELECT count(*) FROM document", "boring").stdout.strip() for s in NAMES]
        live = run(
            [
                "docker",
                "exec",
                self.args.live_container,
                "psql",
                "-U",
                "boring",
                "-d",
                "boring",
                "-Atc",
                "SELECT count(*) FROM document",
            ]
        ).stdout.strip()
        log(f"document rows live={live} A={docs[0]} B={docs[1]}")
        if docs[0] != docs[1]:
            raise Unmeasured("seed copies differ")

    # 컨테이너 ---------------------------------------------------------------
    def config(self) -> Path:
        p = self.work / "boring.json"
        p.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "llm": {
                        "base_url": OLLAMA,
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
        return p

    def build_images(self) -> None:
        """이 워크트리 HEAD 로 엔진·문 이미지를 만든다(없을 때만) — 운영 이미지·컨테이너는 건드리지 않는다."""
        sha = run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"]).stdout.strip()
        self.engine_image, self.door_image = f"p3p-engine:{sha}", f"p3p-door:{sha}"
        if run(["docker", "image", "inspect", self.engine_image]).returncode != 0:
            log(f"building {self.engine_image} (drudge release build)")
            r = run(
                [
                    "docker",
                    "build",
                    "--build-arg",
                    f"BUILD_SHA={sha}",
                    "-t",
                    self.engine_image,
                    str(REPO_ROOT / "drudge"),
                ]
            )
            if r.returncode != 0:
                raise Unmeasured(f"engine image build failed: {r.stderr[-300:]}")
        if run(["docker", "image", "inspect", self.door_image]).returncode != 0:
            log(f"building {self.door_image}")
            r = run(
                [
                    "docker",
                    "build",
                    "-f",
                    str(REPO_ROOT / "agents/door/Dockerfile"),
                    "-t",
                    self.door_image,
                    str(REPO_ROOT),
                ]
            )
            if r.returncode != 0:
                raise Unmeasured(f"door image build failed: {r.stderr[-300:]}")

    def wait_health(self, port: int, vector: bool | None = None) -> None:
        for _ in range(300):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                    body = json.loads(r.read())
                    if r.status == 200 and (vector is None or body.get("vector") is vector):
                        return
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        raise Unmeasured(f"port {port} did not come up")

    def start_container(
        self,
        name: str,
        image: str,
        env: dict[str, str],
        mounts: list[str],
        inner_port: int,
        vector: bool | None,
    ) -> str:
        run(["docker", "rm", "-f", name])
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            NETWORK,
            "--add-host",
            "host.docker.internal:host-gateway",
            "-p",
            f"127.0.0.1::{inner_port}",
        ]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        for m in mounts:
            cmd += ["-v", m]
        r = run(cmd + [image])
        if r.returncode != 0:
            raise Unmeasured(f"{name}: {r.stderr.strip()[-300:]}")
        port = int(
            run(["docker", "port", name, f"{inner_port}/tcp"]).stdout.strip().splitlines()[0].split(":")[-1]
        )
        self.wait_health(port, vector=vector)
        return f"http://127.0.0.1:{port}"

    def start_engine(self, side: str = "a") -> str:
        name = ENGINE if side == "a" else f"{ENGINE}-b"
        env = {
            "PG_DSN": f"postgresql://boring:boring@{NAMES[side]}:5432/boring",
            "BORING_VECTOR": "on",
            "BORING_HTTP_ADDR": "0.0.0.0:7700",
            "BORING_VAULT_DIR": "/vault",
            "BORING_CONFIG": "/config/boring.json",
            "BORING_LLM_BASE_URL": OLLAMA,
            "BORING_OWNER_TOKEN": OWNER_TOKEN,
            "BORING_SYNC_DISABLED": "1",
            "BORING_COMPACT_DISABLED": "1",
            "BORING_BRIEF_HOUR": "99",
            "TZ": "UTC",
        }
        mounts = [f"{self.work / f'vault-{side}'}:/vault", f"{self.config()}:/config/boring.json:ro"]
        return self.start_container(name, self.engine_image, env, mounts, 7700, True)

    def stop_engine_b(self) -> None:
        run(["docker", "rm", "-f", f"{ENGINE}-b"])

    def start_door(self, upstream_name: str) -> str:
        spool = self.work / "b-spool"
        shutil.rmtree(spool, ignore_errors=True)
        spool.mkdir()
        spool.chmod(0o777)
        self.spool = spool / "events.ndjson"
        env = {
            "DOOR_UPSTREAM": f"http://{upstream_name}:7700",
            "DOOR_PG_DSN": f"postgresql://boring:boring@{NAMES['b']}:5432/boring",
            "DOOR_REMEMBER_WRITER": "python",
            "BORING_VAULT_DIR": "/vault",
            "BORING_CONFIG": "/config/boring.json",
            "BORING_URL": f"http://{upstream_name}:7700",
            "BORING_EVENT_SINK": "spool",
            "BORING_EVENT_LOG": "/spool/events.ndjson",
            "BORING_LLM_BASE_URL": OLLAMA,
            "BORING_OWNER_TOKEN": OWNER_TOKEN,
            "DOOR_PORT": "7710",
            "BORING_IN_CONTAINER": "1",
            "TZ": "UTC",
        }
        mounts = [
            f"{self.work / 'vault-b'}:/vault",
            f"{self.config()}:/config/boring.json:ro",
            f"{spool}:/spool",
        ]
        return self.start_container(DOOR, self.door_image, env, mounts, 7710, None)

    def teardown(self) -> None:
        if self.args.keep:
            log(f"--keep: containers and {self.work} left in place")
            return
        for name in (ENGINE, f"{ENGINE}-b", DOOR, *NAMES.values()):
            run(["docker", "rm", "-f", name])
        run(["docker", "network", "rm", NETWORK])
        for image in (getattr(self, "engine_image", None), getattr(self, "door_image", None)):
            if image:
                run(["docker", "rmi", image])
        shutil.rmtree(self.work, ignore_errors=True)


# ── 스냅숏·정규화 ───────────────────────────────────────────────────────────

T = "CASE WHEN {c} >= %(s)s THEN 'T' ELSE {c}::text END"
SNAP_SQL = {
    "document": "SELECT source_path, origin, project, kind, title, tags::text, extracted_sha, "
    + T.format(c="updated_at")
    + ", code_sha, author FROM document",
    "chunk": "SELECT id, source_path, md5(content), origin, project, kind, chunk_idx, md5(embedding::text) FROM chunk",
    "claim": "SELECT subject, predicate, value, source_path, "
    + T.format(c="valid_from")
    + ", "
    + "CASE WHEN superseded_at IS NULL THEN NULL ELSE "
    + T.format(c="superseded_at")
    + " END, md5(embedding::text), kind, confidence,"
    " anchor, stale_at::text, stale_reason, era, said_by FROM claim",
    "node": "SELECT id, kind, label, outcome FROM node",
    "edge": "SELECT src, dst, kind, judge, "
    + T.format(c="first_seen_at")
    + " FROM edge WHERE kind <> 'relates_to'",
}
ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


def connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


def snapshot(dsn: str, start) -> dict[str, list]:
    out: dict[str, list] = {}
    with connect(dsn) as conn, conn.cursor() as cur:
        for name, sql in SNAP_SQL.items():
            cur.execute(sql, {"s": start})
            rows = [tuple(r) for r in cur.fetchall()]
            if name == "node":
                rows = [(r[0], r[1], ISO.sub("T", r[2] or ""), r[3]) for r in rows]
            out[name] = rows
    return out


def delta(before: list, after: list) -> tuple[list, list]:
    added, removed = Counter(after) - Counter(before), Counter(before) - Counter(after)
    return sorted(added.elements(), key=repr), sorted(removed.elements(), key=repr)


def diff_rows(a: list, b: list) -> tuple[list, list]:
    return sorted((Counter(a) - Counter(b)).elements(), key=repr), sorted(
        (Counter(b) - Counter(a)).elements(), key=repr
    )


RELATES = re.compile(r"^relates_to:.*\n(?:[ \t]*- .*\n)*", re.MULTILINE)


def vault_files(vault: Path) -> dict[str, str]:
    wiki = vault / "wiki"
    return {n: (wiki / n).read_text(encoding="utf-8") for n in sorted(os.listdir(wiki)) if n.endswith(".md")}


def fold_date(text: str) -> str:
    """머리말 date 칸의 따옴표만 접는다 — 문 쪽은 `date: '2026-10-27'`, 엔진은 `date: 2026-10-27` (표에는 따로 센다)."""
    return re.sub(r"^date: '(\d{4}-\d{2}-\d{2})'$", r"date: \1", text, flags=re.MULTILINE)


def strip_relates(text: str) -> str:
    return RELATES.sub("", text)


def engine_events(dsn: str, after: int) -> list[dict]:
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT attributes FROM event_log WHERE id > %s AND event_name = ANY(%s) ORDER BY id",
            (after, list(EVENTS)),
        )
        return [dict(r[0]) for r in cur.fetchall()]


def door_events(spool: Path) -> list[dict]:
    if not spool.exists():
        return []
    rows = [json.loads(line) for line in spool.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("event") in EVENTS]


def norm_event(e: dict) -> str:
    return json.dumps(
        {k: v for k, v in e.items() if k not in ("ts", "otel", "run_id") and v is not None},
        sort_keys=True,
        ensure_ascii=False,
    )


def decision_of(e: dict) -> str:
    return ":".join(str(e.get(k)) for k in ("event", "status", "reason", "decision") if e.get(k) is not None)


# ── 본체 ────────────────────────────────────────────────────────────────────


@dataclass
class Out:
    case: Case
    a: tuple[int, bytes, float] | None = None
    b: tuple[int, bytes, float] | None = None
    path_a: str | None = None
    path_b: str | None = None
    target: str | None = None
    bytes_same: bool = False
    problems: list[str] = field(default_factory=list)


def outcome(ans: tuple) -> str:
    """응답 한 줄 요약 — 사건이 없는 갈래(거절·잘못된 몸)가 실제로 일어났는지 응답으로 센다."""
    try:
        data = json.loads(ans[1])
    except ValueError:
        return f"{ans[0]}:non-json"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return f"{ans[0]}:rpc{err.get('code')}"
    return f"{ans[0]}:error" if err else f"{ans[0]}:ok"


def same_response(a: tuple, b: tuple) -> bool:
    """상태가 같고 JSON 으로 읽은 값이 같다 — 글자 그대로 같은지는 따로 센다(키 순서만 다른 답이 있다)."""
    if a[0] != b[0]:
        return False
    try:
        return json.loads(a[1]) == json.loads(b[1])
    except ValueError:
        return a[1] == b[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="remember 쓰기 대조 (C-PORT3 2단계) — 엔진(A) 대 문의 파이썬 길(B)"
    )
    ap.add_argument(
        "--live-vault",
        default=os.environ.get("PARITY_LIVE_VAULT"),
        help="라이브 볼트 디렉터리 (읽기만; 환경 PARITY_LIVE_VAULT)",
    )
    ap.add_argument(
        "--live-container", default="boring-postgres", help="pg_dump 를 읽을 라이브 postgres 컨테이너"
    )
    ap.add_argument("--out", required=True, help="결과 TSV 경로")
    ap.add_argument("--work-root", default=None, help="임시 디렉터리를 만들 곳 (기본 시스템 임시)")
    ap.add_argument(
        "--keep", action="store_true", help="끝나도 컨테이너·임시 디렉터리를 남긴다 (--reuse 로 이어 쓴다)"
    )
    ap.add_argument(
        "--reuse",
        default=None,
        help="--keep 으로 남긴 디렉터리 — 덤프·복원을 건너뛰고 boring_seed 틀에서 다시 시작",
    )
    args = ap.parse_args(argv)
    if not args.reuse and not args.live_vault:
        ap.error("--live-vault (or PARITY_LIVE_VAULT) is required")

    rig = Rig(args)
    try:
        return measure(rig, args)
    except Unmeasured as e:
        print(f"잰 게 아니다: {e}", file=sys.stderr)
        return 2
    finally:
        rig.teardown()


def measure(rig: Rig, args: argparse.Namespace) -> int:
    for side in NAMES:
        rig.start_db(side)
    rig.assert_scratch()
    rig.build_images()
    rig.build_images()
    rig.seed()
    # 엔진은 기동할 때 스키마 이전(claim 되살림 등)을 돈다 — 라이브 엔진이 옛 빌드면 덤프에는 그 이전이 안 돼 있어
    # 엔진 A 만 기동 직후 사본이 달라진다. B 쪽에도 같은 빌드를 한 번 띄웠다 내려 씨앗을 같게 한다.
    rig.start_engine("b")
    rig.stop_engine_b()
    base_a = rig.start_engine("a")
    base_b = rig.start_door(ENGINE)
    log(f"engine A {base_a}, door B {base_b} (upstream for non-object bodies = engine A)")

    with connect(rig.dsn["a"]) as ca, ca.cursor() as cur:
        cur.execute("SELECT coalesce(max(id),0), now() FROM event_log")
        mark_a, start_a = cur.fetchone()
    with connect(rig.dsn["b"]) as cb, cb.cursor() as cur:
        cur.execute("SELECT now()")
        start_b = cur.fetchone()[0]
    snap_a0, snap_b0 = snapshot(rig.dsn["a"], start_a), snapshot(rig.dsn["b"], start_b)
    files_a0, files_b0 = vault_files(rig.work / "vault-a"), vault_files(rig.work / "vault-b")
    differing = [k for k in snap_a0 if Counter(snap_a0[k]) != Counter(snap_b0[k])] + (
        ["vault files"] if files_a0 != files_b0 else []
    )
    if differing:
        sample = {k: diff_rows(snap_a0[k], snap_b0[k]) for k in differing if k in snap_a0}
        raise Unmeasured(
            f"seed copies differ before the batch: {differing} {str({k: (v[0][:2], v[1][:2]) for k, v in sample.items()})[:600]}"
        )

    cases = build_cases()
    outs: dict[str, Out] = {}
    for i, case in enumerate(cases, 1):
        out = Out(case)
        sides = {}
        for side, base in (("a", base_a), ("b", base_b)):
            args_for = case.args
            if case.supersedes_of:
                # 실제 호출자가 넘기는 모양(/vault/wiki/<이름>)으로 — 두 쪽 같은 글자.
                target = getattr(outs[case.supersedes_of], f"path_{side}")
                shaped = "/vault/wiki/" + os.path.basename(target) if target else "wiki-missing.md"
                args_for = {**case.args, "supersedes": [shaped]}
                out.target = shaped
            sides[side] = http_call(base, case, args_for, i, case.owner)
            setattr(out, f"path_{side}", written_path(case, sides[side][1]))
        out.a, out.b = sides["a"], sides["b"]
        outs[case.cid] = out
        out.bytes_same = out.a[1] == out.b[1]
        if not same_response(out.a, out.b):
            out.problems.append(
                f"response: A {out.a[0]} {out.a[1].decode(errors='replace')[:160]!r} / "
                f"B {out.b[0]} {out.b[1].decode(errors='replace')[:160]!r}"
            )

    # 문의 사건은 백그라운드 큐라 A 가 낸 개수에 닿을 때까지 읽는다.
    ev_a = engine_events(rig.dsn["a"], mark_a)
    for _ in range(60):
        ev_b = door_events(rig.spool)
        if len(ev_b) >= len(ev_a):
            break
        time.sleep(0.5)
    ev_b = door_events(rig.spool)

    snap_a1, snap_b1 = snapshot(rig.dsn["a"], start_a), snapshot(rig.dsn["b"], start_b)
    table_diffs = {}
    for name in SNAP_SQL:
        da, db_ = delta(snap_a0[name], snap_a1[name]), delta(snap_b0[name], snap_b1[name])
        table_diffs[name] = (diff_rows(da[0], db_[0]), diff_rows(da[1], db_[1]), len(da[0]), len(da[1]))

    files_a1, files_b1 = vault_files(rig.work / "vault-a"), vault_files(rig.work / "vault-b")
    changed = sorted(
        n
        for n in set(files_a1) | set(files_b1)
        if files_a0.get(n) != files_a1.get(n) or files_b0.get(n) != files_b1.get(n)
    )
    file_diffs = []
    identical = relates_only = date_quote_only = 0
    for n in changed:
        ta, tb = files_a1.get(n), files_b1.get(n)
        if ta == tb:
            identical += 1
        elif ta is not None and tb is not None and strip_relates(ta) == strip_relates(tb):
            relates_only += 1
        elif (
            ta is not None and tb is not None and fold_date(strip_relates(ta)) == fold_date(strip_relates(tb))
        ):
            date_quote_only += 1
        else:
            file_diffs.append((n, ta, tb))

    # 사건: dedup_decision 은 노트 제목으로, owner_supersede_refused 는 겨눈 대상 이름으로 케이스에 묶는다.
    global_problems: list[str] = []

    def event_key(e: dict) -> str:
        if e.get("event") == "owner_supersede_refused":
            return "refused:" + os.path.basename((e.get("targets") or ["?"])[0])
        return e.get("incoming_title") or "?"

    def keys_of(o: Out) -> list[str]:
        return [o.case.title] + (["refused:" + os.path.basename(o.target)] if o.target else [])

    def group(events: list[dict]) -> dict[str, list[dict]]:
        out_: dict[str, list[dict]] = {}
        for e in events:
            out_.setdefault(event_key(e), []).append(e)
        return out_

    evt_a, evt_b = group(ev_a), group(ev_b)

    def events_of(evt: dict[str, list[dict]], o: Out) -> list[dict]:
        return [e for k in keys_of(o) for e in evt.get(k, [])]

    for o in outs.values():
        ea, eb = events_of(evt_a, o), events_of(evt_b, o)
        if sorted(map(norm_event, ea)) != sorted(map(norm_event, eb)):
            o.problems.append(
                f"events: A {[norm_event(e)[:240] for e in ea]} / B {[norm_event(e)[:240] for e in eb]}"
            )
    claimed = {k for o in outs.values() for k in keys_of(o)}
    for t in sorted((set(evt_a) | set(evt_b)) - claimed):
        global_problems.append(
            f"event not tied to any case: {t!r} A={len(evt_a.get(t, []))} B={len(evt_b.get(t, []))}"
        )

    # 행·파일 차이를 케이스에 붙인다 (경로가 행/파일에 나오는 케이스).
    def blame(texts: list[str], label: str) -> None:
        for text in texts:
            hit = False
            for o in outs.values():
                paths = [p for p in (o.path_a, o.path_b) if p]
                if any(os.path.basename(p) in text for p in paths):
                    o.problems.append(f"{label}: {text[:200]}")
                    hit = True
            if not hit:
                global_problems.append(f"{label}: {text[:200]}")

    for name, ((a_add, b_add), (a_rm, b_rm), _, _) in table_diffs.items():
        blame(
            [f"{name} A-only+ {r!r}" for r in a_add[:5]]
            + [f"{name} B-only+ {r!r}" for r in b_add[:5]]
            + [f"{name} A-only- {r!r}" for r in a_rm[:5]]
            + [f"{name} B-only- {r!r}" for r in b_rm[:5]],
            f"rows {name}",
        )
    blame(
        [f"{n}: A={(ta or '')[:300]!r} B={(tb or '')[:300]!r}" for n, ta, tb in file_diffs[:5]], "note file"
    )

    # 갈래 발생 셈 (사건 기준 — 보낸 수가 아니라 실제로 일어난 수).
    occurred: dict[tuple[str, str], list[str]] = {}
    for o in outs.values():
        for side, evt in (("A", evt_a), ("B", evt_b)):
            for e in events_of(evt, o):
                occurred.setdefault((o.case.branch, o.case.path), []).append(f"{side}:{decision_of(e)}")
    sent = Counter((o.case.branch, o.case.path) for o in outs.values())
    rows_out = []
    for (branch, path), n in sorted(sent.items()):
        members = [o for o in outs.values() if (o.case.branch, o.case.path) == (branch, path)]
        problems = [p for o in members for p in o.problems]
        ev = occurred.get((branch, path), [])
        happened_a = sum(1 for x in ev if x.startswith("A:"))
        happened_b = sum(1 for x in ev if x.startswith("B:"))
        rows_out.append(
            [
                branch,
                path,
                n,
                happened_a,
                happened_b,
                ";".join(sorted(set(x[2:] for x in ev))) or "-",
                "PASS" if not problems else "FAIL",
                f"{sum(o.bytes_same for o in members)}/{len(members)}",
                "A "
                + ",".join(sorted({outcome(o.a) for o in members}))
                + " | B "
                + ",".join(sorted({outcome(o.b) for o in members})),
                " | ".join(problems)[:600],
            ]
        )

    # 갈래가 실제로 일어났는지 — 사건이 없는 갈래(새 노트·빈 노트·잘못된 몸·unknown)는 응답 모양으로 센다.
    needs_event = {"same_session", "probable_session", "exact_title", "supersedes owner note (refused)"}
    gaps = [f"{r[0]}/{r[1]}" for r in rows_out if r[0] in needs_event and (r[3] == 0 or r[4] == 0)]
    for o in outs.values():
        if (
            o.case.cid.endswith("supersedes_unknown")
            and b'"unknown":[]' in (o.a[1] or b"")
            and o.case.path == "http"
        ):
            gaps.append(f"{o.case.branch}/{o.case.path}: unknown list empty")
    continued_stored = [
        o
        for o in outs.values()
        if o.case.branch == "continued session"
        and any(decision_of(e).startswith("dedup_decision:stored") for e in events_of(evt_a, o))
    ]
    if len(continued_stored) < 2:
        gaps.append(f"continued session: stored {len(continued_stored)}/2")

    lat = {s: sorted(getattr(o, s)[2] for o in outs.values() if getattr(o, s)[0] == 200) for s in ("a", "b")}

    def pct(xs: list[float], q: int) -> str:
        return (
            f"{statistics.quantiles(xs, n=100, method='inclusive')[q - 1] * 1000:.0f}" if len(xs) > 1 else "-"
        )

    table_lines = [
        ["table", "A rows added", "A rows removed", "A-only/B-only added", "A-only/B-only removed", "verdict"]
    ]
    table_pass = True
    for name, ((a_add, b_add), (a_rm, b_rm), n_add, n_rm) in table_diffs.items():
        same = not (a_add or b_add or a_rm or b_rm)
        table_pass &= same
        table_lines.append(
            [
                name,
                n_add,
                n_rm,
                f"{len(a_add)}/{len(b_add)}",
                f"{len(a_rm)}/{len(b_rm)}",
                "PASS" if same else "FAIL",
            ]
        )
    file_pass = not file_diffs
    all_pass = (
        all(r[6] == "PASS" for r in rows_out)
        and table_pass
        and file_pass
        and not gaps
        and not global_problems
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(["# normalisation: " + NORMALISED, "excluded: " + EXCLUDED])]
    lines.append(
        "\t".join(
            [
                "branch",
                "path",
                "sent",
                "occurred_A",
                "occurred_B",
                "decisions",
                "verdict",
                "bytes_identical",
                "responses",
                "first_diff",
            ]
        )
    )
    lines += ["\t".join(map(str, r)) for r in rows_out]
    lines += ["", "\t".join(map(str, table_lines[0]))] + ["\t".join(map(str, r)) for r in table_lines[1:]]
    lines += [
        "",
        "\t".join(
            [
                "note_files_changed",
                "byte_identical",
                "differ_only_in_relates_to_line",
                "differ_only_in_date_quote (+relates_to)",
                "differ_beyond_that",
                "verdict",
            ]
        ),
        "\t".join(
            map(
                str,
                [
                    len(changed),
                    identical,
                    relates_only,
                    date_quote_only,
                    len(file_diffs),
                    "PASS" if file_pass else "FAIL",
                ],
            )
        ),
    ]
    lines += ["", "\t".join(["latency_ms (HTTP 200 only)", "n", "p50", "p95"])]
    for s, label in (("a", "A engine"), ("b", "B python door")):
        lines.append("\t".join(map(str, [label, len(lat[s]), pct(lat[s], 50), pct(lat[s], 95)])))
    lines += [
        "",
        "\t".join(["gaps (branch never happened)", "; ".join(gaps) or "none"]),
        "\t".join(["global differences", " | ".join(global_problems)[:600] or "none"]),
        "\t".join(["RESULT", "PASS" if all_pass else "NOT PASS"]),
    ]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # 케이스별 응답 전문(볼트 경로는 /vault 로 접음) — 표의 첫 차이를 거슬러 읽는 근거.
    detail = ["\t".join(["case", "side", "status", "ms", "response"])]
    for o in outs.values():
        for side, ans in (("A", o.a), ("B", o.b)):
            detail.append(
                "\t".join(
                    [o.case.cid, side, str(ans[0]), f"{ans[2] * 1000:.0f}", ans[1].decode(errors="replace")]
                )
            )
    out_path.with_name(out_path.stem + "-cases.tsv").write_text("\n".join(detail) + "\n", encoding="utf-8")
    print("\n".join(lines))
    if gaps:
        return 2
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
