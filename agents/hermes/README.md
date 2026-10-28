# hermes-agent adapter

hermes-agent connects to oh-my-boring over MCP and runs cron-driven automation.

## What runs automatically

| Script | Cron source | Purpose |
|---|---|---|
| `briefing.py` | `hermes_cron_jobs.morning-briefing` (optional) | Daily morning digest via `/brief`. |
| `weekly-briefing.py` | `hermes_cron_jobs.weekly-briefing` (default `enabled: false`) | Weekly digest text. The Monday card does not use this job: `agents/slack/weekly_card.py` runs this script in blocks mode and posts the payload with the bot token — hermes is out of the delivery path (its cron wrapper re-rendered our mrkdwn as markdown). |
| `run-morning-card.py` | `hermes_cron_jobs.morning-card` (`0 8 * * *`, `deliver: local`) | Runs the morning card (`agents/slack/card.py`) through `card_runner.run_card`. |
| `run-weekly-card.py` | `hermes_cron_jobs.weekly-card` (`0 9 * * 1`, `deliver: local`) | Runs the weekly card (`agents/slack/weekly_card.py`) through `card_runner.run_card`. |
| `run-repair-judge.py` | not in the default jobs | Runs the name-merge judge (`agents/slack/card_repair_judge.py`) through `card_runner.run_card`. |
| `run-advice-write.py` | not in the default jobs | Pre-writes the morning card's advice (`agents/slack/card_advice_write.py`) through `card_runner.run_card`; it has to run before the card. |
| `ingest-worker.py` | `memory-ingest-worker` job (not config-driven) | Enqueues eligible Claude Code / Codex sessions into the shared distill queue and distills up to QUEUE_PER_TICK (default 3) per tick through the LangGraph distill engine. |
| `codex-collect-sessions.py` | `codex-memory-ingest-worker` job (not config-driven) | Hermes-safe wrapper that runs the repo collector, pops one eligible Codex session per tick, harvests stable rollout transcripts, skips true subagents, and stores it through the same remember path. |

## Config-driven cron

`boring.json` controls managed cron jobs:

```json
{
  "hermes_cron_jobs": {
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
- Defaults when `boring.json` has no `hermes_cron_jobs` entry (`src/ohmyboring/config.py` `DEFAULT_HERMES_CRON_JOBS`): `morning-card` and `weekly-card` are enabled and run the card scripts (the launchd job is gone); `weekly-briefing` is `enabled: false` because the weekly card posts itself. Do not enable it next to `weekly-card`, or Monday gets two briefings.
- `card_runner.py` holds the per-card lock and the timeout (`DOOR_RUN_CARD_TIMEOUT`), reads `SLACK_BOT_TOKEN` from `$HERMES_HOME/.env` and hands it only to the card process, and finds the card scripts under `$BORING_CARD_ROOT` (default: the repo this file sits in).
- `memory-ingest-worker` and `codex-memory-ingest-worker` are managed infrastructure jobs, not `hermes_cron_jobs` entries. `make doctor` reports their health and Codex queue status.
- Ingest worker events carry `workflow=memory_ingest`, `workflow_node`, and
  `workflow_outcome` fields that mirror the Rust workflow graph contract.

## Plugins

`agents/hermes/plugins/` holds two standalone hermes plugins: `boring-card` receives the morning card's button presses over the one Slack socket and applies the same effects `card.py` would, and `boring-memory` hands hermes the owner's own memory (the wiki notes the message names, then an engine recall) before it answers a Slack DM.

## Slack delivery format

Hermes delivers cron script stdout through Slack `chat.postMessage` as plain `text` with `mrkdwn` enabled, so `briefing.py` output stays Slack mrkdwn text, not Block Kit JSON, unless the Hermes Slack adapter grows a `blocks` path. Reference: Slack's [formatting message text](https://docs.slack.dev/messaging/formatting-message-text/), [Block Kit](https://docs.slack.dev/block-kit/), and [`chat.postMessage`](https://docs.slack.dev/reference/methods/chat.postMessage) docs.

`weekly-briefing.py` is different: it no longer ships through hermes. It keeps both renderings — mrkdwn text by default, a Block Kit JSON payload under `BORING_BRIEFING_FORMAT=blocks` — because `agents/slack/weekly_card.py` runs it in blocks mode and posts the payload with the bot token to `SLACK_CARD_CHANNEL` (hermes cron `weekly-card`, `run-weekly-card.py`, Monday 09:00). If a `hermes_cron_jobs.weekly-briefing` entry is enabled, disable it: two senders means two Monday briefings.

The briefing scripts use:

- JSON request body `{}` for `/brief` and `/weekly`.
- Slack-safe headings, priority-first grouped bullets (Blocked → Next → Stalled → Risks → Decisions → Done), compact source basenames, and no empty placeholders.
- A shared `slack_briefing.py` renderer that can emit either current Hermes-safe mrkdwn text or a Block Kit-style JSON payload.
- No eval fixture entries: `make eval` uses `eval-*.md` during the gate, then re-syncs after cleanup; the engine also excludes that internal namespace from recency/claim briefing surfaces.

Preview the exact Slack-bound message before a live briefing:

```bash
uv run --locked python agents/hermes/briefing.py
uv run --locked python agents/hermes/weekly-briefing.py
```

Preview the exact Block Kit payload the weekly card posts (what `weekly_card.py` sends to Slack):

```bash
BORING_BRIEFING_FORMAT=blocks uv run --locked python agents/hermes/weekly-briefing.py
```

Preview the future Block Kit payload for Slack's Block Kit Builder or a `blocks`-aware adapter:

```bash
BORING_BRIEFING_FORMAT=blocks uv run --locked python agents/hermes/briefing.py
```

## Installation

Enable `hermes-agent` in `boring.json` and run `install.sh`:

```json
{ "id": "hermes-agent", "enabled": true, "adapter": "cron" }
```

This also sets `agent.environment_hint` in `~/.hermes/config.yaml` to remind hermes to call `ohmyboring/context` first.
