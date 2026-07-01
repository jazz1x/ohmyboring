# hermes-agent adapter

hermes-agent connects to oh-my-boring over MCP and runs cron-driven automation.

## What runs automatically

| Script | Cron source | Purpose |
|---|---|---|
| `briefing.py` | `hermes_cron_jobs.morning-briefing` (optional) | Daily morning digest via `/brief`. |
| `weekly-briefing.py` | `hermes_cron_jobs.weekly-briefing` (default) | Monday 09:00 KST weekly digest via `/weekly`. |
| `ingest-worker.py` | `memory-ingest-worker` job (not config-driven) | Pops one un-ingested Claude Code session per tick and asks the `memory-ingest` skill to store it. |
| `codex-collect-sessions.py` | `codex-memory-ingest-worker` job (not config-driven) | Hermes-safe wrapper that runs the repo collector, pops one eligible Codex session per tick, harvests stable rollout transcripts, skips true subagents, and stores it through the same remember path. |

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
- `memory-ingest-worker` and `codex-memory-ingest-worker` are managed infrastructure jobs, not `hermes_cron_jobs` entries. `make doctor` reports their health and Codex queue status.
- Ingest worker events carry `workflow=memory_ingest`, `workflow_node`, and
  `workflow_outcome` fields that mirror the Rust workflow graph contract.

## Slack delivery format

Hermes delivers cron script stdout through Slack `chat.postMessage` as plain `text` with `mrkdwn` enabled. Keep `briefing.py` and `weekly-briefing.py` output as Slack mrkdwn text, not Block Kit JSON, unless the Hermes Slack adapter grows a `blocks` path. Reference: Slack's [formatting message text](https://docs.slack.dev/messaging/formatting-message-text/), [Block Kit](https://docs.slack.dev/block-kit/), and [`chat.postMessage`](https://docs.slack.dev/reference/methods/chat.postMessage) docs.

The briefing scripts use:

- JSON request body `{}` for `/brief` and `/weekly`.
- Slack-safe headings, flat labelled bullets, compact source basenames, and no empty `Blocked: -` placeholders.
- A shared `slack_briefing.py` renderer that can emit either current Hermes-safe mrkdwn text or a Block Kit-style JSON payload.
- No eval fixture entries: `make eval` uses `eval-*.md` during the gate, then re-syncs after cleanup; the engine also excludes that internal namespace from recency/claim briefing surfaces.

Preview the exact Slack-bound message before a live briefing:

```bash
BORING_URL=http://127.0.0.1:7700 python3 agents/hermes/briefing.py
BORING_URL=http://127.0.0.1:7700 python3 agents/hermes/weekly-briefing.py
```

Preview the future Block Kit payload for Slack's Block Kit Builder or a `blocks`-aware adapter:

```bash
BORING_BRIEFING_FORMAT=blocks BORING_URL=http://127.0.0.1:7700 python3 agents/hermes/briefing.py
```

## Managed skills

`agents/hermes/skills/memory-ingest/` is copied to `~/.hermes/skills/memory-ingest/` on install. The skill tells hermes how to distill a session and call `ohmyboring/remember`, including extracting `next` and `blocked` claims for the `next_actions` register.

## Installation

Enable `hermes-agent` in `boring.json` and run `install.sh`:

```json
{ "id": "hermes-agent", "enabled": true, "adapter": "cron" }
```

This also sets `agent.environment_hint` in `~/.hermes/config.yaml` to remind hermes to call `ohmyboring/context` first.
