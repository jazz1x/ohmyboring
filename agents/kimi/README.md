# Kimi Code adapter

This adapter wires oh-my-boring into Kimi Code via hooks.

## Hooks

| Hook | Script | What it does |
|---|---|---|
| `SessionEnd` | `distill-session.py` | Distills a Kimi session and stores it via `ohmyboring/remember`. |
| `UserPromptSubmit` | `recall.py` | Pulls relevant memory excerpts on demand (throttled to once per session). |

## Installation

Run `install.sh` with `kimi` enabled in `boring.json`:

```json
{
  "agents": [
    { "id": "kimi", "enabled": true, "adapter": "session-end", "format": "kimi-wire", "paths": ["~/.kimi-code/sessions"] }
  ]
}
```

This writes the hook definitions into `~/.kimi-code/config.toml`.
