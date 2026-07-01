#!/usr/bin/env python3
"""Shared boring.json loader for host-side Python hooks.

Discovery order (first wins):
  1. $BORING_CONFIG
  2. $BORING_HOME/boring.json
  3. <repo-root>/boring.json

Missing file is not an error — hooks degrade gracefully to an empty policy
(personal origin, no source dirs, note_lang=auto).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from omb_env import _in_container


DEFAULT_ORIGIN = "personal"
DEFAULT_NOTE_LANG = "auto"

DEFAULT_HERMES_CRON_JOBS = {
    "weekly-briefing": {
        "enabled": True,
        "schedule": "0 9 * * 1",
        "script": "weekly-briefing.py",
    }
}


def _repo_root() -> Path:
    """Repo root = the dir holding boring.json / boring.example.json.

    This file lives at <repo>/agents/shared/boring_config.py, so the root is two
    levels up (shared → agents → repo). The installed hooks are symlinks that
    sys.path-insert this dir and import it, but `resolve()` follows the symlink to
    the real file location here, so parents[2] is the repo root either way.
    """
    return Path(__file__).resolve().parents[2]


def discover_path() -> Path | None:
    """Return the path to boring.json, or None if not found."""
    if env := os.environ.get("BORING_CONFIG"):
        p = Path(env).expanduser()
        if p.is_file():
            return p
    if _in_container():
        p = Path("/host/boring.json")
        if p.is_file():
            return p
    omb_home = os.environ.get("BORING_HOME")
    if omb_home:
        p = Path(omb_home).expanduser() / "boring.json"
        if p.is_file():
            return p
    p = _repo_root() / "boring.json"
    if p.is_file():
        return p
    return None


def load() -> dict:
    """Load boring.json as a dict.

    Missing file → empty default (hooks degrade gracefully). Parse failure → loud
    stderr warning + empty default. A corrupt config must not silently look like
    "no policy set" (Layer 1: the representation must not lie).
    """
    p = discover_path()
    if not p:
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        print(f"[boring_config] cannot read {p}: {e} — using empty policy", file=sys.stderr)
        return {}
    except json.JSONDecodeError as e:
        print(
            f"[boring_config] {p} is not valid JSON ({e.lineno}:{e.colno}) — using empty policy",
            file=sys.stderr,
        )
        return {}


def note_lang() -> str:
    """Return the configured note language (auto/ko/en)."""
    cfg = load()
    return cfg.get("note_lang") or DEFAULT_NOTE_LANG


def hermes_cron_jobs() -> dict:
    """Return the configured hermes-agent cron jobs.

    If the user has not set `hermes_cron_jobs` in boring.json, default to a
    weekly briefing on Monday 09:00 KST. An explicit empty dict means "no
    managed jobs".
    """
    cfg = load()
    jobs = cfg.get("hermes_cron_jobs")
    if jobs is None:
        return dict(DEFAULT_HERMES_CRON_JOBS)
    if not isinstance(jobs, dict):
        return {}
    return jobs


def _matches(cwd: str, remote_url: str | None, matcher: str) -> bool:
    """Case-insensitive substring match against remote URL first, then cwd.

    Git identity (remote URL) is more stable than the local working-tree path,
    so prefer it when available. Fall back to cwd only when there is no remote.
    """
    needle = matcher.lower()
    if remote_url and needle in remote_url.lower():
        return True
    if needle in cwd.lower():
        return True
    return False


def classify(cwd: str, remote_url: str | None = None) -> tuple[str, str | None]:
    """Return (origin, matched_rule_name) for a repo path/remote URL.

    First matching repo rule wins. If nothing matches, origin=personal and
    matched_rule=None.
    """
    if not cwd:
        return DEFAULT_ORIGIN, None
    cfg = load()
    for rule in cfg.get("repos") or []:
        matcher = rule.get("match") or ""
        if matcher and _matches(cwd, remote_url, matcher):
            origin = rule.get("origin") or DEFAULT_ORIGIN
            return origin.lower(), rule.get("name") or matcher
    return DEFAULT_ORIGIN, None


def canonical_repo(raw_repo: str) -> str:
    """Return a canonical project/repo slug.

    Rules (in order):
      1. If a repo rule has an explicit `name` and its `match` is a substring of
         `raw_repo` (case-insensitive), use that name.
      2. Strip an org prefix (`org/repo` → `repo`).
      3. Strip a trailing `.git`.
      4. Otherwise return as-is.

    This collapses variants like `marketboro/foodspring-front` and
    `foodspring-front` into one project axis.
    """
    repo = (raw_repo or "").strip()
    if not repo:
        return repo
    repo = repo.removesuffix(".git")
    cfg = load()
    lowered = repo.lower()
    for rule in cfg.get("repos") or []:
        matcher = (rule.get("match") or "").strip()
        name = (rule.get("name") or "").strip()
        if matcher and name and matcher.lower() in lowered:
            return name
    if "/" in repo:
        return repo.split("/")[-1].strip() or repo
    return repo


def source_dirs(agent_id: str | None = None, adapter: str | None = None) -> list[str]:
    """Return enabled agent source directories with ~ expanded.

    Args:
        agent_id: if given, only paths from that agent id.
        adapter: if given, only paths from agents with this adapter (e.g. "session-end", "cron").
    """
    cfg = load()
    out = []
    for agent in cfg.get("agents") or []:
        if not agent.get("enabled", True):
            continue
        if agent_id is not None and agent.get("id") != agent_id:
            continue
        if adapter is not None and agent.get("adapter") != adapter:
            continue
        for d in agent.get("paths") or []:
            expanded = os.path.expanduser(d)
            if expanded not in out:
                out.append(expanded)
    return out


def agent_config(agent_id: str) -> dict:
    """Return the configured agent entry for agent_id, or {} if absent/disabled."""
    cfg = load()
    for agent in cfg.get("agents") or []:
        if agent.get("id") == agent_id and agent.get("enabled", True):
            return agent
    return {}
