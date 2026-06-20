# Session note — 2026-01-01
> Auto-distilled (Claude Code · ended) · origin: personal · repo: jazz1x/ohmyboring · cwd: ~/oh-my-boring

> **This is an example note.** It shows the format of the distill notes that accumulate in
> `vault/raw/`, and seeds one thing for `make ask` to answer on a fresh clone. Real notes are
> created automatically by the SessionEnd hook.
> Safe to delete — it disappears from the wiki on the next sync.

🎯 **Problem worked on** — tried to spin up ohmyboring for the first time and verify it works.

🧪 **Attempts/failures** — expected `make up` to need the external hermes-agent image (Nous Hermes
Agent, not in the repo). It doesn't hard-fail: when that image is missing, start.sh falls back to
core-only.

✅ **What worked** — `make up` runs the core wiki-first (ohmyboring RAG engine; hermes-agent joins
automatically if its image exists, else core-only). pgvector (vector + graph RAG) is opt-in:
`DRUDGE_VECTOR=on make up` brings up Postgres via `--profile vector`. The host Ollama
(`ollama serve`) must be up first so the container reaches embeddings/synthesis via `host.docker.internal`.

🔄 **Unfinished/next** — accumulate more sessions to see `make ask` recall quality. Once you register
the SessionEnd/UserPromptSubmit hooks in `~/.claude/settings.json`, it accumulates automatically from then on.
