#!/usr/bin/env python3
"""hermes 출입구 — 도구 범위 판단(C-TOOLLEAK1)을 card_runner 로 돌린다.

엔진(scripts/tool_scope.py)이 켜고 끄는 단위별 표를 세고 행마다 규칙 A 로 범위를 정해 JSON 으로 남긴다.
모델은 부르지 않는다(판정 21027). 설정은 바꾸지 않는다. 손으로 돌리고, 크론은 없다.
"""

from __future__ import annotations

import sys

import card_runner


def main() -> int:
    return card_runner.run_card("tool-scope", "[run-tool-scope]")


if __name__ == "__main__":
    sys.exit(main())
