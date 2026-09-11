# ohmyboring

**English** · [한국어](README.ko.md) · [日本語](README.ja.md)

ohmyboring turns your coding-agent sessions (Claude Code, Kimi Code, Codex) into a local markdown wiki and injects what you solved before into the prompt where you are about to solve it again. It captures how you solved things; recall precision is being measured in the open (§ Status). Everything runs on your machine with a local LLM — no cloud, no tokens.

## Quick start

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/jazz1x/ohmyboring/main/install.sh)"
```

The one-liner clones to `~/oh-my-boring`, builds, and wires the hooks, MCP entries and workers. Step by step:

```bash
git clone https://github.com/jazz1x/ohmyboring.git ~/oh-my-boring
cd ~/oh-my-boring
make up             # start the engine (starts and pulls Ollama models when llm.provider is ollama)
make verify-llm     # provider reachable, both model ids present, embedding dimension matches
make doctor         # stack, hooks, workers, newest note — every finding printed, none hidden
make collect N=20   # seed the vault from your past Claude Code sessions; a fresh clone is empty
make ask Q="how did I fix the docker build cache problem?"
```

You are done when `make up` exits 0 and `http://127.0.0.1:7700/health` returns 200, `make verify-llm` prints `configuration looks consistent`, and `make doctor` shows no `✗`.

Needs Docker, Python 3, jq, curl, git, make, and a local LLM server: Ollama (default; `make up` starts it) or LM Studio (start its server, load one chat and one embedding model, set `llm.provider` to `lmstudio`, run `make verify-llm`). Any other OpenAI-compatible `/v1` endpoint works with `llm.provider: openai-compatible`.

## What it does

When a session ends, the `SessionEnd` hook distills the transcript with the local LLM into one markdown note under `vault/wiki/` and stores it through the engine's `remember` door. When you type a prompt, the `UserPromptSubmit` hook searches the vault and injects up to three past notes — each with one older note it shares a concept with — behind a fence that tells the agent what to do with them: reuse, say which contradicts the code, or say nothing. At the next session end the engine learns what happened: notes the agent reused and notes it argued with become edges, and the next injection shows `reused n×` / `contested n×` beside each note and orders them by it.

Codex has no session hook; a host worker picks up eligible Codex transcripts every 20 minutes. `make collect` backfills past Claude Code sessions, `make collect-kimi` past Kimi sessions, `make distill-now` captures the current session without ending it, and `make remember M="…"` stores a note you write yourself.

The vault is the source of truth: plain markdown you can open as an [Obsidian](https://obsidian.md) vault (tags and `[[wiki-NNNN]]` links are already there). With `BORING_VECTOR=on` the engine keeps a pgvector index and a graph (notes, concepts, tools, claims, sessions) rebuilt from the vault by `make sync`; without it, recall reads the markdown directly. `make peek` opens a read-only local page (`127.0.0.1:7788`) showing what was injected into your sessions and whether it was used.

## Configuration

Policy lives in `boring.json`, created from `boring.example.json` by `make up` and validated against `boring.schema.json`. The keys you will touch:

| Key | Meaning | Read by |
|---|---|---|
| `llm.provider` | `ollama` (pulls models) · `lmstudio` (load in-app) · `openai-compatible` | `scripts/llm-providers/<provider>.sh`, `agents/shared/omb_env.py` |
| `llm.base_url` · `llm.model` | OpenAI-compatible `/v1` endpoint and the chat model used for distillation and `ask` | same |
| `llm.embed_model` · `llm.embed_dim` | embedding model and its vector size — changing the model means updating the dim and running `make reset` | same; the engine checks the dim on start |
| `note_lang` | `auto` · `ko` · `en` — language the notes are written in | `agents/shared/boring_config.py` |
| `repos[]` | path/remote rules → `origin` (`personal` / `company` / `mirror` / `community`); company-origin prose never leaves the engine | `agents/shared/boring_config.py` |
| `agents[]` | which agents are wired (hooks, MCP, workers) | `agents/shared/agent_wiring.py` |

Inside the container the LLM is reached as `host.docker.internal`; on the host as `localhost` — `boring.example.json` already has the container form.

`.env` holds only secrets and runtime overrides. Every `BORING_*` variable the code reads is listed with its default in `.env.example`; when the two disagree the code wins, and the table is not repeated here. The ones people actually set: `BORING_VECTOR=on` (pgvector + graph), `BORING_LLM_API_KEY` (when the provider needs one), `BORING_EVENT_SINK=spool` (write events to a local file instead of the engine — what tests and probes use).

## Commands

`make help` lists all 50 targets with one line each. Daily ones:

| Command | What it does |
|---|---|
| `make up` / `make down` | start / stop the stack |
| `make doctor` | diagnose stack, hooks, newest note, Codex worker; `make readiness` is the strict form that fails on any finding |
| `make ask Q="…"` | one question answered from memory with sources |
| `make remember M="…"` | store a note now |
| `make collect [N=1]` · `make collect-kimi [N=1]` · `make distill-now` | backfill past sessions · capture the current one |
| `make sync` | rebuild index and graph from the vault (also runs every 4 hours) |
| `make peek` · `make events [N=20]` · `make usage` | what was injected and used · recent workflow events · token usage from local transcripts |
| `make guard` · `make quality` · `make eval` | structural gate (fmt, clippy, tests, Python) · release contract gate · recall regression gate |

The engine also speaks MCP at `http://localhost:7700/mcp`; `install.sh` registers it for Claude Code, Kimi, Cursor and Codex, and `.mcp.json` at the repo root is the standard entry for any other client.

Available tools (22): `recall`, `neighbors`, `claims` (memory retrieval) · `code_search`, `code_symbol`, `code_index_status` (separate AST code corpus) · `ask`, `brief`, `weekly_brief`, `project_status`, `decisions`, `risks`, `next_actions`, `stalled` (generative — run the LLM) · `context`, `corpus_status`, `events`, `config_get` (structured / introspection) · `remember`, `forget`, `classify_repo`, `sync` (write / maintain).

In the default wiki-first mode (`BORING_VECTOR=off`) the tools that need the graph, recency ordering or the event DB return JSON-RPC `-32603` until you set `BORING_VECTOR=on`: `neighbors`, `claims`, `corpus_status`, `events`, `brief`, `weekly_brief`, `project_status`, `decisions`, `risks`, `next_actions`, `stalled`. The rest work without it: `recall`, `ask`, `context`, `remember`, `forget`, `sync`, `config_get`, `classify_repo`, `code_index_status`, `code_search`, `code_symbol` (the three code tools need an enabled `code_index` source and a prior `code-sync`).

## Status

Every number below has a command that produced it.

- Recall regression floor: 22/22 golden queries, MRR 1.000, on an 18-document fixture corpus (`make eval`). A wiring floor, not a quality claim — 18 documents have no near competitors and production does.
- Recall precision: LLM judge 6 relevant / 24 judged; the human audit needed to calibrate that judge is under its floor of 30, so no precision figure is published yet (`make peek`, `label_core.py`).
- Whether injected notes get used: a pre-registered measurement (per-prompt uptake of injected notes against a same-pool control, `docs/PRD.md` §2) runs over a window whose dates are registered in `agents/shared/verdict_core.py`; `make peek` shows where it stands. No verdict is quoted until the sample floor (20 sessions, 200 injected prompts) is met.
- Embedding: `bge-m3` averaged 0.105 s per text on a MacBook Pro M5 Pro 48 GB with local Ollama (`make bench-embed`). Distillation pairs by RAM tier and their latencies: `make bench-llm-tier TIER=16gb|32gb`; results in `docs/reports/llm-pair-matrix.md`.
- Delivery: `/health` reports `build_sha`; `make doctor` fails when the engine, the host CLI binary, or the installed hook scripts are behind the checkout.

## Non-goals

- No cloud, no shared team memory, no ingestion of company knowledge bases — the vault is one person's session experience.
- No interactive UI product. `make peek` is a read-only loopback page, and that is the boundary.
- No Windows yet: `hooks/` uses symlinks for backward compatibility. Tested on macOS and Linux.
