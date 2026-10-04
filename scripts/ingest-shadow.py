#!/usr/bin/env python3
"""그림자 대조의 얇은 입구 — 저장소의 단 하나 머리말 쪼개기(vault_note)를 싣고 shadow.main 을 부른다.

src 모듈은 agents 를 import 하지 않으니 vault_note 를 싣는 구성은 이 저장소 밖 입구 한 곳이다
(scripts/data-steward.py 와 같은 뿌리 패턴). 호스트에서는 이 파일이 scripts/ 안에 있고,
문 컨테이너에는 저장소가 /app 에 있어 이 파일을 복사한 자리(src/ 옆이어도)에서도 돈다 —
그 경우 옆에 둔 src/ 가 이미지의 /app/src 보다 앞서 싣힌다.

Run: python3 scripts/ingest-shadow.py [--sample 20] [--out 경로] [--dsn DSN]
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))


def _first_existing(*candidates: str) -> str:
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


# 머리말 쪼개기 정본이 사는 agents/shared — 호스트면 저장소 뿌리, 문 이미지면 /app.
SHARED = _first_existing(
    os.path.join(HERE, "..", "agents", "shared"),
    os.path.join(HERE, "agents", "shared"),
    "/app/agents/shared",
)
# ohmyboring 패키지 — 이 파일 옆의 src/ (문 컨테이너의 새 복사본) 를 이미지 /app/src 보다 먼저.
SRC = _first_existing(
    os.path.join(HERE, "src"),
    os.path.join(HERE, "..", "src"),
    "/app/src",
)

sys.path.insert(0, SHARED)
sys.path.insert(0, SRC)

from vault_note import split_frontmatter  # noqa: E402

from ohmyboring.ingest import shadow  # noqa: E402

if __name__ == "__main__":
    sys.exit(shadow.main(sys.argv[1:], split_frontmatter))
