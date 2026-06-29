# GitHub Codex adapter

GitHub Codex CLI reads MCP servers from `~/.codex/mcp.json`.
ohmyboring wires the `ohmyboring` MCP server automatically when
`codex` is enabled in `boring.json` and you run `install.sh`.

No hooks are needed — Codex calls MCP tools on demand.

## Session ingestion

Codex does not expose a SessionEnd hook, so sessions are ingested by a cron
worker instead:

- `agents/codex/collect-sessions.py` scans `~/.codex/sessions/**/*.jsonl`
- It skips subagent/guardian roll-outs by default and processes one un-ingested
  session per tick.
- When `hermes-agent` is enabled, `install.sh` adds a `codex-memory-ingest-worker`
  job that runs every 20 minutes.
- On the host you can backfill manually:

```bash
COLLECT_LIMIT=10 python3 agents/codex/collect-sessions.py
```

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

- `"oh-my-boring 프로젝트 최근 상황 요약해줘"` → `project_status` / `context`
- `"이전에 똑같은 문제 어떻게 해결했지?"` → `recall`
- `"오늘 작업한 내용 기억해줘"` → `remember`

## Available tools

`context`, `recall`, `ask`, `remember`, `forget`, `project_status`, `weekly_brief`, `decisions`, `risks`, `corpus_status`, `neighbors`.
