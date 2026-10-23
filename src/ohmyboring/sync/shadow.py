"""읽기 전용 연결로 sync 한 회차를 미리 본다(쓰기 0). 진입점은 agents/door/sync_shadow.py.

JSON 한 줄을 stdout 에 낸다: 파일별 분류 개수·조각 수·gc 대상 개수. 연결은 붙자마자
READ ONLY 세션으로 바꾸고 서버가 답한 transaction_read_only 를 session_read_only 로 싣는다.
머리말 쪼개기는 호출자가 넘긴다 — src 는 agents 를 싣지 않는다(의존성 게이트). 문 컨테이너
밖에서는 BORING_IN_CONTAINER=1 또는 BORING_LLM_BASE_URL 이 필요하다(config 가 주소를 정한다).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable

from ohmyboring import config as boring_config
from ohmyboring.sync.run import shadow_json, shadow_report


def main(
    argv: list[str] | None,
    split_frontmatter: Callable[[str], tuple[str, str] | None],
) -> int:
    parser = argparse.ArgumentParser(prog="agents.door.sync_shadow")
    parser.add_argument(
        "--shadow", action="store_true", required=True, help="읽기 전용 미리보기(유일한 모드)"
    )
    parser.add_argument("--dsn", default=os.environ.get("DOOR_PG_DSN", ""))
    parser.add_argument("--vault", default=os.environ.get("BORING_VAULT_DIR", "/vault"))
    args = parser.parse_args(argv)
    if not args.dsn:
        print("[sync] DOOR_PG_DSN 이 비어 있다 — 문 안에서 돌리거나 --dsn 을 준다.", file=sys.stderr)
        return 2
    import psycopg  # 문 이미지의 드라이버 — 호스트 시험은 이 경계를 안 탄다.

    conn = psycopg.connect(args.dsn, autocommit=True)
    try:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        with conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            read_only = str(cur.fetchone()[0])
            report = shadow_report(
                cur,
                os.path.join(args.vault, "wiki"),
                split_frontmatter,
                lambda path: boring_config.classify(path)[0],
            )
    finally:
        conn.close()
    print(json.dumps(shadow_json(report, read_only), ensure_ascii=False))
    return 0
