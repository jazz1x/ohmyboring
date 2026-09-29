#!/usr/bin/env python3
"""주간 브리핑의 바깥 계약을 못 박는다 — 옛 트리(e3d6a01)에서 만든 리터럴이 새 트리에서도 같다.

시계·엔진·슬랙·사건 장부는 pin_fakes/sitecustomize.py 가 자식 파이썬 안에서 바꾼다. 진짜 슬랙에는
아무것도 올라가지 않는다.

Run: python3 src/ohmyboring/weekly/test_pins.py   (PIN_PRINT=1 이면 관측값 표를 찍는다)
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BRIEFING = REPO / "agents" / "hermes" / "weekly-briefing.py"
CARD = REPO / "agents" / "slack" / "weekly_card.py"
FAKES = Path(__file__).resolve().parent / "pin_fakes"
NOW = "2026-09-28T09:00:00+09:00"
SCRUBBED = ("BORING_", "SLACK_", "DRUDGE_", "OMB_", "PIN_", "PYTHONPATH", "NO_COLOR", "PYTHON_COLORS")
PRINT = bool(os.environ.get("PIN_PRINT"))

ANSWER = (
    "# kb-rag-bot\n- Blocked: 컨플루언스 상태 확인 불가\n- Next: 색인 재시도\n- Done: 임베딩 배치 끝\n\n"
    "# foodspring-front\n- Next: 샘플링 확인\n- Decision: 캐시는 하루\n"
)
DAILY = (
    "---\nid: daily\n---\n# kb-rag-bot\n- Blocked: 컨플루언스 상태 확인 불가 {n}\n- Done: 배치 {n}\n\n"
    "# foodspring-front\n- Next: 샘플링 확인 {n}\n"
)
NO_PROJECT_DAILY = "---\nid: daily\n---\n- 없음\n"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def week(count: int, body: str = DAILY) -> list[tuple[str, str]]:
    return [(f"2026-09-{28 - i}", body.replace("{n}", str(i))) for i in range(count)]


def reply(answer: str, sources: list | None = None) -> dict:
    return {"mode": "ok", "reply": {"answer": answer, "sources": sources or []}}


TREND_DAYS = {
    "full": week(5),
    "two": week(2),
    "one": week(1),
    "zero": [],
    "two-without-projects": week(2, NO_PROJECT_DAILY),
}
ENGINES = {
    "ok": reply(ANSWER),
    "sources": reply(ANSWER, [{"title": "wiki-1", "path": "vault/wiki/wiki-1.md"}]),
    "empty": reply("  "),
    "urlerror": {"mode": "urlerror"},
    "notjson": {"mode": "notjson"},
}

FALLBACK_BLOCKS = "486723209e79f42e150ce7c8516bf56bb227f3f29ac835695746d39a8bae28eb"
FALLBACK_TEXT = "f6f23cfb1975055f277cf0cb14ec0a7490381a40d479ff71238dd9e3894636f9"
TREND_SHA = {
    ("full", "blocks"): "e47d0eac911e1dc2cc04f8ae6b5a925ba7a8cf70345810df74207df73d01e436",
    ("full", "text"): "e392d90fb79905267fdff81d13e5324f3210a384c340d6ed47c59b5222cd98be",
    ("two", "blocks"): "321a2c08694255cbb9fa632bf81381f3e44006a449f68e1d512371e7934f4618",
    ("two", "text"): "e6b7e3f60d393d60519ed6fc295b4d7bb6082372d3919f19c28c2891cdd7a3a6",
    **{(name, "blocks"): FALLBACK_BLOCKS for name in ("one", "zero", "two-without-projects")},
    **{(name, "text"): FALLBACK_TEXT for name in ("one", "zero", "two-without-projects")},
}
ENGINE_SHA = {
    ("ok", "blocks"): FALLBACK_BLOCKS,
    ("ok", "text"): FALLBACK_TEXT,
    ("sources", "blocks"): FALLBACK_BLOCKS,
    ("sources", "text"): FALLBACK_TEXT,
    ("empty", "blocks"): "f2157cd61c7e7a8eaf1fc069273d940660b307fff0a0f9c44a4f3b87c41db409",
    ("empty", "text"): "f2157cd61c7e7a8eaf1fc069273d940660b307fff0a0f9c44a4f3b87c41db409",
    ("urlerror", "blocks"): "533789a5703e389218c6572105312541867efa4c116aea0381dc163cf6f59629",
    ("urlerror", "text"): "533789a5703e389218c6572105312541867efa4c116aea0381dc163cf6f59629",
    ("notjson", "blocks"): "2010d25eac1fedb7d7fc7eefe7749dc62379f51abde740898554020704fc104a",
    ("notjson", "text"): "2010d25eac1fedb7d7fc7eefe7749dc62379f51abde740898554020704fc104a",
}

ENV_OK = {"SLACK_BOT_TOKEN": "tok", "SLACK_CARD_CHANNEL": "D0123456789"}
POSTED_TREND = "1ad52b3e3701fca663d50bc2dc7b0449dacc08c9b4c91074daad5a0ab9f524f9"
POSTED_ENGINE = "6e1af5173e96653c77e5abb06debf2ad7aec5935214cec54205c0b9da677f2aa"
RECORDED = '["slack-card", "weekly_card", "ok", {"ts": "1234.5678", "week": "2026-W40"}]'
NO_BRIEFING = "[weekly] 올릴 브리핑 없음 — 이번 주는 새로 짚을 진행/막힘 항목이 회수되지 않았어요\n"
FAILED = "[weekly] 주간 브리핑 생성 실패: *📅 주간 브리핑* `2026-W40 · 2026-09-28 Mon` "


def run_script(script: Path, days, engine: dict, env: dict[str, str], extra: dict | None = None):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        wiki = root / "vault" / "wiki"
        wiki.mkdir(parents=True)
        for date, text in days:
            (wiki / f"daily-brief-{date}.md").write_text(text, encoding="utf-8")
        out_dir = root / "out"
        out_dir.mkdir()
        fakes = {"now": NOW, "out_dir": str(out_dir), "engine": engine, **(extra or {})}
        child_env = {
            **{k: v for k, v in os.environ.items() if not k.startswith(SCRUBBED)},
            "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join([str(FAKES), str(REPO / "src")]),
            "PIN_FAKES": json.dumps(fakes),
            "BORING_VAULT_DIR": str(root / "vault"),
            "BORING_URL": "http://engine.invalid:7700",
            **env,
        }
        done = subprocess.run(
            [sys.executable, str(script)], env=child_env, capture_output=True, text=True, timeout=120
        )
        seen = {name: (out_dir / name).read_text(encoding="utf-8") for name in os.listdir(out_dir)}
    return done, seen


def briefing(days, engine, fmt):
    return run_script(BRIEFING, days, engine, {"BORING_BRIEFING_FORMAT": fmt})


def card(days, engine, env=None, extra=None):
    base = {"post": {"mode": "ok"}, "events": {"recent": []}}
    return run_script(CARD, days, engine, {**ENV_OK, **(env or {})}, {**base, **(extra or {})})


def show(key, value):
    if PRINT:
        print(key, value)


class BriefingPinTest(unittest.TestCase):
    def test_trend_path_stdout_hashes(self):
        for name, days in TREND_DAYS.items():
            for fmt in ("blocks", "text"):
                done, seen = briefing(days, ENGINES["ok"], fmt)
                show(("trend", name, fmt), sha(done.stdout))
                self.assertEqual((done.returncode, done.stderr), (0, ""), (name, fmt))
                self.assertEqual(sha(done.stdout), TREND_SHA[(name, fmt)], (name, fmt))
                self.assertEqual("engine-call.json" in seen, name not in ("full", "two"), (name, fmt))

    def test_engine_path_stdout_hashes_and_the_call(self):
        for name, engine in ENGINES.items():
            for fmt in ("blocks", "text"):
                done, seen = briefing([], engine, fmt)
                show(("engine", name, fmt), sha(done.stdout))
                self.assertEqual(done.returncode, 0, (name, fmt, done.stderr))
                self.assertEqual(sha(done.stdout), ENGINE_SHA[(name, fmt)], (name, fmt))
                call = json.loads(seen["engine-call.json"])
                self.assertEqual(call, {"url": "http://engine.invalid:7700/weekly", "timeout": 180})


class CardPinTest(unittest.TestCase):
    def check(self, run, rc, stdout, stderr):
        done, seen = run
        self.assertEqual((done.returncode, done.stdout, done.stderr), (rc, stdout, stderr))
        return seen

    def test_missing_token_and_channel(self):
        self.check(
            card(TREND_DAYS["full"], ENGINES["ok"], {"SLACK_BOT_TOKEN": ""}),
            2,
            "",
            "[weekly] SLACK_BOT_TOKEN must be set (see .env.example)\n",
        )
        self.check(
            card(TREND_DAYS["full"], ENGINES["ok"], {"SLACK_CARD_CHANNEL": ""}),
            2,
            "",
            "[weekly] SLACK_CARD_CHANNEL must be set — the channel id the weekly card posts to "
            "(see .env.example)\n",
        )

    def test_already_posted_this_week_stops_and_an_older_week_does_not(self):
        this_week = [
            {"event": "weekly_card", "week": "2026-W40", "ts": "1.5"},
            {"event": "weekly_card", "week": "2026-W40", "ts": "1.6"},
        ]
        seen = self.check(
            card(TREND_DAYS["full"], ENGINES["ok"], None, {"events": {"recent": this_week}}),
            0,
            "[weekly] already posted this week (ts=1.6)\n",
            "",
        )
        self.assertNotIn("posted.json", seen)
        older = [{"event": "weekly_card", "week": "2026-W39", "ts": "1.5"}]
        seen = self.check(
            card(TREND_DAYS["full"], ENGINES["ok"], None, {"events": {"recent": older}}),
            0,
            "[weekly] posted ts=1234.5678\n",
            "",
        )
        self.assertEqual(sha(seen["posted.json"]), POSTED_TREND)

    def test_success_posts_the_payload_and_records_the_week(self):
        seen = self.check(card(TREND_DAYS["full"], ENGINES["ok"]), 0, "[weekly] posted ts=1234.5678\n", "")
        show("posted-trend", sha(seen["posted.json"]))
        self.assertEqual(sha(seen["posted.json"]), POSTED_TREND)
        self.assertEqual(seen["recorded.json"], RECORDED)
        self.assertNotIn("engine-call.json", seen)
        seen = self.check(card([], ENGINES["ok"]), 0, "[weekly] posted ts=1234.5678\n", "")
        show("posted-engine", sha(seen["posted.json"]))
        self.assertEqual(sha(seen["posted.json"]), POSTED_ENGINE)
        self.assertIn("engine-call.json", seen)

    def test_nothing_to_say_posts_nothing(self):
        seen = self.check(card([], ENGINES["empty"]), 0, NO_BRIEFING, "")
        self.assertNotIn("posted.json", seen)
        self.assertNotIn("recorded.json", seen)

    def test_engine_notice_is_a_generation_failure(self):
        for name, tail in (
            ("urlerror", "⚠️ ohmyboring(RAG) 응답 없음 — 엔진 가동 확인 필요. (<urlopen error boom>)\n"),
            ("notjson", "⚠️ 응답 파싱 실패 — ohmyboring 점검 필요.\n"),
        ):
            seen = self.check(card([], ENGINES[name]), 3, "", FAILED + tail)
            self.assertNotIn("posted.json", seen)

    def test_unexpected_engine_reply_is_a_generation_failure(self):
        done, seen = card([], {"mode": "list"})
        self.assertEqual((done.returncode, done.stdout), (3, ""))
        self.assertTrue(done.stderr.startswith("[weekly] 주간 브리핑 생성 실패: "), done.stderr)
        self.assertIn("'list' object has no attribute 'get'", done.stderr)
        self.assertNotIn("posted.json", seen)

    def test_hung_engine_is_a_timeout_notice(self):
        seen = self.check(
            card([], {"mode": "hang"}, None, {"clamp": 1}),
            3,
            "",
            "[weekly] 주간 브리핑 생성 시간 초과 (300초)\n",
        )
        self.assertNotIn("posted.json", seen)

    def test_slack_refusal_exits_1_and_records_nothing(self):
        seen = self.check(
            card(TREND_DAYS["full"], ENGINES["ok"], None, {"post": {"mode": "refuse"}}),
            1,
            "",
            "[weekly] 슬랙 전송 실패: channel_not_found detail\n",
        )
        self.assertEqual(sha(seen["posted.json"]), POSTED_TREND)
        self.assertNotIn("recorded.json", seen)

    def test_a_failed_record_does_not_fail_the_post(self):
        self.check(
            card(TREND_DAYS["full"], ENGINES["ok"], None, {"events": {"recent": [], "fail": True}}),
            0,
            "[weekly] posted ts=1234.5678\n",
            "[weekly] weekly_card event not recorded: disk full\n",
        )


if __name__ == "__main__":
    unittest.main()
