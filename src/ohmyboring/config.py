"""Shared boring.json loader and environment / endpoint configuration (stdlib only).

Discovery order (first wins):
  1. $BORING_CONFIG
  2. $BORING_HOME/boring.json
  3. <repo-root>/boring.json

Missing file is not an error — callers degrade gracefully to an empty policy
(personal origin, no source dirs, note_lang=auto).

Endpoint functions honor the corresponding environment variables and fall back to
sensible defaults, so `localhost:7700` / `host.docker.internal` logic lives in one place.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_ORIGIN = "personal"
DEFAULT_NOTE_LANG = "auto"

# The cards used to fire from host launchd (scripts/schedule-card.sh); hermes cron owns the
# schedule now and asks the door to run the same programs (POST /run/{morning,weekly}-card).
# Delivery stays local — the card programs post to Slack themselves, so hermes has nothing
# to deliver. The old weekly-briefing job stays paused (#425): the weekly card posts itself.
DEFAULT_HERMES_CRON_JOBS = {
    "morning-card": {
        "enabled": True,
        "schedule": "0 8 * * *",
        "script": "run-morning-card.py",
        "deliver": "local",
    },
    "weekly-card": {
        "enabled": True,
        "schedule": "0 9 * * 1",
        "script": "run-weekly-card.py",
        "deliver": "local",
    },
    "weekly-briefing": {
        "enabled": False,
        "schedule": "0 9 * * 1",
        "script": "weekly-briefing.py",
    },
}


def _in_container() -> bool:
    return os.environ.get("BORING_IN_CONTAINER", "").lower() in ("1", "true", "yes")


def omb_home() -> str:
    return os.environ.get("BORING_HOME") or os.path.expanduser("~/oh-my-boring")


def drudge_url() -> str:
    return os.environ.get("BORING_URL") or (
        "http://boring-drudge:7700" if _in_container() else "http://localhost:7700"
    )


def door_url() -> str:
    return os.environ.get("BORING_DOOR_URL") or (
        "http://boring-door:7710" if _in_container() else "http://localhost:7710"
    )


def _boring_llm() -> dict:
    """The `llm` block of boring.json (empty dict if absent/unreadable)."""
    try:
        return load().get("llm") or {}
    except Exception:  # noqa: BLE001 — config read is best-effort; defaults follow
        return {}


def llm_base_url() -> str:
    """Resolve the LLM base URL: env override (BORING_LLM_BASE_URL) → boring.json llm.base_url → default.

    On the host (not in a container) rewrite host.docker.internal → localhost, mirroring the shell
    scripts — the configured in-container default must still work for host-side distillation."""
    url = (
        os.environ.get("BORING_LLM_BASE_URL") or _boring_llm().get("base_url") or "http://localhost:11434/v1"
    )
    if not _in_container():
        url = url.replace("host.docker.internal", "localhost")
    return url


def llm_model() -> str:
    return os.environ.get("BORING_LLM_MODEL") or _boring_llm().get("model") or "gemma4:12b"


def llm_api_key() -> str:
    """API key for auth providers. boring.json names the env var holding it (api_key_env, default
    BORING_LLM_API_KEY). Empty when unset (Ollama/LM Studio need none)."""
    key_env = _boring_llm().get("api_key_env") or "BORING_LLM_API_KEY"
    return os.environ.get(key_env) or ""


def embed_model() -> str:
    """Embedding model — the engine's policy SSOT (boring.json only, no env knob). Host distillation
    mirrors that: boring.json llm.embed_model → legacy top-level embed_model → default."""
    llm = _boring_llm()
    if llm.get("embed_model"):
        return llm["embed_model"]
    try:
        top = load().get("embed_model")
        if top:
            return top
    except Exception:  # noqa: BLE001 — best-effort
        pass
    return "bge-m3"


def embed_dim() -> int:
    """Embedding vector width — the engine's policy SSOT (boring.json llm.embed_dim → 1024).

    The door's python remember writer checks every embedding against this before it
    reaches the vector(dim) columns (drudge store.rs checked_vector parity)."""
    dim = _boring_llm().get("embed_dim")
    if isinstance(dim, int) and not isinstance(dim, bool) and dim > 0:
        return dim
    return 1024


def is_local_llm(url: str | None = None) -> bool:
    host = urlparse(url or llm_base_url()).hostname or ""
    return host.lower() in ("localhost", "127.0.0.1", "host.docker.internal")


def _repo_root() -> Path:
    """Repo root = the dir holding boring.json / boring.example.json.

    This file lives at <repo>/src/ohmyboring/config.py, so the root is two levels up
    (ohmyboring → src → repo). `resolve()` follows any symlink to the real file location.
    """
    return Path(__file__).resolve().parents[2]


def discover_path() -> Path | None:
    """Return the path to boring.json, or None if not found."""
    if env := os.environ.get("BORING_CONFIG"):
        p = Path(env).expanduser()
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


@dataclass(frozen=True)
class ObsidianLink:
    vault: str
    folder: str


@dataclass(frozen=True)
class FileLink:
    """Opens the note's file in an editor — Slack does not link `file://` (measured 2026-09-30),
    but it does link an editor's own scheme (`vscode://file/…`, `cursor://file/…`)."""

    folder: str
    editor: str = "vscode"


NoteLink = ObsidianLink | FileLink


_EDITOR_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*$")


def _text(raw: dict, key: str) -> str:
    value = raw.get(key)
    return value.strip() if isinstance(value, str) else ""


def _note_link(kind: str, raw: object) -> NoteLink | str:
    """One configured link, or the reason it cannot be one. The file folder must be absolute:
    the card runs inside the door container, where `~` is that container's home, not the host's
    (measured 2026-09-30: /home/door)."""
    if not isinstance(raw, dict):
        return f"{kind} must be an object"
    match kind:
        case "obsidian" if _text(raw, "vault"):
            return ObsidianLink(vault=_text(raw, "vault"), folder=_text(raw, "folder").strip("/"))
        case "obsidian":
            return "obsidian needs a vault name"
        case "file" if _text(raw, "folder").startswith("/") and _EDITOR_SCHEME.match(
            _text(raw, "editor") or "vscode"
        ):
            return FileLink(folder=_text(raw, "folder").rstrip("/"), editor=_text(raw, "editor") or "vscode")
        case "file":
            return "file needs an absolute folder (no ~) and an editor scheme like vscode, cursor or zed"
        case _:
            return f"unknown link kind {kind!r}"


def note_links() -> tuple[NoteLink, ...]:
    """The note links the card shows beside a note id, in boring.json's order — `card.note_links`
    maps "obsidian" to {vault, folder} and "file" to {folder, editor}. Nothing configured, no links:
    the paths are the owner's, never the code's. An entry that cannot be a link is left out with
    one stderr line naming why — never silently."""
    card = load().get("card")
    if card is None:
        return ()
    if not isinstance(card, dict):
        print("[boring_config] card must be an object — no note links", file=sys.stderr)
        return ()
    links = card.get("note_links")
    if links is None:
        return ()
    if not isinstance(links, dict):
        print("[boring_config] card.note_links must be an object — no note links", file=sys.stderr)
        return ()
    parsed = [(kind, _note_link(kind, raw)) for kind, raw in links.items()]
    for kind, link in parsed:
        if isinstance(link, str):
            print(f"[boring_config] card.note_links.{kind} ignored: {link}", file=sys.stderr)
    return tuple(link for _, link in parsed if not isinstance(link, str))


def checkout_roots() -> list[str] | None:
    """Directories whose git checkouts the vault hygiene pass compares against project names.
    `None` = not configured, which callers report as "not scanned" — never as zero findings."""
    roots = load().get("checkout_roots")
    if not isinstance(roots, list):
        return None
    return [str(Path(r).expanduser()) for r in roots if isinstance(r, str) and r.strip()]


def hermes_cron_jobs() -> dict:
    """Return the configured hermes-agent cron jobs.

    If the user has not set `hermes_cron_jobs` in boring.json, default to the
    weekly briefing job paused. An explicit empty dict means "no managed jobs".
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
