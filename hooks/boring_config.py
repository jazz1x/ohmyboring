#!/usr/bin/env python3
"""Shared boring.json loader for host-side Python hooks.

Discovery order (first wins):
  1. $BORING_CONFIG
  2. $OMB_HOME/boring.json
  3. <repo-root>/boring.json

Missing file is not an error — hooks degrade gracefully to an empty policy
(personal origin, no source dirs, note_lang=auto).
"""
import json
import os
from pathlib import Path


DEFAULT_ORIGIN = "personal"
DEFAULT_NOTE_LANG = "auto"


def _repo_root() -> Path:
    """Directory containing this script → project root."""
    return Path(__file__).resolve().parent.parent


def discover_path() -> Path | None:
    """Return the path to boring.json, or None if not found."""
    if env := os.environ.get("BORING_CONFIG"):
        p = Path(env).expanduser()
        if p.is_file():
            return p
    omb_home = os.environ.get("OMB_HOME")
    if omb_home:
        p = Path(omb_home).expanduser() / "boring.json"
        if p.is_file():
            return p
    p = _repo_root() / "boring.json"
    if p.is_file():
        return p
    return None


def load() -> dict:
    """Load boring.json as a dict. Returns an empty default dict if no file exists."""
    p = discover_path()
    if not p:
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def note_lang() -> str:
    """Return the configured note language (auto/ko/en)."""
    cfg = load()
    return cfg.get("note_lang") or DEFAULT_NOTE_LANG


def _matches(cwd: str, remote_url: str | None, matcher: str) -> bool:
    """Case-insensitive substring match against cwd or remote URL."""
    needle = matcher.lower()
    if needle in cwd.lower():
        return True
    if remote_url and needle in remote_url.lower():
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


def source_dirs() -> list[str]:
    """Return enabled agent source directories with ~ expanded."""
    cfg = load()
    out = []
    for agent in cfg.get("agents") or []:
        if not agent.get("enabled", True):
            continue
        for d in agent.get("paths") or []:
            expanded = os.path.expanduser(d)
            if expanded not in out:
                out.append(expanded)
    return out
