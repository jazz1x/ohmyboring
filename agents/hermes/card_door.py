#!/usr/bin/env python3
"""The shared trigger behind the two card cron scripts — one POST to the door, one line out.

hermes owns WHEN the cards run; the card programs stay the tool and the door runs them
(POST /run/morning-card · /run/weekly-card). The contract with hermes cron is the exit
code: non-zero makes hermes mark the run failed and fire its own failure alert, so a door
that answers ok:false — or does not answer — must never exit 0.
"""

from __future__ import annotations

import http.client
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# `omb_env` lives in agents/shared — the installer lands it flat beside this file in
# ~/.hermes/scripts, where the plain import resolves; running from the repo they are
# siblings through the parent dir, so add that first (the slack_briefing pattern).
_SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
if _SHARED_DIR.is_dir() and str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

import omb_env  # noqa: E402

#: The door waits out the card itself (DOOR_RUN_CARD_TIMEOUT inside the door); the script
#: must outwait the door so a slow card comes back as the door's own timeout answer rather
#: than as our give-up on a card that is still mid-run.
TIMEOUT_S = 960.0


def one_line(text: str) -> str:
    return " ".join(text.split())


def trigger(route: str, label: str) -> int:
    """POST one run route and fold the answer into a single line. 0 only when the door's
    JSON says ok — anything else (HTTP error, unreachable, unparseable body, ok:false) is
    one stderr line and 1."""
    url = f"{omb_env.door_url().rstrip('/')}{route}"
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = one_line(e.read().decode("utf-8", "replace"))[:200]
        print(f"{label} FAILED: door answered HTTP {e.code} {detail}".rstrip(), file=sys.stderr)
        return 1
    except (OSError, ValueError, http.client.HTTPException) as e:
        # URLError, connection refused, a truncated body, a JSON body that is not JSON — one
        # class: the door could not give us a usable answer.
        print(f"{label} FAILED: door unreachable or answer unreadable: {one_line(str(e))}", file=sys.stderr)
        return 1
    if not isinstance(body, dict):
        print(f"{label} FAILED: door answered a non-object JSON body", file=sys.stderr)
        return 1
    if body.get("ok"):
        suffix = f" posted_ts={body['posted_ts']}" if body.get("posted_ts") else ""
        print(f"{label} ok exit={body.get('exit')}{suffix}")
        return 0
    exit_code = body.get("exit")
    where = f"exit={exit_code}" if exit_code is not None else str(body.get("error") or "no exit code")
    tail = one_line(str(body.get("tail") or ""))[:200]
    print(f"{label} FAILED: {where} tail={tail}", file=sys.stderr)
    return 1
