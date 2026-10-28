#!/usr/bin/env python3
"""hermes 크론 출입구 — 아침 카드의 조언을 미리 써 둔다(직접 실행).

run-repair-judge.py 와 같은 한 줄 출입구: hermes 는 언제(WHEN)를 정하고, 조언을 쓰는
프로그램(agents/slack/card_advice_write.py)은 그대로 둔 채 card_runner 가 실행한다. 아침 카드(08:00)는 모델을 부르지 않고 여기서 써 둔
조언을 읽기만 하므로, 이 출입구는 카드보다 먼저(repair-judge 뒤) 돌아야 한다. 한 줄을 남기고,
조언 쓰기가 실패하거나 SLACK_BOT_TOKEN 이 없으면 0 아닌 코드로 끝난다 — 그 신호로 hermes 가
실패를 찍고 자기 실패 알림을 울린다.
"""

from __future__ import annotations

import sys

import card_runner


def main() -> int:
    return card_runner.run_card("advice-write", "[run-advice-write]")


if __name__ == "__main__":
    sys.exit(main())
