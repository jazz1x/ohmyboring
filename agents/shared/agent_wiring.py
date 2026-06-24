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

BORING_HOME = (os.environ.get("BORING_HOME") or os.environ.get("OMB_HOME")) or os.path.expanduser("~/oh-my-boring")

# Agent-specific configuration targets.
AGENTS = {
    "claude-code": {
        "kind": "hooks",
        "path": "{HOME}/.claude/settings.json",
    },
    "kimi": {
        "kind": "kimi-hooks",
        "path": "{HOME}/.kimi-code/config.toml",
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


def _agent_path(agent_id: str) -> Path:
    """Resolve the agent settings file path: config override > per-agent default."""
    cfg = boring_config.agent_config(agent_id)
    if cfg.get("settings_path"):
        return Path(os.path.expanduser(cfg["settings_path"]))
    return _expand(AGENTS[agent_id]["path"])


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


def wire_claude_code(path: Path | None = None) -> dict:
    """Idempotently wire Claude Code SessionEnd/UserPromptSubmit hooks."""
    path = path if path is not None else _agent_path("claude-code")
    _backup(path)

    settings = _load_json(path)
    settings.setdefault("hooks", {})

    distill = f"python3 {BORING_HOME}/hooks/distill-session.py"
    recall = f"python3 {BORING_HOME}/hooks/recall.py"

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


def wire_kimi(path: Path | None = None) -> dict:
    """Idempotently wire Kimi Code SessionEnd/UserPromptSubmit hooks in config.toml.

    Kimi uses TOML [[hooks]] tables rather than JSON, so we append simple blocks
    instead of round-tripping the full document. A .omb-bak copy is preserved.
    """
    path = path if path is not None else _agent_path("kimi")
    _backup(path)

    distill = f"python3 {BORING_HOME}/hooks/kimi-distill-session.py"
    recall = f"python3 {BORING_HOME}/hooks/kimi-recall.py"

    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    changed = False
    if distill not in existing or recall not in existing:
        snippet = (
            "\n[[hooks]]\n"
            'event = "SessionEnd"\n'
            f'command = "{distill}"\n'
            "timeout = 130\n"
            "\n[[hooks]]\n"
            'event = "UserPromptSubmit"\n'
            f'command = "{recall}"\n'
            "timeout = 10\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(snippet)
        changed = True

    return {"agent": "kimi", "path": str(path), "changed": changed}


def wire_mcp_agent(agent_id: str, server_name: str, server_config: dict, path: Path | None = None) -> dict:
    """Idempotently add/update an MCP server entry for a generic MCP-capable agent."""
    info = AGENTS[agent_id]
    if info["kind"] != "mcp":
        raise ValueError(f"agent {agent_id} is not an MCP agent")

    path = path if path is not None else _agent_path(agent_id)
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
    failed = False
    for agent_id in enabled_agents:
        if agent_id not in AGENTS:
            # hermes-agent is a containerized supervisor, not a host-side setting file.
            if agent_id == "hermes-agent":
                print(
                    f"[omb-wire] '{agent_id}' does not need host-side wiring — skipping",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[omb-wire] unsupported agent '{agent_id}' — skipping",
                    file=sys.stderr,
                )
            continue
        try:
            if agent_id == "claude-code":
                results.append(wire_claude_code())
            elif agent_id == "kimi":
                results.append(wire_kimi())
            else:
                results.append(wire_mcp_agent(agent_id, server_name, server_config))
        except Exception as e:
            print(f"[omb-wire] failed to wire {agent_id}: {e}", file=sys.stderr)
            failed = True
    return results, failed


def main():
    global BORING_HOME
    parser = argparse.ArgumentParser(
        description="Wire oh-my-boring adapters for enabled agents"
    )
    parser.add_argument(
        "--install", action="store_true", help="Install/update settings for enabled agents"
    )
    parser.add_argument("--server-name", default="ohmyboring")
    parser.add_argument("--server-url", default="http://localhost:7700/mcp")
    parser.add_argument("--boring-home", default=BORING_HOME)
    args = parser.parse_args()

    BORING_HOME = args.boring_home
    os.environ["BORING_HOME"] = BORING_HOME

    cfg = boring_config.load()
    enabled = [a["id"] for a in cfg.get("agents", []) if a.get("enabled", True)]

    if args.install:
        server = {"type": "http", "url": args.server_url}
        results, failed = install(enabled, args.server_name, server)
        for r in results:
            status = "updated" if r["changed"] else "already wired"
            print(f"[omb-wire] {r['agent']}: {status} ({r['path']})")
        if failed:
            sys.exit(1)
    else:
        print("enabled agents:", ", ".join(enabled) or "(none)")


if __name__ == "__main__":
    main()
