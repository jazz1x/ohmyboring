# Session note — 2026-01-01
> Auto-distilled (Claude Code · ended) · origin: personal · repo: jazz1x/oh-my-boring · cwd: ~/oh-my-boring

> **This is an example note.** It shows the format of the distill notes that accumulate in
> `vault/raw/`, and seeds one thing for `make ask` to answer on a fresh clone. Real notes are
> created automatically by the SessionEnd hook.
> Safe to delete — it disappears from the wiki on the next sync.

🎯 **Problem worked on** — tried to spin up oh-my-boring for the first time and verify it works.

🧪 **Attempts/failures** — running just `docker compose up` brought up hermes-agent (the Slack
assistant) too and failed. That image isn't in the repo (external Nous Hermes Agent).

✅ **What worked** — bring up the core with `make up` only (postgres + drudge). The Slack assistant
is optional, so start it separately with `docker compose --profile agent up -d`. The host Ollama
(`ollama serve`) must be up first so the container can call embeddings/synthesis via `host.docker.internal`.

🔄 **Unfinished/next** — accumulate more sessions to see `make ask` recall quality. Once you register
the SessionEnd/UserPromptSubmit hooks in `~/.claude/settings.json`, it accumulates automatically from then on.
