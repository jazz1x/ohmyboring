# oh-my-boring

[한국어](README.md) · **[English](README.en.md)** · [日本語](README.ja.md)

[![CI](https://github.com/jazz1x/oh-my-boring/actions/workflows/ci.yml/badge.svg)](https://github.com/jazz1x/oh-my-boring/actions/workflows/ci.yml)
![Rust](https://img.shields.io/badge/engine-Rust%20edition%202024-000?logo=rust)
![Postgres](https://img.shields.io/badge/store-Postgres%2016%20%2B%20pgvector-336791?logo=postgresql&logoColor=white)
![Ollama](https://img.shields.io/badge/LLM-Ollama%20(local)-000)
![cloud](https://img.shields.io/badge/cloud-none-success)

**Self-hosted personal memory RAG.** Your work in Claude Code (or any markdown notes) is automatically distilled into a local vector DB, so you can pull *"how did I do this last time?"* back up on demand. **Zero cloud · 100% local data.**

> The boring chore you keep skipping — remembering past work and digging it back up — is what the **drudge** engine quietly does for you.

```text
sessions·notes ──distill──▶ vault/raw ──compile──▶ vault/wiki ──ingest──▶ pgvector(+graph) ──recall──▶ answer
   ▲ Claude Code                (LLM curation)                 (embed·BM25·CTE)        ▲ make ask / Slack
   └ SessionEnd hook triggers it automatically ──────────────────────────────────────────┘
```

---

## Why

- **Auto-accumulation** — when a session ends, a hook distills it into a "problem-solving narrative" and ingests it. No manual upkeep.
- **Local-only** — embedding and synthesis both run on host Ollama. Zero external APIs/tokens. Notes never leave your disk.
- **Vector + graph** — not just similarity search; it extracts problem/solution/tool/concept nodes and edges (GraphRAG).
- **Optional work/personal split** — one env token tags a path as `origin=company` and isolates it. Everything is `personal` by default.

---

## Layers

| # | Layer | Role | Tech | Exposure | Default in `make up` |
|---|---|---|---|---|:---:|
| 1 | **Ollama** (host) | embedding `bge-m3` (1024d) · synthesis `gemma4:12b` (think=false) | host process | `127.0.0.1:11434` | required[^ollama] |
| 2 | **drudge** (Rust engine) | ingest·retrieve·graph·compile·distill (HTTP + 4h scheduler) | axum / tokio | `127.0.0.1:7700` | ✓ |
| 3 | **Postgres + pgvector** | `knowledge` = vector (HNSW) + BM25 + node/edge recursive-CTE graph | `pgvector/pgvector:pg16` | `127.0.0.1:5432` | ✓ |
| 4 | **hooks** (host, Python) | glue wiring sessions → engine (distill·recall·collect) | `python3` | — | manual install[^hooks] |
| 5 | **hermes-agent** (optional) | Slack assistant + autonomous cron (Socket Mode) | external image | — | ✗ (`--profile agent`)[^agent] |

[^ollama]: `ollama serve` must be running on the host. Containers reach it via `host.docker.internal`.
[^hooks]: Register them yourself in `~/.claude/settings.json` — see [Self-augmentation loop](#self-augmentation-loop).
[^agent]: `hermes-agent` is a third-party image not bundled here (Nous Hermes Agent). Build it separately, then `docker compose --profile agent up -d`.

> The core is **#2 + #3 + host #1**. Add #4 (hooks) and auto-accumulation runs; #5 is purely optional.

---

## Prerequisites

| Install | Purpose | Check |
|---|---|---|
| **Docker** (Compose v2) | container stack | `docker compose version` |
| **Ollama** | local embedding/synthesis | `ollama --version` · [ollama.com](https://ollama.com) or `brew install ollama` |
| **Python 3** | run host hooks | `python3 --version` (ships with macOS) |
| ~10GB disk | two models | `gemma4:12b` (~8GB) + `bge-m3` (~1.2GB) — `make up`/`make models` pulls them |

> **Clone location**: `~/oh-my-boring` is recommended. Hook, `start.sh`, and vault paths assume this location. Put it elsewhere and you must adjust the [hook paths](#self-augmentation-loop).

---

## Quick start

```bash
git clone git@github.com:jazz1x/oh-my-boring.git ~/oh-my-boring
cd ~/oh-my-boring
cp .env.example .env          # leave as-is if you don't use Slack (core runs without .env)
make up                       # check Ollama → pull models → build → start → initial sync
make smoke                    # one end-to-end check
make ask Q="how did I fix the docker build cache problem?"
```

`make up` = `start.sh`: Ollama health check → model pull → `docker compose up -d --build` (postgres + drudge) → wait for `/health`. The first ingest (startup sync) runs in the background for a few minutes.

---

## Self-augmentation loop

It accumulates on its own when a session ends — the core value. Three host hooks are the triggers, and **the heavy work (LLM distill, scrub, write) is done by the engine (`/distill`) as the SSOT**.

```text
① end/stop  →  distill-session.py (SessionEnd/Stop hook)
                extract transcript → POST /distill → engine distills, scrubs secrets, writes vault/raw
② sync      →  compile (raw→wiki curation) → embed → pgvector upsert → graph extract
                [4h scheduler · make sync · right after a session ends]
③ recall    →  make ask / recall.py (auto-injected per prompt) / hermes-agent (Slack)
                vector + BM25 RRF top-K
```

| Hook | Claude Code event | What it does |
|---|---|---|
| `hooks/distill-session.py` | `SessionEnd` / `Stop` | extract session → POST `/distill` → write raw note + fix mtime. Also tags the git-remote repo slug as `repo/<slug>` |
| `hooks/recall.py` | `UserPromptSubmit` | recalls relevant past experience via `/search` and injects it as context |
| `hooks/collect-sessions.py` | cron / `make collect` | backfills sessions missed by SessionEnd (a few at a time) |

**Install the hooks** (persist) — `~/.claude/settings.json`:

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

> The engine (drudge) must be up for distill/recall to work. If it isn't, they silently no-op — **a session is never blocked**.

---

## Sources & recall

- **Ingest targets** (`DRUDGE_SOURCE_DIRS`, compose default): `~/.claude/projects` (Claude Code memory) + `vault/wiki` (distilled/curated notes).
- **Instant note**: `make remember M="bge-m3 embeddings are 1024-dimensional"` → writes raw, then syncs.
- **Recall**: `make ask Q="..."` (one-shot) · `recall.py` (auto per prompt) · Slack (when `hermes-agent` is on).

---

## Work/personal tagging (optional · off by default)

To tag documents under a path as `origin=company` and exclude them from ingest, just set env tokens:

```bash
DRUDGE_COMPANY_SUBSTR=acme:acme-kb    # Rust ingest/origin/audit (path substring)
DISTILL_COMPANY_CWD=acme              # session distill hook (cwd substring)
```

**Zero code changes — env only.** Leave empty and the company concept is off entirely; everything is `personal`.

---

## Command reference

`make help` for the full list. The common ones:

| Command | Description |
|---|---|
| `make up` | set up + start (check Ollama · pull models · build · start) |
| `make ask Q="question"` | one-shot query (recall + LLM synthesis + sources) |
| `make sync` | manual ingest (compile → ingest → extract) |
| `make remember M="text"` | write a one-line note and ingest immediately |
| `make collect [N=3]` | backfill past sessions (N per run) |
| `make smoke` | end-to-end smoke test |
| `make logs` | drudge engine logs |
| `make psql` | connect to Postgres directly (peek at the graph) |
| `make guard` | structural gate (fmt + clippy + test) — same as CI |
| `make down` | stop (keeps data in `./data`) |
| `make reset` | ⚠️ wipe Postgres data too (re-ingested from sources) |

---

## Configuration (env)

The core runs without `.env`. Defaults are baked into the `drudge` environment in `docker-compose.yml`.

| Variable | Default | Purpose |
|---|---|---|
| `SLACK_APP_TOKEN` / `SLACK_BOT_TOKEN` | — | only when enabling `hermes-agent` (Slack) |
| `DRUDGE_LLM_MODEL` | `gemma4:12b` | synthesis model (think=false fixed) |
| `DRUDGE_EMBED_MODEL` | `bge-m3` | embedding (1024d) |
| `DRUDGE_SOURCE_DIRS` | `~/.claude/projects:vault/wiki` | ingest sources (`:`-separated) |
| `DRUDGE_SYNC_HOURS` | `4` | background sync interval |
| `DRUDGE_COMPANY_SUBSTR` / `DISTILL_COMPANY_CWD` | — | work tagging (see above) |

---

## Development · guardrails

- **SSOT docs**: `drudge/{PHILOSOPHY,RUST-STYLE,ENFORCEMENT}.md`.
- **Principles**: ROP (Result rails) · Parse-don't-validate · Clean Architecture · simplest-thing-that-works.
- **Gate** (local `make guard` == CI): `rustfmt --check` + `clippy -D warnings` (`unsafe` forbid + `all`/`pedantic` deny) + `cargo test`. Tests are stack-free (no DB needed).
- **CI** (`.github/workflows/ci.yml`): on every PR and main push, `rust-gate` (guard.sh) + `gitleaks` (secret scan). Branch protection requires both — admins can't bypass; no direct push, force-push, or deletion.

---

## Directory

```text
oh-my-boring/
├─ drudge/             # Rust engine (ingest·retrieve·graph·compile·distill·serve)
│  └─ src/{ingest,retrieve,extract,graph,vault,distill,serve,store,ollama,...}.rs
├─ hooks/              # host hooks (distill-session · recall · collect-sessions)
├─ scripts/            # guard.sh (gate) · smoke.sh · eval-gate.sh
├─ vault/              # raw (distilled) → compile → wiki (curated). ingest target
├─ data/              # Postgres persistence (pgdata) — gitignored
├─ docker-compose.yml  # postgres + drudge (+ --profile agent: hermes-agent)
├─ start.sh            # what make up runs (Ollama·models·build·health)
└─ Makefile            # command entrypoint
```
