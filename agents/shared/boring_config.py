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
    """Repo root = the dir holding boring.json / boring.example.json.

    This file lives at <repo>/agents/shared/boring_config.py, so the root is two
    levels up (shared → agents → repo). The installed hooks are symlinks that
    sys.path-insert this dir and import it, but `resolve()` follows the symlink to
    the real file location here, so parents[2] is the repo root either way.
    """
    return Path(__file__).resolve().parents[2]


def _in_container() -> bool:
    """True when running inside the hermes-agent container (host paths are bind-mounted under /host)."""
    return os.path.isdir("/host/.claude") or os.path.isfile("/host/boring.json")


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
