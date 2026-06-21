#!/usr/bin/env python3
"""Centralized environment / endpoint configuration for host-side adapters.

Avoids duplicating `localhost:7700` / `host.docker.internal` logic across shell
scripts and Python hooks. All functions honor the corresponding environment
variables and fall back to sensible defaults.
"""
import os
from urllib.parse import urlparse


def _in_container() -> bool:
    """Detect whether we are running inside a container with host bind mounts.

    The canonical signal is the env var OMB_IN_CONTAINER=1. The fallback
    checks for the /host mount used by the hermes-agent and drudge containers
    so existing stacks keep working without the env var.
    """
    env = os.environ.get("OMB_IN_CONTAINER", "").lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    return os.path.isdir("/host") and os.path.isfile("/host/boring.json")


def omb_home() -> str:
    return os.environ.get("OMB_HOME") or os.path.expanduser("~/oh-my-boring")


def drudge_url() -> str:
    return os.environ.get("DRUDGE_URL") or (
        "http://boring-drudge:7700" if _in_container() else "http://localhost:7700"
    )


def llm_base_url() -> str:
    return os.environ.get("DRUDGE_LLM_BASE_URL") or "http://localhost:11434/v1"


def llm_model() -> str:
    return os.environ.get("DRUDGE_LLM_MODEL") or "gemma4:12b"


def embed_model() -> str:
    return os.environ.get("DRUDGE_EMBED_MODEL") or "bge-m3"


def is_local_llm(url: str | None = None) -> bool:
    host = urlparse(url or llm_base_url()).hostname or ""
    return host.lower() in ("localhost", "127.0.0.1", "host.docker.internal")
