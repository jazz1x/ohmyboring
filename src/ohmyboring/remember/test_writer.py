#!/usr/bin/env python3
"""remember 쓰기 시험 — 문 안의 파이썬 쓰기 길(writer.py)이 엔진 remember 와 같은 결정·같은
행·같은 응답을 내는지 가짜 연결(문자 그대로의 SQL 기록)·임시 볼트·가짜 임베딩으로 본다.

Run: python3 ohmyboring/remember/test_writer.py   (no pytest dependency)

갈래별: 새 노트(번호 max+1·파일·행·간선·claim·사건) · 걸너뜀(아무것도 안 씀, 응답 duplicate)
· 대체(supersedes 간선 + 다시 말한 슬롯만 닫는 부분 봉인 SQL) · 이름한 교정(unknown 세기)
· PII 차단(쓰기 0) · owner 거절(쓰기 0, owner_supersede_refused 사건) · 응답 모양.

Mutation targets: 기본값·max+1·부분 봉인·PII 게이트·owner 거절·응답 문장을 바꾸는 변이가
각각 여기서 사망 확인. 문의 스위치 갈래(기본 engine)는 agents/door/test_door.py 가 본다.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "src", ROOT / "agents" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from vault_note import split_frontmatter  # noqa: E402

from ohmyboring.remember import writer  # noqa: E402
from ohmyboring.remember.parse import Claim  # noqa: E402
from ohmyboring.remember.render import render_wiki_note  # noqa: E402
from ohmyboring.result import Err, Ok  # noqa: E402

_DIM = 1024


class _FakeCursor:
    """문자 그대로의 SQL 과 파라미터를 기록하고, 핸들러가 낸 행을 fetch 로 돌려준다."""

    def __init__(self, handler, log: list) -> None:
        self._handler = handler
        self._log = log
        self._rows: list = []

    def execute(self, sql, params=None):
        self._log.append((sql, params))
        self._rows = self._handler(sql, params)
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConn:
    def __init__(self, handler, log: list) -> None:
        self._handler = handler
        self._log = log
        self.exited = False

    def cursor(self):
        return _FakeCursor(self._handler, self._log)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.exited = True
        return False


def _sha_rows(log, params, sha_by_path) -> list:
    """document INSERT 가 기록된 경로는 그 뒤의 sha 조회에 보인다(문 안의 진짜와 같이)."""
    sha = (sha_by_path or {}).get(params["path"])
    if sha:
        return [(sha,)]
    for stmt, stmt_params in reversed(log):
        if stmt.startswith("INSERT INTO document") and stmt_params["path"] == params["path"]:
            return [(stmt_params["sha"],)]
    return []


def _handler(log, doc_paths=(), sha_by_path=None, owner_paths=(), claim_rows=()):
    """SELECT 별 정해진 답 — 나머지는 영향 없는 빈 결과."""

    def handle(sql, params):
        rows: list = []
        if "AND author = 'owner'" in sql:
            rows = [(p,) for p in owner_paths]
        elif sql.startswith("SELECT source_path FROM document"):
            rows = [(p,) for p in doc_paths]
        elif sql.startswith("SELECT sha FROM document"):
            rows = _sha_rows(log, params, sha_by_path)
        elif sql.startswith("SELECT 1 FROM claim"):
            rows = list(claim_rows)
        return rows

    return handle


def _embed(text: str):
    return Ok([0.001] * _DIM)


class _Events:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def append(self, component, event, status, **fields):
        self.rows.append((component, event, status, fields))
        return True

    def by_event(self, name: str) -> list[dict]:
        return [{"status": status, **fields} for (_c, event, status, fields) in self.rows if event == name]


class WriterCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="e3c1-writer-"))
        self.vault = str(self._tmp / "vault")
        os.makedirs(os.path.join(self.vault, "wiki"))
        os.makedirs(os.path.join(self.vault, "rules"))
        self.sql: list[tuple] = []
        self.events = _Events()

    def p(self, name: str) -> str:
        """문 안의 진짜 경로 규약 — writer가 낼 절대 경로(임시 볼트 기준)."""
        return os.path.join(self.vault, "wiki", name)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────────

    def deps(self, handler=None, *, is_owner=False, scanner=None, embed=None, doc_paths=(), **handler_kw):
        handler = handler or _handler(self.sql, doc_paths=doc_paths, **handler_kw)
        return writer.WriterDeps(
            vault_dir=self.vault,
            connect=lambda: _FakeConn(handler, self.sql),
            read_note=lambda vault, note_id: _read_note(vault, note_id),
            split_frontmatter=split_frontmatter,
            list_notes=lambda: sorted(
                name[: -len(".md")]
                for name in os.listdir(os.path.join(self.vault, "wiki"))
                if name.endswith(".md")
            ),
            pii_scanner=scanner,
            is_owner=is_owner,
            nearest_document=lambda text, exclude: Ok(None),
            embed=embed or _embed,
            append_event=self.events.append,
            clock=lambda: datetime(2026, 10, 2, tzinfo=UTC),
        )

    def write(self, arguments, deps=None):
        request = writer.WriteRequest(
            route="remember", arguments=arguments, omb_session_id=arguments.get("omb_session_id")
        )
        return writer.run_write(request, deps or self.deps())

    def seed_note(self, note_id: str, title: str, body: str, **front) -> str:
        """임시 볼트에 디스크 노트 하나 — 중복 문 스캔이 읽는 것."""
        wiki_id = note_id
        defaults = dict(
            title=title,
            kind="note",
            origin="personal",
            project="",
            date="2026-09-30",
            tags=(),
            tools=(),
            concepts=(),
            claims=(),
            sources=(),
            omb_session_id=None,
            author="unknown",
        )
        defaults.update(front)
        from ohmyboring.remember.parse import FrontMatter

        fm = FrontMatter(**defaults)
        text = render_wiki_note(wiki_id, fm, body)
        path = os.path.join(self.vault, "wiki", f"{wiki_id}.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return f"/vault/wiki/{wiki_id}.md"

    def sql_with(self, needle: str) -> list[tuple]:
        return [(sql, params) for (sql, params) in self.sql if needle in sql]

    def written_files(self) -> list[str]:
        return sorted(os.listdir(os.path.join(self.vault, "wiki")))

    def block_rule(self) -> None:
        """PII block 규칙 하나 — SECRET+digits 를 막는다."""
        rules = Path(self.vault) / "rules" / "pii.yaml"
        rules.write_text(
            "version: '1.0'\n"
            "policy:\n"
            "  default_action: flag\n"
            "rules:\n"
            "  - name: block_test\n"
            "    regex: 'SECRET\\d+'\n"
            "    action: block\n"
            "    severity: critical\n"
            "    reason: 시험 차단 규칙\n",
            encoding="utf-8",
        )
        from ohmyboring.remember.pii import load_from_vault

        match load_from_vault(self.vault):
            case Ok(scanner):
                self._scanner = scanner
            case Err(reason):
                self.fail(f"pii fixture: {reason}")

    # ── 새 노트 ──────────────────────────────────────────────────────────────

    def test_stored_writes_note_rows_edges_claims_and_events(self):
        args = {
            "title": "E3c-1 저장 시험",
            "body": "본문이다. ## 검증\n명령 결과를 기록한다.",
            "tags": ["e3c1"],
            "tools": ["Python", "FastAPI"],
            "concepts": ["door"],
            "claims": [
                {
                    "subject": "Door",
                    "predicate": "Writes",
                    "value": "notes",
                    "kind": "fact",
                    "confidence": "certain",
                }
            ],
            "author": "agent:tester",
        }
        deps = self.deps()
        outcome = self.write(args, deps)
        self.assertIsInstance(outcome, writer.Written)
        assert isinstance(outcome, writer.Written)
        self.assertEqual(outcome.wiki_id, "wiki-0001")
        self.assertIsNone(outcome.duplicate)
        self.assertEqual((outcome.supersedes, outcome.unknown), (0, 0))
        self.assertEqual(
            outcome.message,
            "remembered → wiki/wiki-0001.md · chunks 1 · graph(tools 2 concepts 1 claims 1) — recallable now",
        )

        # 파일 — id·날짜(시계 주입)·relates_to 빈 칸·author.
        note_path = os.path.join(self.vault, "wiki", "wiki-0001.md")
        self.assertEqual(self.written_files(), ["wiki-0001.md"])
        text = Path(note_path).read_text(encoding="utf-8")
        self.assertIn("id: wiki-0001", text)
        self.assertIn("date: '2026-10-02'", text)
        self.assertIn("relates_to: []", text)
        self.assertIn("author: agent:tester", text)

        # document 행 — sha 는 파일 내용 해시(읽어 온 것과 같아야 sync 가 0 변경으로 본다).
        doc = self.sql_with("INSERT INTO document")[0][1]
        self.assertEqual(doc["path"], self.p("wiki-0001.md"))
        self.assertEqual(doc["title"], "E3c-1 저장 시험")
        self.assertEqual(doc["author"], "agent:tester")
        self.assertEqual(doc["tags"], ["e3c1"])
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.assertEqual(doc["sha"], sha)

        # chunk 행 — path#0, 본문 그대로, 이어 prune.
        chunks = self.sql_with("INSERT INTO chunk")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][1]["id"], f"{self.p('wiki-0001.md')}#0")
        self.assertIn("본문이다.", chunks[0][1]["content"])
        prunes = self.sql_with("DELETE FROM chunk")
        self.assertEqual(prunes[0][1]["from_idx"], 1)

        # 간선 — uses 둘·about 하나·claims 하나·tagged 하나, 프로젝트 없어 in_project 없음.
        edges = [p for (_s, p) in self.sql_with("INSERT INTO edge")]
        kinds = sorted((e["kind"], e["dst"]) for e in edges)
        self.assertIn(("uses", "tool:python"), kinds)
        self.assertIn(("uses", "tool:fastapi"), kinds)
        self.assertIn(("about", "concept:door"), kinds)
        self.assertIn(("claims", "claim:door:writes"), kinds)
        self.assertIn(("tagged", "topic:e3c1"), kinds)
        self.assertNotIn("in_project", [k for k, _ in kinds])

        # claim 행 — 캐논 슬롯·era unanchored·fact 봉인은 fact 범위 SQL.
        claim = self.sql_with("INSERT INTO claim")[0][1]
        self.assertEqual(claim["subject"], "door")
        self.assertEqual(claim["predicate"], "writes")
        self.assertEqual(claim["kind"], "fact")
        self.assertEqual(claim["confidence"], "certain")
        self.assertIsNone(claim["anchor"])
        self.assertEqual(len(self.sql_with("seal old claims")), 0)
        self.assertTrue(self.sql_with("FROM (SELECT c.subject, c.predicate, max(c.valid_from)"))
        self.assertTrue(self.sql_with("SET superseded_at = NULL"))

    def test_stored_emits_dedup_and_written_events(self):
        args = {
            "title": "E3c-1 저장 시험",
            "body": "본문이다. ## 검증\n명령 결과를 기록한다.",
            "tags": ["e3c1"],
            "tools": ["Python", "FastAPI"],
            "concepts": ["door"],
            "claims": [
                {
                    "subject": "Door",
                    "predicate": "Writes",
                    "value": "notes",
                    "kind": "fact",
                    "confidence": "certain",
                }
            ],
            "author": "agent:tester",
        }
        self.write(args)
        # 사건 — dedup_decision(엔진 이름·모양) + remember_written(대조가 남는 한 줄).
        dedup = self.events.by_event("dedup_decision")
        self.assertEqual(len(dedup), 1)
        self.assertEqual(dedup[0]["status"], "stored")
        self.assertNotIn("existing_score", dedup[0])
        self.assertEqual(dedup[0]["replace_min_delta"], 8)
        self.assertIn("incoming_score", dedup[0])
        written_events = self.events.by_event("remember_written")
        self.assertEqual(len(written_events), 1)
        event = written_events[0]
        self.assertEqual(event["decision"], "stored")
        self.assertEqual(event["source_path"], self.p("wiki-0001.md"))
        self.assertEqual(event["chunks"], 1)
        self.assertEqual(event["edges"], 4)
        self.assertEqual(event["claims"], 1)
        self.assertTrue(event["relates_to"].startswith("deferred"), event["relates_to"])
        for key in (
            "elapsed_total_s",
            "elapsed_embedding_s",
            "elapsed_db_s",
            "elapsed_vault_s",
            "elapsed_parse_s",
        ):
            self.assertIn(key, event)

    def test_wiki_number_is_max_plus_one_and_never_fills_gaps(self):
        # 디스크 1·2·4 + DB 만 있는 9 — 다음은 10이고 3·5의 빈칸은 채우지 않는다.
        for note_id in ("wiki-0001", "wiki-0002", "wiki-0004"):
            self.seed_note(note_id, f"제목 {note_id}", "본문")
        deps = self.deps(doc_paths=("/vault/wiki/wiki-0009.md",))
        outcome = self.write({"title": "번호 시험", "body": "본문"}, deps)
        self.assertIsInstance(outcome, writer.Written)
        assert isinstance(outcome, writer.Written)
        self.assertEqual(outcome.wiki_id, "wiki-0010")
        self.assertEqual(
            self.written_files(),
            ["wiki-0001.md", "wiki-0002.md", "wiki-0004.md", "wiki-0010.md"],
        )

    def test_wiki_number_ignores_non_numeric_and_negative_stems(self):
        for name in ("wiki-abcd.md", "wiki--1.md", "notes.md"):
            with open(os.path.join(self.vault, "wiki", name), "w", encoding="utf-8") as handle:
                handle.write("---\n---\n")
        outcome = self.write({"title": "번호 시험", "body": "본문"})
        self.assertIsInstance(outcome, writer.Written)
        assert isinstance(outcome, writer.Written)
        self.assertEqual(outcome.wiki_id, "wiki-0001")

    # ── 걸너뜀 ──────────────────────────────────────────────────────────────

    def test_skip_writes_nothing_and_answers_duplicate(self):
        existing = self.seed_note("wiki-0001", "같은 제목", "옛 본문")
        outcome = self.write({"title": "같은 제목", "body": "새로운 본문이다. 충분히 다르다."})
        self.assertIsInstance(outcome, writer.Written)
        assert isinstance(outcome, writer.Written)
        self.assertEqual(outcome.duplicate, existing)
        self.assertEqual(outcome.wiki_id, "wiki-0001")
        self.assertEqual(outcome.source_path, existing)
        self.assertEqual(outcome.message, f"skipped — duplicate of {existing}")
        # 아무것도 안 씀 — 파일도 행도 간선도.
        self.assertEqual(self.written_files(), ["wiki-0001.md"])
        self.assertEqual(self.sql_with("INSERT INTO document"), [])
        self.assertEqual(self.sql_with("INSERT INTO chunk"), [])
        self.assertEqual(self.sql_with("INSERT INTO edge"), [])
        dedup = self.events.by_event("dedup_decision")
        self.assertEqual(dedup[0]["status"], "skipped")
        self.assertEqual(dedup[0]["reason"], "exact_title")
        self.assertEqual(dedup[0]["existing_source_path"], existing)
        self.assertEqual(self.events.by_event("remember_written")[0]["decision"], "skipped")

    # ── 대체(중복 문 판정) ────────────────────────────────────────────────────

    def test_dedup_supersede_links_edge_and_partial_seal_only(self):
        old = self.seed_note(
            "wiki-0001",
            "세션 노트",
            "짧은 옛 노트",
            omb_session_id="sess-e3c1",
            claims=(
                Claim("slot a", "state", "old-a"),
                Claim("slot b", "state", "old-b"),
            ),
        )
        # 같은 세션 + 점수 +8 이상(claim 두 개) → 대체. (옛 노트에 document 행이 있어야
        # 간선이 이어진다 — 행 없는 디스크 노트는 엔진도 unknown 으로 세고 간선을 안 쓴다.)
        args = {
            "title": "세션 노트",
            "body": "같은 세션의 더 풍부한 노트. ## 결정\n근거가 있다.",
            "omb_session_id": "sess-e3c1",
            "claims": [
                {"subject": "slot a", "predicate": "state", "value": "new-a"},
                {"subject": "slot c", "predicate": "state", "value": "new-c"},
            ],
        }
        outcome = self.write(args, self.deps(sha_by_path={old: "sha"}))
        self.assertIsInstance(outcome, writer.Written)
        assert isinstance(outcome, writer.Written)
        self.assertEqual(outcome.wiki_id, "wiki-0002")
        self.assertEqual(outcome.duplicate, old)
        self.assertEqual(outcome.supersedes, 1)
        self.assertEqual(
            outcome.message,
            "remembered → wiki/wiki-0002.md (supersedes wiki/wiki-0001.md) · chunks 1 · "
            "graph(tools 0 concepts 0 claims 2) — recallable now · supersedes linked 1",
        )
        edges = [p for (_s, p) in self.sql_with("INSERT INTO edge")]
        super_edges = [e for e in edges if e["kind"] == "supersedes"]
        self.assertEqual(
            [(e["src"], e["dst"]) for e in super_edges],
            # src 는 새 노트의 진짜 경로, dst 는 중복 문이 이름한 옛 노트 경로(/vault 규약).
            [(f"doc:{self.p('wiki-0002.md')}", f"doc:{old}")],
        )
        # 부분 닫기 SQL — 새 노트가 다시 말한 슬롯 명단만 닫는다(통째 봉인 변이는 여기 빨갛다).
        seals = self.sql_with("IN (SELECT subject, predicate FROM claim WHERE source_path")
        self.assertEqual(len(seals), 1)
        self.assertIn("superseded_at IS NULL", seals[0][0])
        self.assertEqual(seals[0][1]["new"], self.p("wiki-0002.md"))
        self.assertEqual(seals[0][1]["old"], old)
        # 엔진의 통째 봉인 문자열(은퇴 조각)이 아니다.
        self.assertEqual(self.sql_with("AND EXISTS (SELECT 1 FROM edge e"), [])
        dedup = self.events.by_event("dedup_decision")[0]
        self.assertEqual(dedup["status"], "superseded")
        self.assertEqual(dedup["reason"], "same_session")
        self.assertIn("existing_score", dedup)
        self.assertIn("score_delta", dedup)

    # ── 이름한 교정 ─────────────────────────────────────────────────────────

    def test_named_supersedes_skips_gate_and_counts_unknown(self):
        existing = self.seed_note("wiki-0001", "교정 대상", "옛 본문")
        # 제목이 디스크 노트와 같아도 교정은 언제나 저장 — 중복 문을 안 탄다.
        args = {
            "title": "교정 대상",
            "body": "교정 본문",
            "supersedes": ["wiki/wiki-0001.md", "wiki/wiki-9999.md"],
        }
        outcome = self.write(args, self.deps(sha_by_path={existing: "sha"}))
        self.assertIsInstance(outcome, writer.Written)
        assert isinstance(outcome, writer.Written)
        self.assertIsNone(outcome.duplicate)
        self.assertEqual(outcome.supersedes, 1)
        self.assertEqual(outcome.unknown, 1)
        self.assertEqual(
            outcome.message,
            "remembered → wiki/wiki-0002.md · chunks 1 · graph(tools 0 concepts 0 claims 0)"
            " — recallable now · supersedes linked 1, not found 1 (/vault/wiki/wiki-9999.md)",
        )
        dedup = self.events.by_event("dedup_decision")[0]
        self.assertEqual(dedup["status"], "stored")
        self.assertNotIn("reason", dedup)

    # ── PII 차단 ─────────────────────────────────────────────────────────────

    def test_pii_block_writes_nothing(self):
        self.block_rule()
        outcome = self.write(
            {"title": "PII 시험", "body": "여기 SECRET123 이 있다"}, self.deps(scanner=self._scanner)
        )
        self.assertIsInstance(outcome, writer.Refused)
        assert isinstance(outcome, writer.Refused)
        self.assertEqual(outcome.code, -32603)
        self.assertIn("PII gate blocked by rule 'block_test'", outcome.message)
        self.assertIn("matched sensitive text omitted", outcome.message)
        self.assertEqual(self.written_files(), [])
        self.assertEqual(self.sql_with("INSERT INTO"), [])
        self.assertEqual(self.events.rows, [])

    def test_pii_redact_persists_masked_note(self):
        rules = Path(self.vault) / "rules" / "pii.yaml"
        rules.write_text(
            "version: '1.0'\n"
            "policy:\n"
            "  default_action: flag\n"
            "rules:\n"
            "  - name: redact_test\n"
            "    regex: 'ACME\\d+'\n"
            "    action: redact\n"
            "    replacement: '[ACME]'\n"
            "    severity: warning\n"
            "    reason: 시험 가림 규칙\n",
            encoding="utf-8",
        )
        from ohmyboring.remember.pii import load_from_vault

        match load_from_vault(self.vault):
            case Ok(scanner):
                pass
            case Err(reason):
                self.fail(f"pii fixture: {reason}")
        outcome = self.write(
            {"title": "가림 시험", "body": "고객사 ACME42 계약"},
            self.deps(scanner=scanner),
        )
        self.assertIsInstance(outcome, writer.Written)
        text = Path(self.vault, "wiki", "wiki-0001.md").read_text(encoding="utf-8")
        self.assertIn("[ACME]", text)
        self.assertNotIn("ACME42", text)

    # ── owner 거절 ───────────────────────────────────────────────────────────

    def test_owner_author_without_token_refused_and_no_event(self):
        outcome = self.write({"title": "오너 노트", "body": "본문", "author": "owner"})
        self.assertIsInstance(outcome, writer.Refused)
        assert isinstance(outcome, writer.Refused)
        self.assertEqual(outcome.code, -32602)
        self.assertIn("owner door token", outcome.message)
        self.assertEqual(self.written_files(), [])
        self.assertEqual(self.sql_with("INSERT INTO"), [])
        self.assertEqual(self.events.rows, [])

    def test_owner_judge_without_token_refused(self):
        outcome = self.write(
            {"title": "판정 노트", "body": "본문", "judge": "owner"}, self.deps(is_owner=False)
        )
        self.assertIsInstance(outcome, writer.Refused)
        assert isinstance(outcome, writer.Refused)
        self.assertEqual(outcome.code, -32602)
        self.assertIn("owner door token", outcome.message)

    def test_owner_supersede_refused_event_and_zero_writes(self):
        target = "/vault/wiki/wiki-0001.md"
        deps = self.deps(
            _handler(self.sql, doc_paths=(target,), sha_by_path={target: "sha"}, owner_paths=(target,))
        )
        outcome = self.write({"title": "교정", "body": "본문", "supersedes": ["wiki/wiki-0001.md"]}, deps)
        self.assertIsInstance(outcome, writer.Refused)
        assert isinstance(outcome, writer.Refused)
        self.assertEqual(outcome.code, -32602)
        self.assertEqual(outcome.message, f"only the owner may supersede an owner-written note: {target}")
        refused = self.events.by_event("owner_supersede_refused")
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["targets"], [target])
        self.assertEqual(refused[0]["door"], "remember")
        self.assertEqual(self.written_files(), [])
        self.assertEqual(self.sql_with("INSERT INTO"), [])

    def test_owner_may_supersede_owner_note(self):
        target = "/vault/wiki/wiki-0001.md"
        deps = self.deps(
            _handler(self.sql, doc_paths=(target,), sha_by_path={target: "sha"}, owner_paths=(target,)),
            is_owner=True,
        )
        outcome = self.write({"title": "교정", "body": "본문", "supersedes": ["wiki/wiki-0001.md"]}, deps)
        self.assertIsInstance(outcome, writer.Written)
        assert isinstance(outcome, writer.Written)
        self.assertEqual(outcome.wiki_id, "wiki-0002")
        self.assertTrue(self.sql_with("INSERT INTO edge"), "supersedes 간선이 쓰여야 한다")
        self.assertEqual(self.events.by_event("owner_supersede_refused"), [])

    # ── 쓰기 길 고장 ─────────────────────────────────────────────────────────

    def test_embedding_dim_mismatch_refused_with_engine_message(self):
        outcome = self.write(
            {"title": "차원 시험", "body": "본문"},
            self.deps(embed=lambda text: Ok([0.0] * 512)),
        )
        self.assertIsInstance(outcome, writer.Refused)
        assert isinstance(outcome, writer.Refused)
        self.assertEqual(outcome.code, -32603)
        self.assertTrue(
            outcome.message.startswith("ingest: embedding dim mismatch: got 512, expected 1024."),
            outcome.message,
        )

    def test_embedding_unreachable_refused(self):
        outcome = self.write(
            {"title": "임베딩 불응", "body": "본문"},
            self.deps(embed=lambda text: Err("connect refused")),
        )
        self.assertIsInstance(outcome, writer.Refused)
        assert isinstance(outcome, writer.Refused)
        self.assertEqual(outcome.code, -32603)
        self.assertEqual(outcome.message, "ingest: connect refused")

    # ── 모양 ────────────────────────────────────────────────────────────────

    def test_empty_body_refused(self):
        outcome = self.write({"title": "빈 본문", "body": "   \n  "})
        self.assertEqual((outcome.code, outcome.message), (-32602, "missing argument: body"))
        self.assertEqual(self.written_files(), [])

    def test_empty_title_refused(self):
        outcome = self.write({"title": "  ", "body": "본문"})
        self.assertEqual((outcome.code, outcome.message), (-32602, "missing argument: title"))

    def test_non_fact_claim_uses_item_scope_seal(self):
        args = {
            "title": "결정 노트",
            "body": "본문",
            "claims": [{"subject": "팀", "predicate": "결정", "value": "문으로", "kind": "decision"}],
        }
        self.write(args)
        claim = self.sql_with("INSERT INTO claim")[0][1]
        self.assertEqual(claim["subject"], "팀")
        self.assertEqual(claim["kind"], "decision")
        self.assertEqual(claim["confidence"], "unknown")  # 빈 confidence 는 unknown
        # non-fact 는 노트 안 범위 봉인 — fact 범위 SQL 이 아니다.
        self.assertEqual(self.sql_with("FROM (SELECT c.subject, c.predicate, max(c.valid_from)"), [])
        self.assertTrue(self.sql_with("GROUP BY subject, predicate, source_path"))

    def test_anchor_ported_from_body_citation(self):
        args = {
            "title": "앵커 시험",
            "body": "근거는 src/thing.py:42 에 있다",
            "repo": "e3c1scratch",
            "claims": [{"subject": "door", "predicate": "anchors", "value": "see src/thing.py:42"}],
        }
        self.write(args)
        claim = self.sql_with("INSERT INTO claim")[0][1]
        self.assertEqual(claim["anchor"], "e3c1scratch:src/thing.py:L42")

    def test_same_path_supersedes_pair_counts_unknown(self):
        # 자기 자신을 가리키는 쌍은 간선·봉인 없이 unknown — split_supersedes(store.rs:153-164).
        cur = _FakeCursor(_handler(self.sql), self.sql)
        linked, unknown, paths = writer._record_supersedes(
            cur, "/vault/wiki/wiki-0003.md", ["/vault/wiki/wiki-0003.md"], None
        )
        self.assertEqual((linked, unknown, paths), (0, 1, ["/vault/wiki/wiki-0003.md"]))
        self.assertEqual(self.sql, [])

    def test_said_by_owner_writes_said_edges(self):
        args = {
            "title": "오너 발언",
            "body": "본문",
            "author": "owner",
            "claims": [
                {"subject": "door", "predicate": "speaks", "value": "yes", "said_by": "owner", "kind": "fact"}
            ],
        }
        self.write(args, self.deps(is_owner=True))
        edges = [p for (_s, p) in self.sql_with("INSERT INTO edge")]
        said = [(e["src"], e["dst"]) for e in edges if e["kind"] == "said"]
        self.assertEqual(
            said,
            [
                ("person:owner", f"doc:{self.p('wiki-0001.md')}"),
                ("person:owner", "claim:door:speaks"),
            ],
        )
        mirror = self.sql_with("UPDATE claim SET said_by")[0][1]
        self.assertEqual(mirror["said_by"], "owner")


def _read_note(vault_dir: str, note_id: str) -> str | None:
    path = os.path.join(vault_dir, "wiki", f"{note_id}.md")
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


if __name__ == "__main__":
    unittest.main()
