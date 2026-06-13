# oh-my-boring

**English** · [한국어](README.ko.md) · [日本語](README.ja.md)

[![CI](https://github.com/jazz1x/oh-my-boring/actions/workflows/ci.yml/badge.svg)](https://github.com/jazz1x/oh-my-boring/actions/workflows/ci.yml)
![Rust](https://img.shields.io/badge/engine-Rust%20edition%202024-000?logo=rust)
![memory](https://img.shields.io/badge/memory-vault%2Fwiki%20(markdown)-success)
![LLM](https://img.shields.io/badge/LLM-OpenAI--compatible%20(Ollama%2FLM%20Studio%2F…)-000)
![cloud](https://img.shields.io/badge/cloud-none-success)

**Self-hosted personal memory RAG.** Your work in Claude Code (or any markdown notes) is distilled into a local, human-readable wiki and recalled on demand — *"how did I do this last time?"* **Zero cloud · 100% local.**

> The boring chore you keep skipping — remembering past work and digging it back up — is what the **drudge** engine quietly does for you.

```text
            WRITE (gated)                                READ (open, fast)
  ┌────────────────────────────────┐          ┌──────────────────────────────┐
  session ──distill──▶ vault/raw    │          │  "what was X?"                │
  (Claude Code,        ──compile──▶ vault/wiki ◀──────  make ask · recall.py    │
   SessionEnd hook)    (curate)     │  *.md    │         · Slack · MCP recall   │
  └────────────────────────────────┘  (1급)   └──────────────────────────────┘
                                         │
                          (optional) ────┴──── DRUDGE_VECTOR=on
                                    pgvector: embeddings + graph RAG
```

**vault/wiki markdown is the primary memory** — the agent and engine read it directly (no embeddings needed). pgvector (vector + graph RAG) is an **optional accelerator** you switch on when you want it.

---

## Why

- **Auto-accumulation** — when a session ends, it's distilled into a "problem-solving narrative" and curated into `vault/wiki`. No manual upkeep.
- **Markdown-first** — memory is plain, human-readable, git-diffable markdown you can read and edit. Recall reads it directly (the Karpathy "LLM wiki" approach; simplest thing that works at personal scale).
- **Local-only** — embedding/synthesis run on a local OpenAI-compatible LLM server (Ollama by default). Zero external APIs/tokens.
- **Optional vector + graph** — flip `DRUDGE_VECTOR=on` for pgvector similarity + GraphRAG (problem/solution/tool/concept nodes) when scale or precision calls for it.

---

## Layers

| # | Layer | Role | Default in `make up` |
|---|---|---|:---:|
| 1 | **LLM server** (host, OpenAI-compatible) | embedding `bge-m3` · synthesis `gemma4:12b` — Ollama default, swap for LM Studio/vLLM via `DRUDGE_LLM_BASE_URL` | required[^llm] |
| 2 | **drudge** (Rust engine) | distill · compile (raw→wiki) · recall · serve (HTTP + MCP + scheduler) | ✓ |
| 3 | **vault/wiki** (markdown) | the primary memory — curated notes, read directly | ✓ (files) |
| 4 | **hooks** (host, Python) | session→engine glue (distill · recall · collect) | manual install[^hooks] |
| 5 | **hermes-agent** (the brain) | autonomous agent driving ingest/recall/skills (MCP) | ✓[^agent] |
| 6 | **Postgres + pgvector** | vector (HNSW) + BM25 + graph — **optional** accelerator | ✗ (`--profile vector`)[^vec] |

[^llm]: An OpenAI-compatible `/v1` server on the host (default `ollama serve`). Point elsewhere with `DRUDGE_LLM_BASE_URL` (e.g. LM Studio `:1234/v1`).
[^hooks]: Register in `~/.claude/settings.json` — see [Self-augmentation loop](#self-augmentation-loop).
[^agent]: Third-party image (Nous Hermes Agent), not bundled — build it first ([Prerequisites](#prerequisites)). `start.sh` stops with a hint if missing.
[^vec]: Off by default (wiki is primary). Enable with `DRUDGE_VECTOR=on` + `docker compose --profile vector up` (brings up Postgres).

> Core = LLM (1) + drudge (2) + wiki files (3). Hooks (4) drive auto-capture; the agent (5) is the brain; pgvector (6) is opt-in.

---

## Two doors (read / write)

Reads and writes are asymmetric, so they use different doors:

- **Read door (open, fast)** — recall reads `vault/wiki` directly (~ms, no LLM loop, safe to expose widely). Used by `recall.py`, `make ask`, MCP `recall`, Slack. Reads never need the agent.
- **Write door (gated)** — accumulation is judged: is this worth keeping, how to curate? By default the **engine** distills + gates (deterministic, reliable). Opt into `DRUDGE_VECTOR=on` for vector storage, and `DISTILL_VIA_AGENT=1` to route the gate through the agent's own judgment.

---

## Prerequisites

| Install | Purpose | Check |
|---|---|---|
| **Docker** (Compose v2) | container stack | `docker compose version` |
| **LLM runtime** (OpenAI-compatible) | local embedding/synthesis | default **Ollama** ([ollama.com](https://ollama.com) / `brew install ollama`). LM Studio/vLLM also work |
| **Python 3** | host hooks | `python3 --version` (ships with macOS) |
| **hermes-agent image** | the brain (default core) | `docker image inspect hermes-agent` · else build [Nous Hermes Agent](https://github.com/NousResearch) + prepare `~/.hermes` |
| ~10GB disk | two models | `gemma4:12b` (~8GB) + `bge-m3` (~1.2GB) — `make up`/`make models` pulls them |

> **Clone location**: `~/oh-my-boring` recommended (hook/`start.sh`/vault paths assume it).

---

## Quick start

```bash
git clone git@github.com:jazz1x/oh-my-boring.git ~/oh-my-boring
cd ~/oh-my-boring
cp .env.example .env          # optional (core runs without it)
make up                       # check Ollama → pull models → build → start (wiki mode)
make ask Q="how did I fix the docker build cache problem?"
```

`make up` (wiki default) starts **drudge + hermes-agent** — no Postgres. To use vector + graph RAG: `DRUDGE_VECTOR=on make up` (adds Postgres via `--profile vector`).

---

## Self-augmentation loop

When a session ends it accumulates on its own — the core value.

```text
① end/stop  →  distill-session.py (SessionEnd/Stop hook)
                distill the session → vault/raw  (engine; or the agent, opt-in)
② compile   →  raw → vault/wiki  (LLM curation: title, tags, repo/<slug>)
                [scheduler · make sync · right after a session]
③ recall    →  make ask / recall.py / Slack / MCP  →  reads vault/wiki directly
                (+ pgvector similarity & graph when DRUDGE_VECTOR=on)
```

| Hook | Claude Code event | What it does |
|---|---|---|
| `hooks/distill-session.py` | `SessionEnd` / `Stop` | distill session → vault/raw (engine, or agent if `DISTILL_VIA_AGENT=1`) |
| `hooks/recall.py` | `UserPromptSubmit` | recall relevant past work and inject it as context |
| `hooks/collect-sessions.py` | cron / `make collect` | backfill sessions missed by SessionEnd |

**Install** (persist) — `~/.claude/settings.json`:

```jsonc
{
  "hooks": {
    "SessionEnd": [
      { "type": "command", "command": "python3 ~/oh-my-boring/hooks/distill-session.py", "timeout": 130, "async": true }
    ],
    "UserPromptSubmit": [
      { "type": "command", "command": "python3 ~/oh-my-boring/hooks/recall.py", "timeout": 10 }
    ]
  }
}
```

> The engine (drudge) must be up for distill/recall. If not, they silently no-op — a session is never blocked.

---

## Connecting Nous Hermes Agent

drudge is the agent's **MCP memory backend**. The agent (brain) drives; drudge (hands) does the mechanics.

1. The agent comes up with `make up` (image must be prebuilt — see [Prerequisites](#prerequisites)).
2. Register drudge as an MCP server in `~/.hermes/config.yaml`:
   ```yaml
   mcp_servers:
     drudge:
       url: http://drudge:7700/mcp   # same compose network
       transport: http
   ```
3. MCP tools (drudge `/mcp`): `recall{query}` (read), `remember{text,title?}` (write a note), `sync{}` (compile → ingest).

---

## Deployment: Docker / native

| Mode | How | When |
|---|---|---|
| **Docker** (default) | `make up` — drudge + hermes-agent (+ Postgres with `DRUDGE_VECTOR=on`) | simplest |
| **Native** | `cd drudge && cargo run --release -- serve` | no containers / dev. Set env: `DRUDGE_LLM_BASE_URL`, `DRUDGE_VAULT_DIR`, `DRUDGE_SOURCE_DIRS` (+ `PG_DSN` if vector). drudge is a single static binary |

---

## Command reference

`make help` for all. Common:

| Command | Description |
|---|---|
| `make up` | set up + start (wiki mode; `DRUDGE_VECTOR=on` for vector) |
| `make ask Q="question"` | one-shot query (recall + synthesis + sources) |
| `make sync` | distill/compile cycle (+ ingest/graph when vector on) |
| `make remember M="text"` | write a one-line note |
| `make smoke` | end-to-end smoke test |
| `make logs` | drudge engine logs |
| `make guard` | structural gate (fmt + clippy + test) — same as CI |
| `make deny` | supply-chain gate (cargo-deny) |
| `make down` | stop (keeps `./data`) |
| `make reset` | ⚠️ wipe Postgres data (re-ingested from sources) |

---

## Configuration (env)

Core runs without `.env`; defaults live in `docker-compose.yml`.

| Variable | Default | Purpose |
|---|---|---|
| `DRUDGE_VECTOR` | `off` | `on` enables pgvector (vector + graph RAG); off = wiki-only |
| `DRUDGE_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible LLM server (Ollama · LM Studio · …) |
| `DRUDGE_LLM_API_KEY` | — | only for providers needing auth |
| `DRUDGE_LLM_MODEL` / `DRUDGE_EMBED_MODEL` | `gemma4:12b` / `bge-m3` | synthesis / embedding models |
| `DRUDGE_SOURCE_DIRS` | `~/.claude/projects:vault/wiki` | ingest sources (vector mode) |
| `DISTILL_VIA_AGENT` | — | route the write gate through hermes-agent (else engine distill) |
| `DRUDGE_COMPANY_SUBSTR` / `DISTILL_COMPANY_CWD` | — | tag a path `origin=company` (off by default) |
| `SLACK_APP_TOKEN` / `SLACK_BOT_TOKEN` | — | only for the Slack assistant |

---

## Development · guardrails

- **SSOT docs**: `drudge/{PHILOSOPHY,RUST-STYLE,ENFORCEMENT}.md`.
- **Principles**: ROP (Result rails) · Parse-don't-validate · Clean Architecture · simplest-thing-that-works.
- **Gate** (local `make guard` == CI): `rustfmt --check` + `clippy -D warnings` (`unsafe` forbid + pedantic) + `cargo test` (stack-free). Supply chain: `make deny`.
- **pre-commit**: run `pre-commit install` once (file hygiene + gitleaks + fmt/clippy/test).
- **CI** (`.github/workflows/ci.yml`): every PR/main push runs `rust-gate` + `gitleaks` + `cargo-deny`; branch protection requires all three (admins can't bypass).

---

## Directory

```text
oh-my-boring/
├─ drudge/             # Rust engine (distill·compile·recall·wiki_recall·serve·store·llm)
├─ hooks/              # host hooks (distill-session · recall · collect-sessions)
├─ scripts/            # guard.sh · smoke.sh · eval-gate.sh
├─ vault/              # raw (distilled) → compile → wiki (PRIMARY memory). .rules/ schema
├─ data/               # Postgres persistence (vector mode) — gitignored
├─ docker-compose.yml  # drudge + hermes-agent (+ Postgres via --profile vector)
├─ start.sh            # what make up runs
└─ Makefile            # command entrypoint
```
