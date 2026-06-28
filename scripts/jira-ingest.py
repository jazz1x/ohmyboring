#!/usr/bin/env python3
"""Jira ticket status → ohmyboring claims.

Fetches tickets assigned to the configured user and upserts lightweight notes +
claims for each. Origin=jira. Opt-in via JIRA_SYNC=on.
"""
import base64
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "agents", "shared"))
from drudge_client import DrudgeClient  # noqa: E402


def _env(key: str) -> Optional[str]:
    return os.environ.get(key)


def _jira_get(path: str) -> dict[str, Any]:
    base = (_env("JIRA_BASE_URL") or "").rstrip("/")
    user = _env("JIRA_USER") or ""
    token = _env("JIRA_TOKEN") or ""
    if not base or not user or not token:
        raise RuntimeError("JIRA_BASE_URL, JIRA_USER, JIRA_TOKEN required")
    url = f"{base}/rest/api/2{path}"
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    headers = {
        "authorization": f"Basic {auth}",
        "content-type": "application/json",
        "accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _summarize_ticket(t: dict[str, Any]) -> Optional[dict[str, Any]]:
    key = t.get("key", "")
    fields = t.get("fields") or {}
    summary = fields.get("summary", "")
    status = (fields.get("status") or {}).get("name", "")
    priority = (fields.get("priority") or {}).get("name", "")
    assignee = ((fields.get("assignee") or {}) or {}).get("displayName", "")
    project = ((fields.get("project") or {}) or {}).get("key", "")
    updated = t.get("fields", {}).get("updated", "")
    description = (fields.get("description") or "")[:500]

    if not key:
        return None

    body = f"Jira ticket {key}: {summary}\n\nStatus: {status}\nPriority: {priority}\nAssignee: {assignee}\n\n{description}"
    claims = [
        {"subject": key, "predicate": "status", "value": status, "kind": "fact", "confidence": "certain"},
    ]
    if priority:
        claims.append({"subject": key, "predicate": "priority", "value": priority, "kind": "fact", "confidence": "certain"})
    if assignee:
        claims.append({"subject": key, "predicate": "assignee", "value": assignee, "kind": "fact", "confidence": "certain"})

    return {
        "title": f"jira: {key} {summary}",
        "body": body,
        "project": project.lower() or key.split("-")[0].lower(),
        "tags": ["jira", f"repo/{project.lower()}"],
        "claims": claims,
        "omb_session_id": f"jira-{key}",
        "date": updated[:10] if updated else datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


import urllib.request  # noqa: E402


def main() -> int:
    if os.environ.get("JIRA_SYNC", "").lower() not in ("on", "1", "true", "yes"):
        print("[jira-ingest] JIRA_SYNC is off — skipping.")
        return 0

    jql = os.environ.get("JIRA_JQL", "assignee = currentUser() AND status NOT IN (Done, Closed, Resolved)")
    client = DrudgeClient(timeout=30, retries=2)
    try:
        client.health()
    except Exception as e:
        print(f"[jira-ingest] drudge unreachable: {e}", file=sys.stderr)
        return 1

    try:
        data = _jira_get(f"/search?jql={urllib.parse.quote(jql)}&maxResults=50&fields=key,summary,status,priority,assignee,project,updated,description")
    except Exception as e:
        print(f"[jira-ingest] Jira query failed: {e}", file=sys.stderr)
        return 1

    issues = data.get("issues", [])
    remembered = 0
    skipped = 0
    for issue in issues:
        note = _summarize_ticket(issue)
        if note is None:
            continue
        try:
            resp = client.mcp_call(
                "remember",
                {
                    "title": note["title"],
                    "body": note["body"],
                    "origin": "jira",
                    "repo": note["project"],
                    "tags": note["tags"],
                    "claims": note["claims"],
                    "omb_session_id": note["omb_session_id"],
                    "date": note["date"],
                },
                timeout=45.0,
            )
            result = resp.get("result", "")
            if "skipped" in result.lower():
                skipped += 1
            else:
                remembered += 1
        except Exception as e:
            print(f"[jira-ingest] remember failed for {note['title']}: {e}", file=sys.stderr)

    print(f"[jira-ingest] remembered={remembered} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
