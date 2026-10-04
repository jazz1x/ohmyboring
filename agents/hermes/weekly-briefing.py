#!/usr/bin/env python3
"""주간 브리핑 — ohmyboring RAG 회수·합성을 stdout 으로.

hermes-agent cron --no-agent --script 로 호출 → stdout 이 그대로 Slack DM 등으로 배달.
지능은 ohmyboring 엔진이 SSOT. 이 스크립트는 그래프(ohmyboring.weekly)를 돌려 찍기만 한다.
"""

import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "src"))
from vault_note import split_frontmatter

from ohmyboring.weekly.run import render_week


def main() -> None:
    print(render_week(os.environ, split_frontmatter))


if __name__ == "__main__":
    main()
    sys.exit(0)
