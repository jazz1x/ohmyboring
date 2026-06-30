# GitHub Codex adapter

GitHub Codex CLI reads MCP servers from `~/.codex/mcp.json`.
ohmyboring wires the `ohmyboring` MCP server automatically when
`codex` is enabled in `boring.json` and you run `install.sh`.

No Codex hook is needed — Codex calls MCP tools on demand, and a host launchd/cron worker backfills eligible session transcripts.

## Session ingestion

Codex does not expose a SessionEnd hook, so sessions are ingested by a periodic
worker instead:

- `agents/codex/collect-sessions.py` scans `~/.codex/sessions/**/*.jsonl`.
- It skips Codex Desktop `rollout-*` copies and subagent/guardian roll-outs by
  default, then processes one un-ingested eligible session per tick.
- `install.sh` registers a host launchd/cron worker that runs every 20 minutes
  when the `codex` adapter is enabled.
- When `hermes-agent` is enabled, `install.sh` also adds a
  `codex-memory-ingest-worker` job inside Hermes.
- Use `make doctor` to check queue, skipped rollout copies, marker health, host
  worker state, Hermes worker state, and newest Codex note.
- On the host you can backfill manually when needed:

```bash
make doctor
COLLECT_LIMIT=10 python3 agents/codex/collect-sessions.py
```

`make doctor` is mostly read-only and includes Codex queued sessions, skipped
rollout copies, non-rollout marker counts, stale pending/retry markers,
dead-letter markers, the host worker state, the Hermes worker state, and the
newest Codex note. It writes and removes one owner-only sentinel marker to prove
the queue directory is writable. Rollout markers may remain on disk from older
runs, but status output does not count them as successful user-session ingestion.

`make readiness` runs the same checks in strict mode. It fails when the host
worker is missing/unloaded, the Hermes `codex-memory-ingest-worker` is missing,
disabled, failed, stale-scheduled, or reports `last_error`, non-rollout pending
or retry markers exceed their TTL, any dead-letter marker exists, or the newest
Codex note is older than the configured freshness window.

Session markers live in `~/.cache/boring-distill/codex-<sid>.*` and are shared
with the rest of the pipeline.

Collector status/run events include `workflow=memory_ingest`, `workflow_node`,
and `workflow_outcome` so `make events` can show the Rust workflow projection
for queued, skipped, completed, and retry-visible Codex ingestion states.

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
