# GitHub Codex adapter

GitHub Codex (and the Copilot CLI) reads MCP servers from `~/.codex/mcp.json`.
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
