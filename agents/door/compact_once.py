"""python3 -m agents.door.compact_once [--shadow] — compact 1회 실행 진입점(문 이미지 안).

기본: 배경 스레드와 같은 잠금·같은 함수·같은 compact_run 사건으로 한 회차를 돌고 사건 칸을 JSON 한 줄로
낸다. --shadow: 읽기 전용 세션에서 바뀔 개수만 센다(쓰기 0, 서버가 답한 transaction_read_only 를 싣는다).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg

from ohmyboring import config as boring_config
from ohmyboring.adapters import events as event_log
from ohmyboring.compact import run
from ohmyboring.result import Err, Ok

from .pg_path_guard import pg_path_refusal


def _shadow(dsn: str) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        with conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            read_only = str(cur.fetchone()[0])
            preview = run.preview(cur)
    print(json.dumps(run.preview_json(preview, read_only), ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents.door.compact_once")
    parser.add_argument("--shadow", action="store_true", help="읽기 전용 미리보기(쓰기 0)")
    parser.add_argument("--dsn", default=os.environ.get("DOOR_PG_DSN", ""))
    args = parser.parse_args(argv)
    # 쓰기 길이라 설정을 못 읽어 {} 가 오면 backend 를 모르는 채 pg 를 쓰게 되므로 거부한다.
    if not boring_config.load():
        print("[compact] boring.json 을 읽지 못했다 — backend 를 모르고 pg 를 쓸 수 없다.", file=sys.stderr)
        return 2
    refusal = pg_path_refusal(["compact_once (compact/run.py writes pg)"])
    if refusal is not None:
        print(f"[compact] {refusal}", file=sys.stderr)
        return 2
    if not args.dsn:
        print("[compact] DOOR_PG_DSN 이 비어 있다 — 문 안에서 돌리거나 --dsn 을 준다.", file=sys.stderr)
        return 2
    if args.shadow:
        return _shadow(args.dsn)
    refusal = event_log.foreign_dsn_refusal(args.dsn, os.environ)
    if refusal is not None:
        print(f"[compact] {refusal}", file=sys.stderr)
        return 2
    deps = run.CompactDeps(
        connect=lambda: psycopg.connect(args.dsn, autocommit=True),
        append_event=event_log.try_append_event,
    )
    match run.run_once(deps):
        case Ok(report):
            print(json.dumps({"status": "ok", **report.event_fields()}, ensure_ascii=False))
            return 0
        case Err(reason):
            print(json.dumps({"status": "error", "reason": reason}, ensure_ascii=False))
            return 1


if __name__ == "__main__":
    sys.exit(main())
