#!/usr/bin/env python3
"""Calendar meeting prep — brief before upcoming meetings.

Uses `icalBuddy` (macOS) to list meetings in the next N hours, then calls
ohmyboring/ask with the meeting title + attendees to produce a context brief.

Opt-in: CALENDAR_PREP=on. Configure CALENDAR_LOOKAHEAD_HOURS (default 4).
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "agents", "shared"))
from drudge_client import DrudgeClient  # noqa: E402


def _run_icalbuddy(start: datetime, end: datetime) -> str:
    if not shutil.which("icalBuddy"):
        raise RuntimeError("icalBuddy not found — install with `brew install ical-buddy`")
    fmt = "[%title] | [%start] | [%attendees] | [%notes]"
    cmd = [
        "icalBuddy",
        "-n",
        "-li", "1",
        "-ps", "\n---EVENT---\n",
        "-b", "",
        "-ab", "",
        "-tf", "%H:%M",
        "-df", "%Y-%m-%d",
        "eventsFrom:", start.strftime("%Y-%m-%dT%H:%M:%S"),
        "to:", end.strftime("%Y-%m-%dT%H:%M:%S"),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=60)
        return out.decode("utf-8", errors="ignore")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"icalBuddy failed: {e.output.decode('utf-8', errors='ignore')}")


def _parse_events(text: str) -> list[dict[str, str]]:
    events = []
    for raw in text.split("---EVENT---"):
        raw = raw.strip()
        if not raw:
            continue
        lines = [l.strip(" •\n") for l in raw.splitlines() if l.strip()]
        title = lines[0] if lines else ""
        when = ""
        attendees = ""
        notes = ""
        for line in lines[1:]:
            if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", line):
                when = line
            elif "attendees" in line.lower() or "@" in line:
                attendees = re.sub(r"(?i)attendees?:\s*", "", line)
            else:
                notes = line
        if title and not title.lower().startswith("warning"):
            events.append({"title": title, "when": when, "attendees": attendees, "notes": notes})
    return events


def main() -> int:
    if os.environ.get("CALENDAR_PREP", "").lower() not in ("on", "1", "true", "yes"):
        print("[calendar-prep] CALENDAR_PREP is off — skipping.")
        return 0

    lookahead = int(os.environ.get("CALENDAR_LOOKAHEAD_HOURS", "4"))
    client = DrudgeClient(timeout=30, retries=2)
    try:
        client.health()
    except Exception as e:
        print(f"[calendar-prep] drudge unreachable: {e}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=lookahead)
    try:
        raw = _run_icalbuddy(now, end)
    except RuntimeError as e:
        print(f"[calendar-prep] {e}", file=sys.stderr)
        return 1

    events = _parse_events(raw)
    if not events:
        print("[calendar-prep] no upcoming meetings.")
        return 0

    for ev in events:
        query = f"meeting prep: {ev['title']}"
        if ev["attendees"]:
            query += f" with {ev['attendees']}"
        query += "\n\nSummarize relevant recent context from my notes."
        try:
            resp = client._retry(
                "POST",
                "/ask",
                {"question": query, "max_results": 5, "max_tokens": 1500},
                timeout=60,
            )
            answer = resp.get("answer", "")
            print(f"\n📅 {ev['when'] or 'upcoming'} — {ev['title']}")
            print(answer)
        except Exception as e:
            print(f"[calendar-prep] ask failed for '{ev['title']}': {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
