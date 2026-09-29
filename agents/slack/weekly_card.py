#!/usr/bin/env python3
"""The weekly card — the weekly briefing, posted like the morning card.

hermes cron used to deliver the weekly: it wrapped the output in a "Cronjob Response" banner
and re-read our Slack mrkdwn as markdown, so `*bold*` arrived as italics. This poster is the
weekly's own process with the bot token — it runs the weekly graph (`ohmyboring.weekly`) in
blocks mode and posts the Block Kit payload to SLACK_CARD_CHANNEL verbatim. No socket, no
hermes wrapper; launchd fires it Monday 09:00 (`scripts/schedule-card.sh install weekly`) and,
since the handover, hermes cron asks the door to fire it (`POST /run/weekly-card`) — one tool,
two schedulers, and the second one of the ISO week sees the first card's own `weekly_card`
event and exits 0 with one line instead of posting twice.

Exit codes: 0 posted — or nothing to say, one line and no post — or already posted this
week, one line and no post; 1 Slack refused the post; 2 SLACK_BOT_TOKEN/SLACK_CARD_CHANNEL
missing; 3 the weekly itself could not be produced.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "shared"))
sys.path.insert(0, str(_HERE.parents[1] / "src"))

from vault_note import split_frontmatter  # noqa: E402

from ohmyboring.weekly.run import deliver_week  # noqa: E402


def main() -> int:
    if not os.environ.get("SLACK_BOT_TOKEN"):
        print("[weekly] SLACK_BOT_TOKEN must be set (see .env.example)", file=sys.stderr)
        return 2
    if not os.environ.get("SLACK_CARD_CHANNEL"):
        print(
            "[weekly] SLACK_CARD_CHANNEL must be set — the channel id the weekly card posts to "
            "(see .env.example)",
            file=sys.stderr,
        )
        return 2

    report = deliver_week(os.environ, split_frontmatter)
    for line in report.out:
        print(line, flush=True)
    for line in report.err:
        print(line, file=sys.stderr)
    return report.code


if __name__ == "__main__":
    sys.exit(main())
