# GitHub Codex adapter

GitHub Codex CLI reads MCP servers from `~/.codex/mcp.json`.
ohmyboring wires the `ohmyboring` MCP server automatically when
`codex` is enabled in `boring.json` and you run `install.sh`.

No hooks are needed — Codex calls MCP tools on demand.

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
