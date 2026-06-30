# GitHub Codex adapter

GitHub Codex CLI reads MCP servers from `~/.codex/mcp.json`.
ohmyboring wires the `ohmyboring` MCP server automatically when
`codex` is enabled in `boring.json` and you run `install.sh`.

No Codex hook is needed — Codex calls MCP tools on demand, and a host launchd/cron worker backfills eligible session transcripts.

## Session ingestion

Codex does not expose a SessionEnd hook, so sessions are ingested by a periodic
worker instead:

- `agents/codex/collect-sessions.py` scans `~/.codex/sessions/**/*.jsonl`.
- Installed workers set `CODEX_INCLUDE_ROLLOUTS=1` and
  `COLLECT_STABLE_AGE_SECONDS=1800`, so stable Codex Desktop rollout transcripts
  are harvested while files still being written are skipped.
- Large transcripts are extracted into a bounded distill budget:
  `CODEX_DISTILL_CLAMP`, then `INGEST_CLAMP`, then `4000` characters. Status
  output reports `distill_clamp`, and the distill hook emits an `input_budget`
  event with raw/extracted/emitted character counts.
- True subagent/guardian roll-outs are skipped unless `CODEX_INCLUDE_SUBAGENTS=1`
  is set explicitly.
- `install.sh` registers a host launchd/cron worker that runs every 20 minutes
  when the `codex` adapter is enabled.
- When `hermes-agent` is enabled, `install.sh` also adds a
  `codex-memory-ingest-worker` job inside Hermes.
- Use `make doctor` to check queue, skipped rollout copies, marker health, host
  worker state, Hermes worker state, and newest Codex note.
- On the host you can backfill manually when needed:

```bash
make doctor
CODEX_INCLUDE_ROLLOUTS=1 COLLECT_STABLE_AGE_SECONDS=1800 COLLECT_LIMIT=10 python3 agents/codex/collect-sessions.py
```

`make doctor` is mostly read-only and includes Codex queued sessions, skipped
rollout copies, marker counts, stale pending/retry markers, dead-letter markers,
the host worker state, the Hermes worker state, and the newest Codex note. It
writes and removes one owner-only sentinel marker to prove the queue directory is
writable. Completed rollout markers may remain on disk from older runs, but
status output does not count them as successful user-session ingestion. Pending,
retry, and dead-letter rollout markers are still surfaced because they represent
failed or incomplete harvest attempts.

`make readiness` runs the same checks in strict mode. It fails when the host
worker is missing/unloaded, the Hermes `codex-memory-ingest-worker` is missing,
disabled, failed, stale-scheduled, or reports `last_error`, pending or retry
markers exceed their TTL, any dead-letter marker exists, or the newest Codex note
is older than the configured freshness window.

Session markers live in `~/.cache/boring-distill/codex-<sid>.*` and are shared
with the rest of the pipeline.

Collector status/run events include `workflow=memory_ingest`, `workflow_node`,
and `workflow_outcome`. They are appended to the local NDJSON spool and,
by default, mirrored into the engine DB so HTTP `/events` and MCP `events`
can show queued, skipped, completed, and retry-visible Codex ingestion states.

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

`context`, `recall`, `ask`, `remember`, `forget`, `sync`, `config_get`, `classify_repo`, `project_status`, `weekly_brief`, `decisions`, `risks`, `next_actions`, `stalled`, `brief`, `claims`, `corpus_status`, `events`, `neighbors`.
