#!/usr/bin/env python3
"""hermes 크론 출입구 — 문에게 이름 맞추기 판정을 돌려 달라고 부탁한다.

run-morning-card.py 와 같은 한 줄 출입구: hermes 는 언제(WHEN)를 정하고, 판정
프로그램(agents/slack/card_repair_judge.py)은 그대로 둔 채 문이 실행한다 — 문의
POST /run/repair-judge 가 launchd 시절 schedule-card.sh 가 하던 일을 대신한다. 한 줄을
남기고, 문이 ok:false 로 답하거나 닿지 않으면 0 아닌 코드로 끝난다 — 그 신호로 hermes 가
실패를 찍고 자기 실패 알림을 울린다.
"""

from __future__ import annotations

import sys

import card_door


def main() -> int:
    return card_door.trigger("/run/repair-judge", "[run-repair-judge]")


if __name__ == "__main__":
    sys.exit(main())
