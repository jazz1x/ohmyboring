#!/usr/bin/env python3
"""판정 쓰기(E4-5) 시험 — 요청 경계의 엔진 문구, SQL 모양, 그리고 진짜 pgvector 에 물어 본 봉인·트랜잭션.

Run: BORING_TEST_DATABASE_URL=postgresql://… python3 src/ohmyboring/verdict/test_verdict.py
DB 시험은 변수가 없으면 건너뛴다(search/test_pg.py 와 같은 관례). 일회용 DB 의 별도 스키마
(omb_verdict_test)에 물을 만들고 끝에 떨군다 — public 의 표는 손 안 탄다. 거절 사건은 수집기로
받는다 — 진짜 사건 저장소에 안 쓴다.

봉인 시험은 drudge/tests 의 같은 이름 시험을 옮긴 것이다(store_integration.rs ·
consumption_integration.rs) — 같은 물, 같은 단언.
Mutation targets: 부분 봉인 SQL·RETIRED_NOTE 의 오너 조건·judge ON CONFLICT·트랜잭션 — 각각 아래
SQL 모양 시험과 DB 시험에서 사망 확인(구현 커밋 뒤 사본에서).
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ohmyboring.remember.writer import _RETIRED_NOTE, _UPSERT_EDGE_SQL  # noqa: E402
from ohmyboring.result import Err, Ok  # noqa: E402
from ohmyboring.verdict import parse as verdict_parse  # noqa: E402
from ohmyboring.verdict import pg as verdict_pg  # noqa: E402
from ohmyboring.verdict import run as verdict_run  # noqa: E402

DSN = os.environ.get("BORING_TEST_DATABASE_URL")
SCHEMA = "omb_verdict_test"
T0 = "2026-10-07T01:02:03+09:00"


def consumption(**fields):
    return verdict_parse.parse_consumption({"session_id": "s-1", "observed_at": T0, **fields})


def rejected_message(parsed) -> str:
    match parsed:
        case Err(verdict_parse.Rejected(message, _)):
            return message
        case other:
            raise AssertionError(f"expected a rejection, got {other!r}")


class EngineWordingTests(unittest.TestCase):
    """serve.rs validate_consumption_req · validate_handover_req — 검사 순서와 문구가 엔진 그대로."""

    def test_empty_session_id(self):
        self.assertEqual(rejected_message(consumption(session_id="  ")), "session_id must not be empty")
        parsed = verdict_parse.parse_handover({"session_id": "", "observed_at": T0})
        self.assertEqual(rejected_message(parsed), "session_id must not be empty")

    def test_observed_at_must_be_rfc3339_and_the_message_quotes_it(self):
        self.assertEqual(
            rejected_message(consumption(observed_at="yesterday")),
            'observed_at must be RFC 3339, got "yesterday"',
        )
        parsed = verdict_parse.parse_handover({"session_id": "s", "observed_at": "2026-10-07"})
        self.assertEqual(rejected_message(parsed), 'observed_at must be RFC 3339, got "2026-10-07"')

    def test_rfc3339_accepts_what_chrono_accepts_and_rejects_the_rest(self):
        for good in (
            T0,
            "2026-10-07T01:02:03Z",
            "2026-10-07T01:02:03.123456789+00:00",
            "2024-02-29T00:00:00Z",
        ):
            self.assertTrue(verdict_parse.is_rfc3339(good), good)
        for bad in (
            "2026-10-07T01:02:03",
            "2026-13-01T00:00:00Z",
            "2025-02-29T00:00:00Z",
            "2026-10-07T24:00:00Z",
        ):
            self.assertFalse(verdict_parse.is_rfc3339(bad), bad)

    def test_verdict_vocabulary(self):
        self.assertEqual(
            rejected_message(consumption(verdict="maybe")),
            'verdict must be "used" or "contested", got "maybe"',
        )
        self.assertEqual(
            rejected_message(consumption(verdict="")),
            'verdict must be "used" or "contested", got ""',
        )

    def test_verdict_and_paths_together(self):
        self.assertEqual(
            rejected_message(consumption(verdict="used", used=["/a.md"])),
            "verdict applies to what was handed; do not also list paths",
        )

    def test_padded_verdict_is_the_trimmed_verdict(self):
        match consumption(verdict=" used "):
            case Ok(verdict_parse.Consumption(basis=verdict_parse.Handed(verdict_parse.Verdict.USED))):
                pass
            case other:
                self.fail(f"expected Handed(USED), got {other!r}")

    def test_judge_vocabulary(self):
        self.assertEqual(
            rejected_message(consumption(judge="boss")),
            'judge: author must be owner | inferred | unknown | agent:<name>, got "boss"',
        )

    def test_unknown_judge_is_null_and_agent_judge_is_kept(self):
        self.assertEqual(consumption(judge="unknown").value.judge, None)
        self.assertEqual(consumption(judge="agent: r2").value.judge, "agent:r2")

    def test_at_most_200_paths(self):
        many = [f"/p{i}.md" for i in range(201)]
        self.assertEqual(rejected_message(consumption(used=many)), "used: at most 200 paths, got 201")
        self.assertEqual(
            rejected_message(consumption(contested=many)), "contested: at most 200 paths, got 201"
        )
        self.assertEqual(
            rejected_message(consumption(supersedes=[["/n.md", p] for p in many])),
            "supersedes: at most 200 paths, got 201",
        )
        parsed = verdict_parse.parse_handover({"session_id": "s", "observed_at": T0, "paths": many})
        self.assertEqual(rejected_message(parsed), "paths: at most 200 paths, got 201")
        self.assertIsInstance(consumption(used=many[:200]), Ok)

    def test_wrong_field_kinds_are_422_and_come_before_the_400s(self):
        for body in ({"used": "/a.md"}, {"supersedes": [["/only-one.md"]]}, {"judge": 5}, {"session_id": 7}):
            match consumption(**body) if "session_id" not in body else verdict_parse.parse_consumption(body):
                case Err(verdict_parse.Rejected("unprocessable entity", 422)):
                    pass
                case other:
                    self.fail(f"{body}: expected 422, got {other!r}")
        match consumption(session_id=" ", used=5):
            case Err(verdict_parse.Rejected(_, 422)):
                pass
            case other:
                self.fail(f"serde 모양이 먼저다 — got {other!r}")

    def test_mcp_verdict_arguments(self):
        for args, message in (
            ({}, "missing argument: session_id"),
            ({"session_id": "  ", "verdict": "used"}, "missing argument: session_id"),
            ({"session_id": 5, "verdict": "used"}, "missing argument: session_id"),
            ({"session_id": "s"}, "missing argument: verdict"),
            ({"session_id": "s", "verdict": ""}, "missing argument: verdict"),
            ({"session_id": "s", "verdict": "meh"}, 'verdict must be "used" or "contested", got "meh"'),
        ):
            self.assertEqual(rejected_message(verdict_parse.parse_verdict_call(args)), message, args)
        match verdict_parse.parse_verdict_call({"session_id": " s ", "verdict": " contested "}):
            case Ok(verdict_parse.VerdictCall("s", verdict_parse.Verdict.CONTESTED)):
                pass
            case other:
                self.fail(f"expected trimmed call, got {other!r}")


class NeverConnect:
    def __call__(self):
        raise AssertionError("쓰기 전에 거절돼야 한다 — 연결을 열면 안 된다")


class OwnerStandingTests(unittest.TestCase):
    def test_judge_owner_without_the_token_is_refused_before_any_write(self):
        deps = verdict_run.Deps(NeverConnect(), lambda *a, **k: None, is_owner=False, now=lambda: T0)
        match verdict_run.consume(consumption(judge="owner").value, deps):
            case Err(verdict_parse.Rejected(message, 400)):
                self.assertEqual(
                    message, "owner (as author or judge) needs the owner door token in x-boring-owner-token"
                )
            case other:
                self.fail(f"expected a 400 rejection, got {other!r}")


class StoreOffTests(unittest.TestCase):
    def test_no_store_is_the_engines_sentence_and_comes_after_the_owner_check(self):
        deps = verdict_run.Deps(None, lambda *a, **k: None, is_owner=False, now=lambda: T0)
        match verdict_run.consume(consumption(used=["/a.md"]).value, deps):
            case Err(verdict_run.StoreOff(message)):
                self.assertEqual(
                    message,
                    "BORING_VECTOR=off — this feature requires the vector backend (pgvector). "
                    "Set BORING_VECTOR=on and start Postgres.",
                )
            case other:
                self.fail(f"expected StoreOff, got {other!r}")
        self.assertIsInstance(
            verdict_run.consume(consumption(judge="owner").value, deps).error, verdict_parse.Rejected
        )


class SqlShapeTests(unittest.TestCase):
    """살아 있는 postgres 없이도 옮긴 규칙을 본다 — 변이 (a)(b)(c) 는 여기서도 사망."""

    def test_seal_is_whole_note_not_the_slots_the_newer_note_restates(self):
        sql = verdict_pg._SEAL_SQL
        self.assertIn("WHERE c.source_path = %(older)s AND c.superseded_at IS NULL", sql)
        self.assertNotIn("(c.subject, c.predicate) IN", sql, "부분 봉인(remember)이 아니라 통째 봉인")
        self.assertIn(_RETIRED_NOTE, sql)

    def test_promote_competes_only_among_live_rows(self):
        sql = verdict_pg._PROMOTE_SQL
        self.assertIn(f"AND NOT {_RETIRED_NOTE}", sql)
        self.assertIn("c.valid_from = m.mx", sql)

    def test_retired_note_keeps_the_owner_rule(self):
        self.assertIn("n.author = 'owner'", _RETIRED_NOTE)
        self.assertIn("o.author = 'owner'", _RETIRED_NOTE)

    def test_edges_keep_the_first_judge(self):
        self.assertIn("ON CONFLICT DO NOTHING", _UPSERT_EDGE_SQL)
        self.assertNotIn("DO UPDATE", _UPSERT_EDGE_SQL)


def _skip_reason() -> str | None:
    if not DSN:
        return "BORING_TEST_DATABASE_URL unset — DB integration test skipped (disposable DB only)"
    return None


def at(minutes: int) -> datetime:
    return datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=minutes)


@unittest.skipIf(DSN is None, _skip_reason())
class VerdictDbTests(unittest.TestCase):
    """별도 스키마에 고정 물 — 순수 파이썬 게이트에는 안 잡히는 SQL 결과를 본다."""

    OPTIONS = f"-c search_path={SCHEMA},public"

    @classmethod
    def setUpClass(cls):
        import psycopg

        cls.psycopg = psycopg
        cls.db = psycopg.connect(DSN, autocommit=True, options=cls.OPTIONS)
        with cls.db.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;")
            cur.execute(f"CREATE SCHEMA {SCHEMA};")
            cur.execute("SET search_path TO " + SCHEMA + ", public;")
            cur.execute(
                "CREATE TABLE document (source_path text PRIMARY KEY, sha text NOT NULL DEFAULT '',"
                " author text NOT NULL DEFAULT 'unknown');"
            )
            cur.execute(
                "CREATE TABLE node (id text PRIMARY KEY, kind text NOT NULL, label text NOT NULL DEFAULT '',"
                " outcome text);"
            )
            cur.execute(
                "CREATE TABLE edge (src text NOT NULL, dst text NOT NULL, kind text NOT NULL, judge text,"
                " PRIMARY KEY (src, dst, kind));"
            )
            cur.execute(
                "CREATE TABLE claim (subject text NOT NULL, predicate text NOT NULL, value text NOT NULL,"
                " source_path text NOT NULL, valid_from timestamptz NOT NULL, superseded_at timestamptz,"
                " kind text NOT NULL DEFAULT 'fact', PRIMARY KEY (subject, predicate, valid_from));"
            )
            cur.execute(
                "CREATE FUNCTION fail_on_boom() RETURNS trigger LANGUAGE plpgsql AS $$"
                " BEGIN IF NEW.dst = 'doc:/boom.md' THEN RAISE EXCEPTION 'boom'; END IF; RETURN NEW; END $$;"
            )
            cur.execute(
                "CREATE TRIGGER edge_boom BEFORE INSERT ON edge FOR EACH ROW EXECUTE FUNCTION fail_on_boom();"
            )

    @classmethod
    def tearDownClass(cls):
        with cls.db.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;")
        cls.db.close()

    def setUp(self):
        with self.db.cursor() as cur:
            cur.execute("TRUNCATE document, node, edge, claim;")
        self.events: list[tuple] = []

    def deps(self, is_owner: bool = False) -> verdict_run.Deps:
        return verdict_run.Deps(
            connect=lambda: self.psycopg.connect(DSN, options=self.OPTIONS),
            append_event=lambda *args, **fields: self.events.append((args, fields)),
            is_owner=is_owner,
            now=lambda: "2026-10-07T00:00:00+00:00",
        )

    def q(self, sql: str, *params):
        with self.db.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def doc(self, path: str, author: str = "unknown") -> str:
        self.q("INSERT INTO document (source_path, author) VALUES (%s, %s) RETURNING 1;", path, author)
        return path

    def claim(self, subject, value, path, when, **more):
        """more: kind(기본 fact)·predicate(기본 status)·sealed_at(기본 현재)."""
        self.q(
            "INSERT INTO claim (subject, predicate, value, source_path, valid_from, superseded_at, kind)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING 1;",
            subject,
            more.get("predicate", "status"),
            value,
            path,
            when,
            more.get("sealed_at"),
            more.get("kind", "fact"),
        )

    def sealed_at(self, subject):
        return self.q("SELECT superseded_at FROM claim WHERE subject = %s;", subject)[0][0]

    def sealed_count(self, path) -> int:
        return self.q(
            "SELECT count(*) FROM claim WHERE source_path = %s AND superseded_at IS NOT NULL;", path
        )[0][0]

    def current_rows(self, subject):
        return self.q(
            "SELECT value, source_path FROM claim WHERE subject = %s AND predicate = 'status'"
            " AND superseded_at IS NULL ORDER BY valid_from;",
            subject,
        )

    def edges(self, kind):
        return self.q("SELECT src, dst, judge FROM edge WHERE kind = %s ORDER BY src, dst;", kind)

    def supersede_in_store(self, pairs):
        """store.record_supersedes 를 곧장 — 문이 거르는 단계(오너 노트 거절)를 건너뛴 저장소 쪽 시험."""
        with self.psycopg.connect(DSN, options=self.OPTIONS) as conn, conn.cursor() as cur:
            return verdict_pg.record_supersedes(cur, pairs, None)

    def owner_holder_probe(self, b_author: str):
        """비오너 C 의 옛 행이 오너 A 의 새 행 밑에 봉인돼 있고, B 는 슬롯을 안 건드린 채 대체를 건다."""
        path_c, path_a, path_b = (
            self.doc("/w/c.md"),
            self.doc("/w/a.md", "owner"),
            self.doc("/w/b.md", b_author),
        )
        self.claim("slot", "c-old", path_c, at(0), sealed_at=at(1))
        self.claim("slot", "a-new", path_a, at(1))
        self.claim("b-item", "y", path_b, at(2), kind="next", predicate="next_action")
        return path_c, path_a, path_b

    def test_record_supersedes_twice_seals_once(self):
        """store_integration.rs record_supersedes_twice_seals_once — 두 번째는 도장을 다시 찍지 않는다."""
        path_a, path_b = self.doc("/w/a.md"), self.doc("/w/b.md")
        self.claim("a-item", "x", path_a, at(0), kind="next", predicate="next_action")
        self.claim("b-item", "y", path_b, at(1), kind="next", predicate="next_action")
        for round_ in (1, 2):
            resp = verdict_run.consume(consumption(supersedes=[[path_b, path_a]]).value, self.deps()).value
            self.assertEqual((resp["supersedes"], resp["refused"]), (1, 0), f"round {round_}: 시도 수")
            self.assertEqual(self.sealed_at("a-item"), at(1), f"round {round_}: 도장이 안 움직인다")
            self.assertEqual(self.sealed_count(path_a), 1, f"round {round_}")
            self.assertIsNone(self.sealed_at("b-item"), f"round {round_}: 새 노트 행은 안 닫힌다")
            self.assertEqual(self.sealed_count(path_b), 0, f"round {round_}")

    def test_owner_note_superseded_by_a_non_owner_keeps_its_current_claims(self):
        """store_integration.rs owner_note_superseded_by_a_non_owner_keeps_its_current_claims."""
        path_a, path_b = self.doc("/w/a.md", "owner"), self.doc("/w/b.md")
        self.claim("a-item", "x", path_a, at(0), kind="next", predicate="next_action")
        self.claim("b-item", "y", path_b, at(1), kind="next", predicate="next_action")
        self.supersede_in_store(((path_b, path_a),))
        self.assertEqual(len(self.edges("supersedes")), 1, "간선은 쓴다 — 거르는 쪽은 문")
        self.assertIsNone(self.sealed_at("a-item"), "비오너 노트가 오너 글을 은퇴시키지 못한다")

    def test_a_non_owner_supersede_of_an_owner_note_reopens_nothing(self):
        path_c, path_a, path_b = self.owner_holder_probe("unknown")
        self.supersede_in_store(((path_b, path_a),))
        self.assertEqual(self.sealed_count(path_a), 0)
        self.assertEqual(self.sealed_count(path_c), 1, "C 는 A 밑에 봉인된 채 — 승격이 아무것도 안 연다")
        self.assertEqual(self.current_rows("slot"), [("a-new", path_a)])

    def test_an_owner_supersede_of_an_owner_note_hands_the_slot_back(self):
        """store_integration.rs an_owner_supersede_of_an_owner_note_hands_the_slot_back."""
        path_c, path_a, path_b = self.owner_holder_probe("owner")
        self.supersede_in_store(((path_b, path_a),))
        self.assertEqual(
            self.q("SELECT superseded_at FROM claim WHERE source_path = %s;", path_a)[0][0],
            at(2),
            "A 는 은퇴 — B 의 최신 claim 시각으로 봉인",
        )
        self.assertEqual(self.sealed_count(path_c), 0, "슬롯이 C 로 넘어왔다 — 봉인이 풀렸다")
        self.assertEqual(self.current_rows("slot"), [("c-old", path_c)], "슬롯의 현재 행은 정확히 C 하나")

    def test_consumption_judge_lands_on_edges_and_the_first_judge_stays(self):
        """consumption_integration.rs consumption_judge_lands_on_edges_and_the_first_judge_stays."""
        path_a, path_b = self.doc("/w/a.md"), self.doc("/w/b.md")
        first = consumption(used=[path_a], contested=[path_b], judge="agent:r2-check").value
        verdict_run.consume(first, self.deps())
        self.assertEqual(self.edges("used"), [("session:s-1", f"doc:{path_a}", "agent:r2-check")])
        self.assertEqual(self.edges("contested"), [("session:s-1", f"doc:{path_b}", "agent:r2-check")])
        again = consumption(used=[path_a], judge="owner", observed_at="2026-10-07T03:00:00Z").value
        resp = verdict_run.consume(again, self.deps(is_owner=True)).value
        self.assertEqual(resp["used"], 1, "이미 있는 간선도 센다(시도 수)")
        self.assertEqual(self.edges("used")[0][2], "agent:r2-check", "처음 이름 댄 judge 가 남는다")
        self.assertEqual(
            self.q("SELECT label FROM node WHERE id = 'session:s-1';")[0][0], "2026-10-07T03:00:00Z"
        )
        verdict_run.consume(consumption(session_id="bare", used=[path_a]).value, self.deps())
        self.assertEqual(self.q("SELECT judge FROM edge WHERE src = 'session:bare';")[0][0], None)

    def test_verdict_only_consumption_marks_exactly_the_handed_docs(self):
        """consumption_integration.rs verdict_only_consumption_marks_exactly_the_handed_docs."""
        path_a, path_b, path_c = self.doc("/w/a.md"), self.doc("/w/b.md"), self.doc("/w/c.md")
        handed = verdict_parse.parse_handover(
            {
                "session_id": "s-h",
                "observed_at": T0,
                "paths": [path_a, path_b, "/vault/wiki/never-ingested.md"],
            }
        ).value
        self.assertEqual(
            verdict_run.hand_over(handed, self.deps()).value,
            {"session": "session:s-h", "handed": 2, "unknown": 1},
        )
        resp = verdict_run.consume(consumption(session_id="s-h", verdict=" used ").value, self.deps()).value
        self.assertEqual(
            resp,
            {
                "session": "session:s-h",
                "used": 2,
                "contested": 0,
                "supersedes": 0,
                "unknown": 0,
                "refused": 0,
            },
            "응답 칸 순서까지 엔진 그대로",
        )
        self.assertEqual([dst for _, dst, _ in self.edges("used")], [f"doc:{path_a}", f"doc:{path_b}"])
        self.assertEqual(self.edges("contested"), [], "한 판정만 — 공백 낀 ' used ' 가 contested 로 안 간다")
        none_handed = verdict_run.consume(
            consumption(session_id="nobody", verdict="used").value, self.deps()
        ).value
        self.assertEqual((none_handed["used"], none_handed["unknown"]), (0, 0), "모르는 세션은 평범한 0 응답")
        self.assertNotIn(
            f"doc:{path_c}", [dst for _, dst, _ in self.edges("used")], "건네지 않은 문서는 그대로"
        )

    def test_non_owner_supersede_of_an_owner_note_is_dropped_counted_and_logged_once(self):
        owned, mine, newer = self.doc("/w/o.md", "owner"), self.doc("/w/m.md"), self.doc("/w/n.md")
        self.claim("o-item", "x", owned, at(0), kind="next", predicate="next_action")
        self.claim("m-item", "x", mine, at(0), kind="next", predicate="next_action")
        req = consumption(supersedes=[[newer, owned], [newer, mine]]).value
        resp = verdict_run.consume(req, self.deps(is_owner=False)).value
        self.assertEqual((resp["supersedes"], resp["refused"]), (1, 1))
        self.assertEqual(
            [dst for _, dst, _ in self.edges("supersedes")], [f"doc:{mine}"], "오너 노트 쪽은 버렸다"
        )
        self.assertIsNone(self.sealed_at("o-item"))
        self.assertEqual(
            self.events,
            [
                (
                    ("drudge.owner", "owner_supersede_refused", "warn"),
                    {"door": "consumption", "targets": [owned]},
                )
            ],
        )
        self.events.clear()
        token_only = verdict_run.consume(
            consumption(session_id="s-2", judge="inferred", supersedes=[[newer, owned]]).value,
            self.deps(is_owner=True),
        ).value
        self.assertEqual(
            (token_only["supersedes"], token_only["refused"]),
            (0, 1),
            "토큰만 있고 judge 가 오너가 아니면 비오너",
        )
        self.events.clear()
        owner_resp = verdict_run.consume(
            consumption(session_id="s-3", judge="owner", supersedes=[[newer, owned]]).value,
            self.deps(is_owner=True),
        ).value
        self.assertEqual(
            (owner_resp["supersedes"], owner_resp["refused"]), (1, 0), "대조군: 오너는 못 막는다"
        )
        self.assertEqual(self.events, [])

    def test_a_failure_midway_writes_nothing(self):
        """한 요청 = 한 트랜잭션 — 두 번째 경로에서 터지면 session 노드도 첫 간선도 안 남는다."""
        path_a, boom = self.doc("/w/a.md"), self.doc("/boom.md")
        failing = consumption(used=[path_a, boom]).value
        match verdict_run.consume(failing, self.deps()):
            case Err(verdict_run.Failed(message)):
                self.assertIn("boom", message)
            case other:
                self.fail(f"expected Failed, got {other!r}")
        self.assertEqual(self.q("SELECT count(*) FROM node;")[0][0], 0)
        self.assertEqual(self.q("SELECT count(*) FROM edge;")[0][0], 0)
        ok = verdict_run.consume(consumption(used=[path_a]).value, self.deps())
        self.assertIsInstance(ok, Ok, "대조군: boom 이 없으면 같은 요청이 쓰인다")
        self.assertEqual(self.q("SELECT count(*) FROM edge;")[0][0], 1)

    def test_handover_counts_attempts_and_a_repeat_adds_no_edges(self):
        path_a = self.doc("/w/a.md")
        req = verdict_parse.parse_handover(
            {"session_id": "s-h", "observed_at": T0, "paths": [path_a, path_a, "/ghost.md"]}
        ).value
        first = verdict_run.hand_over(req, self.deps()).value
        self.assertEqual(first, {"session": "session:s-h", "handed": 2, "unknown": 1})
        verdict_run.hand_over(req, self.deps())
        self.assertEqual(len(self.edges("handed")), 1)

    def test_mcp_verdict_uses_the_handed_docs_and_has_no_refused_field(self):
        path_a = self.doc("/w/a.md")
        verdict_run.hand_over(
            verdict_parse.parse_handover({"session_id": "s-m", "observed_at": T0, "paths": [path_a]}).value,
            self.deps(),
        )
        call = verdict_parse.parse_verdict_call({"session_id": "s-m", "verdict": "contested"}).value
        payload = verdict_run.verdict(call, self.deps()).value
        self.assertEqual(
            list(payload.items()),
            [("session", "session:s-m"), ("used", 0), ("contested", 1), ("supersedes", 0), ("unknown", 0)],
        )
        self.assertEqual(
            self.q("SELECT label FROM node WHERE id = 'session:s-m';")[0][0], "2026-10-07T00:00:00+00:00"
        )
        self.assertEqual(self.edges("contested"), [("session:s-m", f"doc:{path_a}", None)])


if __name__ == "__main__":
    unittest.main()
