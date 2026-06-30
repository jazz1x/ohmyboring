#!/usr/bin/env python3
"""Hermes cron wrapper for the canonical Codex session collector.

Hermes no-agent cron scripts must live in ~/.hermes/scripts inside the agent
container. Keep the real collector in the repo and execute it from here.
"""
import os
import runpy


def _collector_path() -> str:
    override = os.environ.get("BORING_CODEX_COLLECTOR")
    if override:
        return override
    container_path = "/host/oh-my-boring/agents/codex/collect-sessions.py"
    if os.path.exists(container_path):
        return container_path
    home = os.environ.get("BORING_HOME") or os.path.expanduser("~/oh-my-boring")
    return os.path.join(home, "agents", "codex", "collect-sessions.py")


if __name__ == "__main__":
    os.environ.setdefault("CODEX_INCLUDE_ROLLOUTS", "1")
    os.environ.setdefault("COLLECT_STABLE_AGE_SECONDS", "1800")
    runpy.run_path(_collector_path(), run_name="__main__")
