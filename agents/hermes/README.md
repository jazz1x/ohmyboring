# hermes-agent adapter

hermes-agent connects to oh-my-boring over MCP and runs cron-driven automation.

## What runs automatically

| Script | Cron source | Purpose |
|---|---|---|
| `briefing.py` | `hermes_cron_jobs.morning-briefing` (optional) | Daily morning digest via `/brief`. |
| `weekly-briefing.py` | `hermes_cron_jobs.weekly-briefing` (default) | Monday 09:00 KST weekly digest via `/weekly`. |
| `ingest-worker.py` | `memory-ingest-worker` job (not config-driven) | Pops one un-ingested Claude Code session per tick and asks the `memory-ingest` skill to store it. |

## Config-driven cron

`boring.json` controls managed cron jobs:

```json
{
  "hermes_cron_jobs": {
    "weekly-briefing": {
      "enabled": true,
      "schedule": "0 9 * * 1",
      "script": "weekly-briefing.py"
    },
    "morning-briefing": {
      "enabled": true,
      "schedule": "0 8 * * *",
      "script": "briefing.py"
    }
  }
}
```

- Jobs are synced into `~/.hermes/cron/jobs.json` when `agent_wiring.py` runs.
- Jobs not listed in `hermes_cron_jobs` are left untouched.
- `enabled: false` pauses the job.

## Managed skills

`agents/hermes/skills/memory-ingest/` is copied to `~/.hermes/skills/memory-ingest/` on install. The skill tells hermes how to distill a session and call `ohmyboring/remember`.

## Installation

Enable `hermes-agent` in `boring.json` and run `install.sh`:

```json
{ "id": "hermes-agent", "enabled": true, "adapter": "cron" }
```

This also sets `agent.environment_hint` in `~/.hermes/config.yaml` to remind hermes to call `ohmyboring/context` first.
