# Cursor adapter

Cursor reads MCP servers from `~/.cursor/mcp.json`. oh-my-boring wires the
`ohmyboring` MCP server automatically when `cursor` is enabled in
`boring.json` and you run `install.sh`.

No hooks are needed — Cursor calls MCP tools on demand.

## Manual setup

If you prefer to wire it yourself, create or edit `~/.cursor/mcp.json`:

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
