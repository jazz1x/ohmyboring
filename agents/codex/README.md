# GitHub Codex adapter

GitHub Codex CLI reads MCP servers from `~/.codex/mcp.json`.
ohmyboring wires the `ohmyboring` MCP server automatically when
`codex` is enabled in `boring.json` and you run `install.sh`.

No Codex hook is needed — Codex calls MCP tools on demand, and the managed Hermes worker backfills eligible session transcripts.

## Session ingestion

Codex does not expose a SessionEnd hook, so sessions are ingested by a cron
worker instead:

- `agents/codex/collect-sessions.py` scans `~/.codex/sessions/**/*.jsonl`.
- It skips Codex Desktop `rollout-*` copies and subagent/guardian roll-outs by
  default, then processes one un-ingested eligible session per tick.
- When `hermes-agent` is enabled, `install.sh` adds a `codex-memory-ingest-worker`
  job that runs every 20 minutes.
- Use `make doctor` to check queue, skipped rollout copies, marker counts, worker
  state, and newest Codex note.
- On the host you can backfill manually when needed:

```bash
make doctor
COLLECT_LIMIT=10 python3 agents/codex/collect-sessions.py
```

`make doctor` is read-only and includes Codex queued sessions, skipped rollout
copies, marker counts, the hermes worker state, and the newest Codex note.

Session markers live in `~/.cache/boring-distill/codex-<sid>.*` and are shared
with the rest of the pipeline.

## Manual setup

If you prefer to wire it yourself, create or edit `~/.codex/mcp.json`:

```json
{
  "mcpServers": {
    "ohmyboring": {
      "type": "http",
      "url": "http://localhost:7700/mcp"
    }
  }
}
```

## Running Codex with MCP

```bash
# Make sure the engine is running
make up

# Start Codex with the MCP config
codex --mcp-config ~/.codex/mcp.json
```

Or set the environment variable:

```bash
CODEX_MCP_CONFIG_PATH=~/.codex/mcp.json codex
```

## Example prompts

Codex will automatically invoke the right ohmyboring tool:

- `"summarize recent oh-my-boring project status"` → `project_status` / `context`
- `"how did I solve this last time?"` → `recall`
- `"remember today's decision"` → `remember`

## Available tools

`context`, `recall`, `ask`, `remember`, `forget`, `sync`, `config_get`, `classify_repo`, `project_status`, `weekly_brief`, `decisions`, `risks`, `next_actions`, `stalled`, `brief`, `claims`, `corpus_status`, `neighbors`.
