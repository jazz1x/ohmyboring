# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/), versioning per [SemVer](https://semver.org/).

## [0.1.0] — 2026-06-16

First public cut of **ohmyboring** — a self-hosted personal memory RAG. Your
Claude Code work (or any markdown notes) is distilled into a local, human-readable
wiki and recalled on demand. Zero cloud, 100% local.

### Architecture
- **Two-door model** — gated write (distill → curate) vs open/fast read (recall).
- **vault/wiki markdown is the primary memory** (Karpathy "LLM wiki"): the engine
  reads it directly, no embeddings required.
- **pgvector (vector + graph RAG) is optional** — `DRUDGE_VECTOR=on` +
  `docker compose --profile vector`. The engine runs without Postgres by default.
- **Engine-direct distillation** — the SessionEnd/Stop hook (`distill-session.py`)
  calls the local LLM directly and writes through drudge's `remember` MCP tool.
- **hermes-agent is optional** — it can drive advanced orchestration, Slack, and
  cron-based backfill via `ingest-worker.py`, but the core loop works without it.

### Engine — `drudge` (Rust, edition 2024)
- `serve`: HTTP daemon (`/health` `/ask` `/search` `/graph` `/audit` `/sync`)
  + MCP-over-HTTP (`/mcp`: `recall` · `remember` · `sync` · `config_get` ·
  `classify_repo`) + background scheduler.
- `remember`: agent/hook supplies a curated note; drudge deterministically writes
  it to `vault/wiki`, embeds, builds graph, recomputes relations.
- `wiki_recall`: direct markdown recall (substring scoring; Korean-josa friendly),
  no Postgres.
- Vector path: pgvector (HNSW) + BM25 RRF + node/edge graph (problem/solution/tool/concept).
- **LLM client is OpenAI-compatible** (`/v1`) — Ollama (default) · LM Studio · vLLM · any,
  via `DRUDGE_LLM_BASE_URL` (+ optional `DRUDGE_LLM_API_KEY`). Model swappable.

### Host hooks (Python)
- `distill-session.py` (SessionEnd/Stop): extract transcript → local LLM →
  `remember` via drudge MCP. Respects `boring.json` `note_lang` and `repos`
  (company/personal/mirror/community).
- `recall.py` (UserPromptSubmit): inject relevant past work as context.
- `collect-sessions.py`: backfill sessions missed by SessionEnd.
- `ingest-worker.py` (hermes-agent cron): serial, one-at-a-time autonomous
  ingestion for hermes-driven backfill.

### Agent
- **hermes-agent** (Nous Hermes Agent) as an optional supervisor — drives
  recall/ingest/skills via drudge's MCP memory backend when built separately.

### Tooling & CI
- `make` entrypoints (`up`/`ask`/`sync`/`remember`/`smoke`/`guard`/`deny`/…).
- CI (GitHub Actions): `rust-gate` (rustfmt + clippy `-D warnings` + tests) ·
  `gitleaks` (secret scan) · `cargo-deny` (supply chain) · `trivy` (security).
  All required on `main`.
- `pre-commit` config (file hygiene + gitleaks + fmt/clippy/test + py-compile).
- Vault templates shipped (`boring.schema.json`, example note, sample `wiki-0000.md`).

### Notes
- Naming: engine = `drudge`, project/containers = `ohmyboring`/`boring-*`
  (`omb` was rejected to avoid clashing with an existing internal `omb` CLI).
- READMEs in English (default), Korean, Japanese.

[0.1.0]: https://github.com/jazz1x/ohmyboring/releases/tag/v0.1.0
