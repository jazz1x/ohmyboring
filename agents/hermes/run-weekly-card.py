#!/usr/bin/env python3
"""hermes 크론 출입구 — 주간 브리핑 카드를 직접 돌린다.

hermes 는 언제(WHEN)를 정하고, 카드 프로그램(agents/slack/weekly_card.py)은 그대로 둔 채
card_runner 가 실행한다 — 문의 `POST /run/weekly-card` 가 하던 일(잠금·시간 제한·토큰 주입)을
그대로 옮겼다. 한 줄을 남기고, 카드가 실패하거나 SLACK_BOT_TOKEN 이 없으면 0 아닌 코드로
끝난다 — 그 신호로 hermes 가 실패를 찍고 자기 실패 알림을 울린다.
"""

from __future__ import annotations

import sys

import card_runner


def main() -> int:
    return card_runner.run_card("weekly-card", "[run-weekly-card]")


if __name__ == "__main__":
    sys.exit(main())
