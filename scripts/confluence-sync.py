#!/usr/bin/env python3
"""Confluence → ohmyboring memory (read-only).

Fetches recently updated pages from a configured Confluence space, converts the
XHTML storage format to plain markdown, and remembers them with origin=confluence.

Opt-in: CONFLUENCE_SYNC=on. Requires CONFLUENCE_URL, CONFLUENCE_USER, CONFLUENCE_TOKEN.
"""
import base64
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "agents", "shared"))
from drudge_client import DrudgeClient  # noqa: E402


def _env(key: str) -> Optional[str]:
    return os.environ.get(key)


def _confluence_get(path: str) -> dict[str, Any]:
    base = (_env("CONFLUENCE_URL") or "").rstrip("/")
    user = _env("CONFLUENCE_USER") or ""
    token = _env("CONFLUENCE_TOKEN") or ""
    if not base or not user or not token:
        raise RuntimeError("CONFLUENCE_URL, CONFLUENCE_USER, CONFLUENCE_TOKEN required")
    url = f"{base}{path}" if path.startswith("/") else f"{base}/{path}"
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    headers = {
        "authorization": f"Basic {auth}",
        "content-type": "application/json",
        "accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _xhtml_to_markdown(xhtml: str) -> str:
    """Crude XHTML → markdown. Preserves structure enough for recall."""
    text = html.unescape(xhtml)
    # block tags
    text = re.sub(r"<h1[^>]*>\s*", "\n# ", text, flags=re.I)
    text = re.sub(r"<h2[^>]*>\s*", "\n## ", text, flags=re.I)
    text = re.sub(r"<h3[^>]*>\s*", "\n### ", text, flags=re.I)
    text = re.sub(r"</h[1-6]>", "\n", text, flags=re.I)
    text = re.sub(r"<p[^>]*>\s*", "\n\n", text, flags=re.I)
    text = re.sub(r"</p>", "", text, flags=re.I)
    text = re.sub(r"<li[^>]*>\s*", "\n- ", text, flags=re.I)
    text = re.sub(r"</li>", "", text, flags=re.I)
    text = re.sub(r"<ul[^>]*>|</ul>|<ol[^>]*>|</ol>", "", text, flags=re.I)
    text = re.sub(r"<code[^>]*>", "`", text, flags=re.I)
    text = re.sub(r"</code>", "`", text, flags=re.I)
    text = re.sub(r"<pre[^>]*>\s*", "\n```\n", text, flags=re.I)
    text = re.sub(r"</pre>", "\n```\n", text, flags=re.I)
    text = re.sub(r"<strong[^>]*>|</strong>|<b>|</b>", "**", text, flags=re.I)
    text = re.sub(r"<em[^>]*>|</em>|<i>|</i>", "_", text, flags=re.I)
    # strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # collapse whitespace
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _summarize_page(page: dict[str, Any]) -> Optional[dict[str, Any]]:
    pid = page.get("id", "")
    title = page.get("title", "")
    space = (((page.get("space") or {}).get("key") or "")).lower()
    history = page.get("history") or {}
    updated = history.get("lastUpdated", "")
    when = updated[:10] if isinstance(updated, str) else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    body = ""
    try:
        storage = (page.get("body") or {}).get("storage") or {}
        body = _xhtml_to_markdown(storage.get("value", ""))
    except Exception:
        body = ""
    if not title:
        return None
    slug = re.sub(r"[^a-z0-9_-]", "-", title.lower())[:40]
    project = space or "confluence"
    body_text = f"Confluence page: {title}\nURL: {page.get('_links', {}).get('webui', '')}\n\n{body[:2000]}"
    return {
        "title": f"confluence: {title}",
        "body": body_text,
        "project": project,
        "tags": ["confluence", f"repo/{project}"],
        "claims": [
            {"subject": project, "predicate": f"page/{slug}", "value": "updated", "kind": "fact", "confidence": "certain"}
        ],
        "omb_session_id": f"confluence-{pid}",
        "date": when,
    }


import urllib.request  # noqa: E402


def main() -> int:
    if os.environ.get("CONFLUENCE_SYNC", "").lower() not in ("on", "1", "true", "yes"):
        print("[confluence-sync] CONFLUENCE_SYNC is off — skipping.")
        return 0

    space = os.environ.get("CONFLUENCE_SPACE", "")
    if not space:
        print("[confluence-sync] CONFLUENCE_SPACE not set", file=sys.stderr)
        return 1

    since_hours = int(os.environ.get("CONFLUENCE_SINCE_HOURS", "24"))
    client = DrudgeClient(timeout=30, retries=2)
    try:
        client.health()
    except Exception as e:
        print(f"[confluence-sync] drudge unreachable: {e}", file=sys.stderr)
        return 1

    try:
        data = _confluence_get(
            f"/rest/api/content?spaceKey={urllib.parse.quote(space)}"
            f"&expand=body.storage,space,history,version"
            f"&limit=50&orderby=history.lastUpdated desc"
        )
    except Exception as e:
        print(f"[confluence-sync] Confluence query failed: {e}", file=sys.stderr)
        return 1

    cutoff = datetime.now(timezone.utc).timestamp() - since_hours * 3600
    pages = data.get("results", [])
    remembered = 0
    skipped = 0
    for page in pages:
        updated = (((page.get("history") or {}).get("lastUpdated") or ""))
        try:
            ts = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = 0
        if ts < cutoff:
            continue
        note = _summarize_page(page)
        if note is None:
            continue
        try:
            resp = client.mcp_call(
                "remember",
                {
                    "title": note["title"],
                    "body": note["body"],
                    "origin": "confluence",
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
            print(f"[confluence-sync] remember failed for {note['title']}: {e}", file=sys.stderr)

    print(f"[confluence-sync] remembered={remembered} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
