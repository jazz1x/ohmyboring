#!/usr/bin/env python3
"""hermes 크론 출입구 — 이름 맞추기 판정을 직접 돌린다.

run-morning-card.py 와 같은 한 줄 출입구: hermes 는 언제(WHEN)를 정하고, 판정
프로그램(agents/slack/card_repair_judge.py)은 그대로 둔 채 card_runner 가 실행한다. 한 줄을 남기고, 판정이 실패하거나
SLACK_BOT_TOKEN 이 없으면 0 아닌 코드로 끝난다 — 그 신호로 hermes 가 실패를 찍고 자기 실패
알림을 울린다.
"""

from __future__ import annotations

import sys

import card_runner


def main() -> int:
    return card_runner.run_card("repair-judge", "[run-repair-judge]")


if __name__ == "__main__":
    sys.exit(main())
