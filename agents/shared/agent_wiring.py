#!/usr/bin/env python3
"""Agent wiring automation — install hooks/MCP settings for enabled agents.

Reads boring.json to decide which agents are enabled, then idempotently
configures each agent's settings file. Backups are created as `.omb-bak`.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

# Allow import of shared agent policy library regardless of how this script is invoked.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared")
)
import boring_config

OMB_HOME = os.environ.get("OMB_HOME") or os.path.expanduser("~/oh-my-boring")

# Agent-specific configuration targets.
AGENTS = {
    "claude-code": {
        "kind": "hooks",
        "path": "{HOME}/.claude/settings.json",
    },
    "cursor": {
        "kind": "mcp",
        "path": "{HOME}/.cursor/mcp.json",
        "root_key": "mcpServers",
    },
    "codex": {
        "kind": "mcp",
        "path": "{HOME}/.codex/mcp.json",
        "root_key": "mcpServers",
    },
    "windsurf": {
        "kind": "mcp",
        "path": "{HOME}/.windsurf/mcp.json",
        "root_key": "mcpServers",
    },
    "claude-desktop": {
        "kind": "mcp",
        "path": "{HOME}/.claude/mcp.json",
        "root_key": "mcpServers",
    },
}

DEFAULT_MCP_SERVER = {
    "type": "http",
    "url": "http://localhost:7700/mcp",
}


def _expand(path_template: str) -> Path:
    return Path(path_template.format(HOME=os.path.expanduser("~")))


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _backup(path: Path) -> Path:
    bak = Path(str(path) + ".omb-bak")
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)
    return bak


def _already_wired(settings: dict, command: str) -> bool:
    for group in settings.get("hooks", {}).values():
        if not isinstance(group, list):
            continue
        for entry in group:
            if not isinstance(entry, dict):
                continue
            for h in entry.get("hooks", []):
                if isinstance(h, dict) and command in (h.get("command") or ""):
                    return True
    return False


def wire_claude_code() -> dict:
    """Idempotently wire Claude Code SessionEnd/UserPromptSubmit hooks."""
    path = _expand(AGENTS["claude-code"]["path"])
    _backup(path)

    settings = _load_json(path)
    settings.setdefault("hooks", {})

    distill = f"python3 {OMB_HOME}/hooks/distill-session.py"
    recall = f"python3 {OMB_HOME}/hooks/recall.py"

    changed = False
    if not _already_wired(settings, distill):
        settings["hooks"].setdefault("SessionEnd", []).append(
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": distill,
                        "timeout": 130,
                        "async": True,
                    }
                ],
            }
        )
        changed = True

    if not _already_wired(settings, recall):
        settings["hooks"].setdefault("UserPromptSubmit", []).append(
            {
                "matcher": "",
                "hooks": [{"type": "command", "command": recall, "timeout": 10}],
            }
        )
        changed = True

    if changed:
        _save_json(path, settings)

    return {"agent": "claude-code", "path": str(path), "changed": changed}


def wire_mcp_agent(agent_id: str, server_name: str, server_config: dict) -> dict:
    """Idempotently add/update an MCP server entry for a generic MCP-capable agent."""
    info = AGENTS[agent_id]
    if info["kind"] != "mcp":
        raise ValueError(f"agent {agent_id} is not an MCP agent")

    path = _expand(info["path"])
    _backup(path)

    data = _load_json(path)
    root_key = info.get("root_key", "mcpServers")
    data.setdefault(root_key, {})

    existing = data[root_key].get(server_name)
    if existing == server_config:
        return {"agent": agent_id, "path": str(path), "changed": False}

    data[root_key][server_name] = server_config
    _save_json(path, data)
    return {"agent": agent_id, "path": str(path), "changed": True}


def install(enabled_agents, server_name, server_config):
    results = []
    for agent_id in enabled_agents:
        if agent_id not in AGENTS:
            print(
                f"[omb-wire] unsupported agent '{agent_id}' — skipping",
                file=sys.stderr,
            )
            continue
        try:
            if agent_id == "claude-code":
                results.append(wire_claude_code())
            else:
                results.append(wire_mcp_agent(agent_id, server_name, server_config))
        except Exception as e:
            print(f"[omb-wire] failed to wire {agent_id}: {e}", file=sys.stderr)
    return results


def main():
    global OMB_HOME
    parser = argparse.ArgumentParser(
        description="Wire oh-my-boring adapters for enabled agents"
    )
    parser.add_argument(
        "--install", action="store_true", help="Install/update settings for enabled agents"
    )
    parser.add_argument("--server-name", default="ohmyboring-memory")
    parser.add_argument("--server-url", default="http://localhost:7700/mcp")
    parser.add_argument("--omb-home", default=OMB_HOME)
    args = parser.parse_args()

    OMB_HOME = args.omb_home
    os.environ["OMB_HOME"] = OMB_HOME

    cfg = boring_config.load()
    enabled = [a["id"] for a in cfg.get("agents", []) if a.get("enabled", True)]

    if args.install:
        server = {"type": "http", "url": args.server_url}
        results = install(enabled, args.server_name, server)
        for r in results:
            status = "updated" if r["changed"] else "already wired"
            print(f"[omb-wire] {r['agent']}: {status} ({r['path']})")
    else:
        print("enabled agents:", ", ".join(enabled) or "(none)")


if __name__ == "__main__":
    main()
