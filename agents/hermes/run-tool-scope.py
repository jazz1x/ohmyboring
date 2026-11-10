#!/usr/bin/env python3
"""hermes 출입구 — 도구 범위 판단(C-TOOLLEAK1)을 card_runner 로 돌린다.

엔진(scripts/tool_scope.py)이 켜고 끄는 단위별 표를 세고, 카드 모델이 행마다 범위와 숫자 이유를 고르고,
인용 대조가 틀리면 0 아닌 코드로 끝난다. 설정은 바꾸지 않는다. 1판은 손으로 한 번 돌리고, 크론은 없다.
"""

from __future__ import annotations

import sys

import card_runner


def main() -> int:
    return card_runner.run_card("tool-scope", "[run-tool-scope]")


if __name__ == "__main__":
    sys.exit(main())
