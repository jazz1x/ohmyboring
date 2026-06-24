#!/usr/bin/env python3
"""Centralized environment / endpoint configuration for host-side adapters.

Avoids duplicating `localhost:7700` / `host.docker.internal` logic across shell
scripts and Python hooks. All functions honor the corresponding environment
variables and fall back to sensible defaults.
"""
import os
import sys
from urllib.parse import urlparse


def _in_container() -> bool:
    """Detect whether we are running inside a container with host bind mounts.

    The canonical signal is the env var BORING_IN_CONTAINER=1. The fallback
    checks for the /host mount used by the hermes-agent and drudge containers
    so existing stacks keep working without the env var.
    """
    env = os.environ.get("BORING_IN_CONTAINER", "").lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    return os.path.isdir("/host") and os.path.isfile("/host/boring.json")


def omb_home() -> str:
    return (os.environ.get("BORING_HOME") or os.environ.get("OMB_HOME")) or os.path.expanduser("~/oh-my-boring")


def drudge_url() -> str:
    # BORING_URL canonical, DRUDGE_URL deprecated alias (defined below; safe — module-level call order).
    return _env_alias("BORING_URL", "DRUDGE_URL") or (
        "http://boring-drudge:7700" if _in_container() else "http://localhost:7700"
    )


def _env_alias(canonical: str, deprecated: str) -> str | None:
    """Read the canonical env var, falling back to a deprecated alias (warns once on the alias).

    Mirrors the engine's `env_alias` (llm.rs): BORING_* is canonical, DRUDGE_* the deprecated alias.
    """
    v = os.environ.get(canonical)
    if v:
        return v
    v = os.environ.get(deprecated)
    if v:
        print(f"[config] deprecated: {deprecated} is set; rename it to {canonical}", file=sys.stderr)
        return v
    return None


def _boring_llm() -> dict:
    """The `llm` block of boring.json (empty dict if absent/unreadable). Imported lazily to avoid a
    circular import (boring_config imports this module)."""
    try:
        import boring_config

        return boring_config.load().get("llm") or {}
    except Exception:  # noqa: BLE001 — config read is best-effort; defaults follow
        return {}


def llm_base_url() -> str:
    """Resolve the LLM base URL: env override (BORING_ then DRUDGE_) → boring.json llm.base_url → default.

    On the host (not in a container) rewrite host.docker.internal → localhost, mirroring the shell
    scripts — the configured in-container default must still work for host-side distillation."""
    url = (
        _env_alias("BORING_LLM_BASE_URL", "DRUDGE_LLM_BASE_URL")
        or _boring_llm().get("base_url")
        or "http://localhost:11434/v1"
    )
    if not _in_container():
        url = url.replace("host.docker.internal", "localhost")
    return url


def llm_model() -> str:
    return (
        _env_alias("BORING_LLM_MODEL", "DRUDGE_LLM_MODEL")
        or _boring_llm().get("model")
        or "gemma4:12b"
    )


def llm_api_key() -> str:
    """API key for auth providers. boring.json names the env var holding it (api_key_env, default
    BORING_LLM_API_KEY); DRUDGE_LLM_API_KEY stays as a legacy fallback. Empty when unset (Ollama/LM Studio)."""
    key_env = _boring_llm().get("api_key_env") or "BORING_LLM_API_KEY"
    return os.environ.get(key_env) or os.environ.get("DRUDGE_LLM_API_KEY") or ""


def embed_model() -> str:
    """Embedding model — the engine's policy SSOT (boring.json only, no env knob). Host distillation
    mirrors that: boring.json llm.embed_model → legacy top-level embed_model → default."""
    llm = _boring_llm()
    if llm.get("embed_model"):
        return llm["embed_model"]
    try:
        import boring_config

        top = boring_config.load().get("embed_model")
        if top:
            return top
    except Exception:  # noqa: BLE001 — best-effort
        pass
    return "bge-m3"


def is_local_llm(url: str | None = None) -> bool:
    host = urlparse(url or llm_base_url()).hostname or ""
    return host.lower() in ("localhost", "127.0.0.1", "host.docker.internal")
