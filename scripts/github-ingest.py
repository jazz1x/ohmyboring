#!/usr/bin/env python3
"""GitHub activity → ohmyboring memory.

Fetches recent events for the authenticated user via the `gh` CLI, summarizes
relevant ones (issues, PRs, reviews, pushes) into notes, and calls
ohmyboring/remember with origin=github.

Opt-in: set GITHUB_SYNC=on in the environment.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "agents", "shared"))
from drudge_client import DrudgeClient  # noqa: E402

RELEVANT_EVENTS = {"IssuesEvent", "PullRequestEvent", "PullRequestReviewEvent", "IssueCommentEvent", "PushEvent", "CreateEvent"}


def _run_gh(args: list[str]) -> list[dict[str, Any]]:
    cmd = ["gh", "api", "--paginate"] + args
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=120)
    except FileNotFoundError:
        print("[github-ingest] gh CLI not found — install GitHub CLI and auth with `gh auth login`", file=sys.stderr)
        return []
    except subprocess.CalledProcessError as e:
        print(f"[github-ingest] gh failed: {e.output.decode('utf-8', errors='ignore')}", file=sys.stderr)
        return []
    # --paginate with gh returns a JSON array of pages when there are multiple pages.
    text = out.decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Sometimes gh returns concatenated arrays; try to split.
        results: list[dict[str, Any]] = []
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(text):
            try:
                obj, end = decoder.raw_decode(text, idx)
                if isinstance(obj, list):
                    results.extend(obj)
                idx = end
            except json.JSONDecodeError:
                idx += 1
        return results


def _slug(repo_name: str) -> str:
    return repo_name.split("/")[-1]


def _iso_to_date(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _summarize(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    etype = event.get("type", "")
    repo = event.get("repo", {}).get("name", "")
    payload = event.get("payload") or {}
    created = event.get("created_at", "")
    if etype not in RELEVANT_EVENTS or not repo:
        return None

    slug = _slug(repo)
    actor = event.get("actor", {}).get("login", "")
    date = _iso_to_date(created)

    if etype == "IssuesEvent":
        action = payload.get("action", "")
        issue = payload.get("issue") or {}
        num = issue.get("number", "")
        title = issue.get("title", "")
        url = issue.get("html_url", "")
        body = f"GitHub issue #{num} in {repo}: {action}.\n\n{title}\n{url}"
        return {
            "title": f"github: {slug} issue #{num} {action}",
            "body": body,
            "project": slug,
            "tags": ["github", f"repo/{slug}"],
            "claims": [
                {"subject": slug, "predicate": f"issue/{num}", "value": action, "kind": "fact", "confidence": "certain"}
            ],
            "omb_session_id": f"github-issue-{repo}-{num}",
            "date": date,
        }

    if etype == "PullRequestEvent":
        action = payload.get("action", "")
        pr = payload.get("pull_request") or {}
        num = pr.get("number", "")
        title = pr.get("title", "")
        url = pr.get("html_url", "")
        merged = pr.get("merged", False)
        if action == "closed" and merged:
            action = "merged"
        body = f"GitHub PR #{num} in {repo}: {action}.\n\n{title}\n{url}"
        return {
            "title": f"github: {slug} PR #{num} {action}",
            "body": body,
            "project": slug,
            "tags": ["github", f"repo/{slug}"],
            "claims": [
                {"subject": slug, "predicate": f"pr/{num}", "value": action, "kind": "fact", "confidence": "certain"}
            ],
            "omb_session_id": f"github-pr-{repo}-{num}",
            "date": date,
        }

    if etype == "PullRequestReviewEvent":
        pr = payload.get("pull_request") or {}
        review = payload.get("review") or {}
        num = pr.get("number", "")
        state = review.get("state", "")
        url = review.get("html_url", "")
        body = f"GitHub PR #{num} in {repo}: review {state}.\n\n{url}"
        return {
            "title": f"github: {slug} PR #{num} review {state}",
            "body": body,
            "project": slug,
            "tags": ["github", f"repo/{slug}"],
            "claims": [
                {"subject": slug, "predicate": f"pr/{num}-review", "value": state, "kind": "fact", "confidence": "certain"}
            ],
            "omb_session_id": f"github-pr-review-{repo}-{num}-{review.get('id','')}",
            "date": date,
        }

    if etype == "IssueCommentEvent":
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        num = issue.get("number", "")
        url = comment.get("html_url", "")
        body = f"Comment on {repo}#{num}.\n\n{url}"
        return {
            "title": f"github: {slug} #{num} comment",
            "body": body,
            "project": slug,
            "tags": ["github", f"repo/{slug}"],
            "claims": [],
            "omb_session_id": f"github-comment-{repo}-{num}-{comment.get('id','')}",
            "date": date,
        }

    if etype == "PushEvent":
        ref = payload.get("ref", "")
        commits = payload.get("commits", [])
        branch = ref.split("/")[-1] if ref else "unknown"
        commit_lines = "\n".join(f"- {c.get('message','')}" for c in commits[:5])
        body = f"Pushed {len(commits)} commit(s) to {repo}:{branch}.\n\n{commit_lines}"
        return {
            "title": f"github: {slug} push to {branch}",
            "body": body,
            "project": slug,
            "tags": ["github", f"repo/{slug}"],
            "claims": [
                {"subject": slug, "predicate": f"branch/{branch}", "value": f"pushed {len(commits)} commits", "kind": "fact", "confidence": "certain"}
            ],
            "omb_session_id": f"github-push-{repo}-{branch}-{event.get('id','')}",
            "date": date,
        }

    if etype == "CreateEvent":
        ref_type = payload.get("ref_type", "")
        ref = payload.get("ref", "")
        body = f"Created {ref_type} {ref} in {repo}."
        return {
            "title": f"github: {slug} created {ref_type} {ref}",
            "body": body,
            "project": slug,
            "tags": ["github", f"repo/{slug}"],
            "claims": [
                {"subject": slug, "predicate": f"{ref_type}/{ref}", "value": "created", "kind": "fact", "confidence": "certain"}
            ],
            "omb_session_id": f"github-create-{repo}-{ref_type}-{ref}",
            "date": date,
        }

    return None


def main() -> int:
    if os.environ.get("GITHUB_SYNC", "").lower() not in ("on", "1", "true", "yes"):
        print("[github-ingest] GITHUB_SYNC is off — skipping.")
        return 0

    since_hours = int(os.environ.get("GITHUB_SINCE_HOURS", "24"))
    client = DrudgeClient(timeout=30, retries=2)

    # Verify health.
    try:
        health = client.health()
        if health.get("status") != "ok":
            print("[github-ingest] drudge not healthy", file=sys.stderr)
            return 1
    except Exception as e:
        print(f"[github-ingest] drudge unreachable: {e}", file=sys.stderr)
        return 1

    events = _run_gh(["user/events"])
    if not events:
        print("[github-ingest] no events returned.")
        return 0

    cutoff = datetime.now(timezone.utc).timestamp() - since_hours * 3600
    remembered = 0
    skipped = 0
    for event in events:
        created = event.get("created_at", "")
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if ts < cutoff:
            continue
        note = _summarize(event)
        if note is None:
            continue
        try:
            resp = client.mcp_call(
                "remember",
                {
                    "title": note["title"],
                    "body": note["body"],
                    "origin": "github",
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
            print(f"[github-ingest] remember failed for {note['title']}: {e}", file=sys.stderr)

    print(f"[github-ingest] remembered={remembered} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
