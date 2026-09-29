#!/usr/bin/env python3
"""Which rule firings answered the owner, and which answered the harness.

`rule_fired` events carry a session and a subject but not the prompt, so the event log cannot
say whether a firing met the owner's words or a subagent's report quoting them. The transcript
can: the rules hook's context is an attachment whose parent is the prompt it answered. Each
prompt is judged by the hooks' own filter (`recall._is_injection`), so this meter and the hook
never disagree about what counts as the harness.

Read-only, local, no network. Prints and exits.
"""

import argparse
import collections
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents" / "shared"))
sys.path.insert(0, str(ROOT / "agents" / "claude-code"))
sys.path.insert(0, str(ROOT / "src"))

import recall  # noqa: E402
import rules_core  # noqa: E402

from ohmyboring import config as boring_config  # noqa: E402


def _prompt_above(row, by_uuid):
    """The user prompt a hook attachment answered — hook attachments chain onto each other first."""
    node = by_uuid.get(row.get("parentUuid"))
    while node is not None and node.get("type") == "attachment":
        node = by_uuid.get(node.get("parentUuid"))
    content = ((node or {}).get("message") or {}).get("content")
    return content if node is not None and node.get("type") == "user" and isinstance(content, str) else None


def _who(prompt):
    if prompt is None:
        return "unknown"
    return "harness" if recall._is_injection({"prompt": prompt}) else "owner"


def firings(rows):
    """(session, day, 'owner'|'harness'|'unknown', rules injected) per rules attachment in one transcript."""
    rows = list(rows)
    by_uuid = {row.get("uuid"): row for row in rows}
    out = []
    for row in rows:
        att = row.get("attachment") or {}
        if row.get("type") != "attachment" or att.get("hookName") != "UserPromptSubmit":
            continue
        for text in att.get("content") or []:
            if not text.startswith(rules_core.HEADER):
                continue
            n = sum(1 for line in text.splitlines() if line.startswith("- "))
            day = (row.get("timestamp") or "")[:10]
            out.append((row.get("sessionId"), day, _who(_prompt_above(row, by_uuid)), n))
    return out


def _rows(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def transcript_files():
    dirs = boring_config.source_dirs(adapter="session-end") or [os.path.expanduser("~/.claude/projects")]
    return sorted(p for d in dirs if Path(d).is_dir() for p in Path(d).glob("*/*.jsonl"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", help="ISO date, inclusive")
    ap.add_argument("--by-session", action="store_true")
    args = ap.parse_args(argv)
    files = transcript_files()
    prompts = collections.Counter()
    rules = collections.Counter()
    sessions = collections.defaultdict(collections.Counter)
    for path in files:
        for session, day, who, n in firings(_rows(path)):
            if args.since and day < args.since:
                continue
            prompts[who] += 1
            rules[who] += n
            sessions[session][who] += n
    print(f"transcripts {len(files)} · sessions with firings {len(sessions)}")
    for who in ("owner", "harness", "unknown"):
        print(f"{who:8} prompts {prompts[who]:5} · rules injected {rules[who]:5}")
    if args.by_session:
        for session, c in sorted(sessions.items(), key=lambda kv: -sum(kv[1].values())):
            print(f"{session}  owner {c['owner']:4}  harness {c['harness']:4}  unknown {c['unknown']:4}")


if __name__ == "__main__":
    main()
