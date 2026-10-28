#!/usr/bin/env python3
"""hermes 크론 출입구 — 아침 카드를 직접 돌린다.

hermes 는 언제(WHEN)를 정하고, 카드 프로그램(agents/slack/card.py)은 그대로 둔 채
card_runner 가 실행한다(잠금·시간 제한·토큰 주입). 한 줄을 남기고, 카드가 실패하거나 SLACK_BOT_TOKEN 이 없으면 0 아닌 코드로
끝난다 — 그 신호로 hermes 가 실패를 찍고 자기 실패 알림을 울린다.
"""

from __future__ import annotations

import sys

import card_runner


def main() -> int:
    return card_runner.run_card("morning-card", "[run-morning-card]")


if __name__ == "__main__":
    sys.exit(main())
