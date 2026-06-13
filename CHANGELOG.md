# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/), versioning per [SemVer](https://semver.org/).

## [0.1.0] — 2026-06-13

First public cut of **oh-my-boring** — a self-hosted personal memory RAG. Your
Claude Code work (or any markdown notes) is distilled into a local, human-readable
wiki and recalled on demand. Zero cloud, 100% local.

### Architecture
- **Two-door model** — gated write (distill → curate) vs open/fast read (recall).
- **vault/wiki markdown is the primary memory** (Karpathy "LLM wiki"): the engine
  and agent read it directly, no embeddings required.
- **pgvector (vector + graph RAG) is optional** — `DRUDGE_VECTOR=on` +
  `docker compose --profile vector`. The engine runs without Postgres by default.
- **Engine fallback maintained** — accumulation never depends solely on the agent.

### Engine — `drudge` (Rust, edition 2024)
- `serve`: HTTP daemon (`/health` `/ask` `/search` `/graph` `/audit` `/sync`
  `/distill`) + MCP-over-HTTP (`/mcp`: `recall` · `remember` · `sync`) + background scheduler.
- `distill`: session → "problem-solving narrative" with KEEP/SKIP gate + secret scrub.
- `compile`: raw → curated `vault/wiki` (title/tags, `repo/<slug>` category, Obsidian-safe tags).
- `wiki_recall`: direct markdown recall (substring scoring; Korean-josa friendly), no Postgres.
- Vector path: pgvector (HNSW) + BM25 RRF + node/edge graph (problem/solution/tool/concept).
- **LLM client is OpenAI-compatible** (`/v1`) — Ollama (default) · LM Studio · vLLM · any,
  via `DRUDGE_LLM_BASE_URL` (+ optional `DRUDGE_LLM_API_KEY`). Model swappable.

### Host hooks (Python)
- `distill-session.py` (SessionEnd/Stop): extract transcript → engine `/distill`
  (or, opt-in `DISTILL_VIA_AGENT`, route the gate through hermes-agent).
- `recall.py` (UserPromptSubmit): inject relevant past work as context.
- `collect-sessions.py`: backfill sessions missed by SessionEnd.

### Agent
- **hermes-agent** (Nous Hermes Agent) as the brain — drives recall/ingest via
  drudge's MCP memory backend. Default core; image is third-party (build separately).

### Tooling & CI
- `make` entrypoints (`up`/`ask`/`sync`/`remember`/`smoke`/`guard`/`deny`/…).
- CI (GitHub Actions): `rust-gate` (rustfmt + clippy `-D warnings` + tests) ·
  `gitleaks` (secret scan) · `cargo-deny` (supply chain). All required on `main`.
- `pre-commit` config (file hygiene + gitleaks + fmt/clippy/test).
- Vault templates shipped (`.rules/schema.yaml`, `frontmatter.md`, example note).

### Notes
- Naming: engine = `drudge`, project/containers = `oh-my-boring`/`boring-*`
  (`omb` was rejected to avoid clashing with the marketboro `omb` CLI).
- READMEs in English (default), Korean, Japanese.

[0.1.0]: https://github.com/jazz1x/oh-my-boring/releases/tag/v0.1.0
