#!/usr/bin/env python3
"""hermes 크론 출입구 — 문에게 아침 카드를 돌려 달라고 부탁한다.

hermes 는 언제(WHEN)를 정하고, 카드 프로그램(agents/slack/card.py)은 그대로 둔 채 문이
실행한다 — `scripts/schedule-card.sh` 가 launchd 에서 하던 일을 문의 `POST /run/morning-card`
가 대신한다. 한 줄을 남기고, 문이 ok:false 로 답하거나 닿지 않으면 0 아닌 코드로 끝난다 —
그 신호로 hermes 가 실패를 찍고 자기 실패 알림을 울린다.
"""

from __future__ import annotations

import sys

import card_door


def main() -> int:
    return card_door.trigger("/run/morning-card", "[run-morning-card]")


if __name__ == "__main__":
    sys.exit(main())
