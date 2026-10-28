"""python3 -m agents.door.brief_once [--out DIR] [--date YYYY-MM-DD] — 일일 브리프 1회 실행 진입점(문 이미지 안).

정기 스레드와 같은 함수·같은 brief_run 사건. --out 이 지정되면(vault 가 아닌 곳에 쓴다) 사건도 임시 spool 로만
나간다: BORING_EVENT_SINK=spool 과 임시 BORING_EVENT_LOG 가 둘 다 있어야 돈다. --date 는 그 날짜 파일명과
그날 BORING_BRIEF_HOUR 시각을 기준 시각으로 삼는다(손 실행으로 지난 날의 모양을 재현).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

import psycopg

from ohmyboring import config as boring_config
from ohmyboring.adapters import events as event_log
from ohmyboring.briefing import daily, generate
from ohmyboring.result import Err, Ok

_VAULT_ENV = "BORING_VAULT_DIR"
_DEFAULT_VAULT_DIR = "/vault"


def _as_of(date: str | None, hour: int) -> datetime | None:
    if date is None:
        return None
    return datetime.strptime(date, "%Y-%m-%d").astimezone().replace(hour=hour)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents.door.brief_once")
    parser.add_argument(
        "--out", default=None, help="노트를 쓸 디렉터리(기본: <vault>/wiki). 지정하면 사건은 임시 spool 만"
    )
    parser.add_argument("--date", default=None, help="YYYY-MM-DD(기본: 오늘)")
    parser.add_argument("--dsn", default=os.environ.get("DOOR_PG_DSN", ""))
    args = parser.parse_args(argv)
    if not args.dsn:
        print("[brief] DOOR_PG_DSN 이 비어 있다 — 문 안에서 돌리거나 --dsn 을 준다.", file=sys.stderr)
        return 2
    if args.out is not None or args.dsn != os.environ.get("DOOR_PG_DSN", ""):
        refusal = event_log.foreign_dsn_refusal("--out", os.environ)
        if refusal is not None:
            print(f"[brief] {refusal}", file=sys.stderr)
            return 2
    match boring_config.schedule():
        case Err(reason):
            print(f"[brief] {reason}", file=sys.stderr)
            return 2
        case Ok(boring_config.Schedule(brief_hour=None)):
            print("[brief] schedule.brief_hour is off — no reference hour", file=sys.stderr)
            return 2
        case Ok(boring_config.Schedule(brief_hour=hour)):
            pass
    wiki_dir = args.out or os.path.join(os.environ.get(_VAULT_ENV) or _DEFAULT_VAULT_DIR, "wiki")
    deps = daily.BriefDeps(
        connect=lambda: psycopg.connect(args.dsn, autocommit=True),
        generate=generate.llm_generate,
        lang=boring_config.note_lang(),
        wiki_dir=wiki_dir,
        append_event=event_log.try_append_event,
        clock=lambda: datetime.now().astimezone(),
    )
    match daily.run_once(deps, args.date, _as_of(args.date, hour)):
        case Ok(report):
            print(json.dumps({"status": report.status, **report.event_fields()}, ensure_ascii=False))
            return 0
        case Err(reason):
            print(json.dumps({"status": "error", "reason": reason}, ensure_ascii=False))
            return 1


if __name__ == "__main__":
    sys.exit(main())
