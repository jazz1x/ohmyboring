# Claude Code adapter

This adapter wires oh-my-boring into Claude Code via hooks.

## Hooks

| Hook | Script | What it does |
|---|---|---|
| `SessionStart` | `session-start-recall.py` | Calls `POST /context` and injects `{decisions, risks, facts, glossary}` as compact additional context. Falls back to recent work if no project is detected. |
| `UserPromptSubmit` | `recall.py` | Prepends relevant memory excerpts to **every** prompt (no throttle). Also records what it injected, so `SessionEnd` can measure whether any of it was used. |
| `SessionEnd` / `Stop` | `distill-session.py` | Distills the session into a durable note and stores it via `ohmyboring/remember`. |

`distill-session.py` emits `distill_resolution` events with
`workflow=memory_ingest`, `workflow_node`, and `workflow_outcome`, including
intentional `skipped` outcomes when the model decides a session has no durable
work to store.

## Installation

Run `install.sh` (or `python3 agents/shared/agent_wiring.py --install`) with `claude-code` enabled in `boring.json`:

```json
{
  "agents": [
    { "id": "claude-code", "enabled": true, "adapter": "session-end", "format": "claude-json", "paths": ["~/.claude/projects"] }
  ]
}
```

This writes the hook definitions into `~/.claude/settings.json`.

## Manual setup

This directory has no `settings.json` to copy from. Add the hook entries to your Claude Code settings yourself and point their commands at the scripts below (the ones `agent_wiring.py` registers, `_CLAUDE_CODE_ROLE_SCRIPTS`): `hooks/distill-session.py`, `hooks/recall.py`, `hooks/rules.py` (symlinks to the scripts in this directory) and `agents/claude-code/session-start-recall.py`.
