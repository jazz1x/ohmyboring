.PHONY: help up down build logs agent-logs events ask sync remember collect distill-now collect-kimi smoke e2e doctor readiness heal verify-llm maintenance maintenance-install maintenance-uninstall maintenance-status steward steward-fix vault-cleanup-check vault-cleanup-fix retention retention-apply backup-db restore-db compact models ollama hermes-build guard quality test-db test-db-upgrade self-verify-check deny eval bench-llm psql reset

# Some Docker Desktop installs have a broken `docker compose` plugin while the
# standalone `docker-compose` binary works. Fall back transparently.
COMPOSE := $(shell if docker compose version 2>&1 | grep -q "Docker Compose"; then echo "docker compose"; else echo "docker-compose"; fi)

help: ## List commands
	@grep -E '^[a-z0-9-]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:.*## / — /' | sort

up: ## Setup + start (check Ollama, pull models, build, start everything)
	./start.sh

ollama: ## Ensure Ollama is running (start it in the background if possible)
	./scripts/ensure-ollama.sh

hermes-build: ## Clone/build the optional hermes-agent image
	@if [ -d "$(HOME)/hermes-agent-src" ]; then \
		echo "ⓘ hermes-agent source already exists at $(HOME)/hermes-agent-src"; \
	else \
		git clone https://github.com/NousResearch/hermes-agent.git "$(HOME)/hermes-agent-src"; \
	fi
	cd "$(HOME)/hermes-agent-src" && docker build -t hermes-agent .

down: ## Stop the whole stack, including Postgres when vector mode was used (keeps ./data)
	@case "$$(printf '%s' "$${BORING_VECTOR:-off}" | tr '[:upper:]' '[:lower:]')" in \
	  on|1|true|yes) $(COMPOSE) --profile vector down ;; \
	  *) $(COMPOSE) down ;; \
	esac

build: ## Build images (stamps the image with the current commit; see /health build_sha)
	BUILD_SHA=$$(git rev-parse HEAD 2>/dev/null || true) $(COMPOSE) build

logs: ## engine logs
	$(COMPOSE) logs -f boring-drudge

agent-logs: ## boring-agent (hermes) logs (MCP connection diagnostics)
	$(COMPOSE) logs -f boring-agent

events: ## Show recent workflow events (engine DB first, file fallback)
	@python3 agents/shared/event_log.py --tail --max "$${N:-20}"

models: ## Pull Ollama models (DRUDGE_LLM_MODEL + DRUDGE_EMBED_MODEL, defaults gemma4:12b + bge-m3)
	ollama pull "${DRUDGE_LLM_MODEL:-gemma4:12b}" && ollama pull "${DRUDGE_EMBED_MODEL:-bge-m3}"

verify-llm: ## Verify boring.json LLM config (reachability, model presence, embed_dim)
	./scripts/verify-llm.sh

ask: ## Single query   make ask Q="question"
	@command -v jq >/dev/null 2>&1 || { echo 'jq not found — install: brew install jq / apt-get install jq'; exit 1; }
	@[ -n "$(Q)" ] || { echo 'usage: make ask Q="question"'; exit 1; }
	@code=$$(curl -s -m120 -o /tmp/omb-ask.$$$$ -w '%{http_code}' "$${BORING_URL:-http://127.0.0.1:7700}/ask" \
	  -H 'content-type: application/json' \
	  -d "$$(jq -nc --arg q "$(Q)" '{question:$$q}')"); \
	  jq -r '.answer // .error // "ask failed"' /tmp/omb-ask.$$$$ 2>/dev/null || cat /tmp/omb-ask.$$$$; rm -f /tmp/omb-ask.$$$$; \
	  [ "$$code" = 200 ] || { echo "ask failed: HTTP $$code" >&2; exit 1; }

sync: ## Deterministic re-ingest of the vault (vault/wiki → embed → graph → relates_to)
	@command -v jq >/dev/null 2>&1 || { echo 'jq not found — install: brew install jq / apt-get install jq'; exit 1; }
	@curl -s -m600 -X POST "$${BORING_URL:-http://127.0.0.1:7700}/sync" | jq .

remember: ## Save + ingest a note immediately   make remember M="content" [T="title"]
	@command -v jq >/dev/null 2>&1 || { echo 'jq not found — install: brew install jq / apt-get install jq'; exit 1; }
	@[ -n "$(M)" ] || { echo 'usage: make remember M="content" [T="title"]'; exit 1; }
	@curl -s -m600 -X POST "$${BORING_URL:-http://127.0.0.1:7700}/mcp" -H 'content-type: application/json' \
	  -d "$$(jq -nc --arg t "$${T:-$(M)}" --arg b "$(M)" \
	    '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:"remember",arguments:{title:$$t,body:$$b}}}')" \
	  | jq -r '.result.content[0].text // .error.message'

collect: ## Lazily collect past Claude Code sessions (one at a time)   make collect [N=1]
	@COLLECT_LIMIT=$${N:-1} python3 agents/schedulers/collect-sessions.py

distill-now: ## Distill the CURRENT session right now (no need to end it; re-runnable)   make distill-now
	@python3 agents/schedulers/collect-sessions.py --now

collect-kimi: ## Lazily collect past Kimi Code sessions (one at a time)   make collect-kimi [N=1]
	@COLLECT_LIMIT=$${N:-1} python3 agents/schedulers/collect-kimi-sessions.py

smoke: ## end-to-end smoke test
	./scripts/smoke.sh

e2e: ## wiki-mode end-to-end (remember→recall round-trip + vector-off reject); skips if stack down
	./scripts/e2e.sh

doctor: ## Diagnose the write-door (stack, hooks, newest note, Codex worker/queue)
	./scripts/doctor.sh

readiness: ## Strict briefing/readiness gate (fails on any doctor finding)
	./scripts/doctor.sh --strict

heal: ## Auto-fix common doctor findings (env perms, hooks, engine, Ollama, containers)
	./scripts/doctor.sh --fix

maintenance: ## Run unattended housekeeping now (data-steward + retention)
	./scripts/schedule-maintenance.sh run

maintenance-install: ## Register daily housekeeping (macOS launchd / Linux cron)
	./scripts/schedule-maintenance.sh install

maintenance-uninstall: ## Remove daily housekeeping registration
	./scripts/schedule-maintenance.sh uninstall

maintenance-status: ## Show daily housekeeping registration state
	./scripts/schedule-maintenance.sh status

steward: ## Inspect vault data hygiene (project variants, placeholder tags, missing sources)
	@python3 scripts/data-steward.py

steward-fix: ## Apply safe data-steward repairs through the backup-first cleanup gate
	@python3 scripts/vault-cleanup-gate.py --fix --vault "$${BORING_VAULT_DIR:-$(PWD)/vault}"

vault-cleanup-check: ## Verify vault cleanup contract without rewriting notes
	@python3 scripts/vault-cleanup-gate.py --check --vault "$${BORING_VAULT_DIR:-$(PWD)/vault}"

vault-cleanup-fix: ## Backup vault/wiki, apply safe steward repairs, then verify
	@python3 scripts/vault-cleanup-gate.py --fix --vault "$${BORING_VAULT_DIR:-$(PWD)/vault}"

retention: ## Show raw session retention plan (dry-run)
	@python3 scripts/retention.py

retention-apply: ## Apply raw session retention (archive old transcripts, delete ancient archives)
	@python3 scripts/retention.py --apply

guard: ## Structural gate (fmt+clippy+test+py-compile+py-unit-tests) + vault data hygiene dry-run
	./scripts/guard.sh
	@echo "7) data-steward dry-run …"
	@python3 scripts/data-steward.py --vault "$(PWD)/vault"

quality: ## Release acceptance gate (MCP contract + docs drift + removed dangerous surface)
	cd drudge && cargo test --quiet quality_gate

test-db: ## Run every drudge/tests/*.rs suite against a disposable local pgvector container. Needs docker; the container is always removed on exit.
	@command -v docker >/dev/null 2>&1 || { echo 'docker not found — required for make test-db (spins up a disposable pgvector container)'; exit 1; }
	@name="drudge-test-db-$$$$"; \
	trap 'docker rm -f "$$name" >/dev/null 2>&1 || true' EXIT INT TERM; \
	echo "▶ starting disposable pgvector container ($$name) …"; \
	docker run -d --name "$$name" -p 127.0.0.1::5432 \
	  -e POSTGRES_USER=boring -e POSTGRES_PASSWORD=boring -e POSTGRES_DB=boring \
	  pgvector/pgvector:pg16 >/dev/null || { echo "failed to start $$name"; exit 1; }; \
	ready=0; streak=0; \
	for _ in $$(seq 1 60); do \
	  if docker exec "$$name" psql -U boring -d boring -tAc 'SELECT 1' >/dev/null 2>&1; then \
	    streak=$$((streak + 1)); [ "$$streak" -ge 3 ] && { ready=1; break; }; \
	  else streak=0; fi; \
	  sleep 1; \
	done; \
	[ "$$ready" = 1 ] || { echo "pgvector container never became ready"; exit 1; }; \
	port="$$(docker port "$$name" 5432/tcp | cut -d: -f2)"; \
	export BORING_TEST_DATABASE_URL="postgresql://boring:boring@127.0.0.1:$$port/boring"; \
	echo "▶ running DB gate suites against $$BORING_TEST_DATABASE_URL …"; \
	fail=0; \
	for suite in $$(ls drudge/tests/*.rs | xargs -n1 basename | sed 's/\.rs$$//'); do \
	  echo "── $$suite ──"; \
	  (cd drudge && cargo test -p drudge --test "$$suite" -- --test-threads=1) || { echo "FAILED: $$suite"; fail=1; }; \
	done; \
	exit "$$fail"

test-db-upgrade: ## Schema parity — a DB upgraded from data/schema/baseline.sql must equal a fresh one
	@sh scripts/test_schema_parity.sh

self-verify-check: ## Check self-verification stage contract: make self-verify-check [STAGE=bootstrap] [SUMMARY=/path/summary.tsv]
	@python3 scripts/self-verify-contract.py --stage "$${STAGE:-bootstrap}" $${SUMMARY:+--summary "$$SUMMARY"}

deny: ## Supply-chain gate (cargo-deny: vulnerabilities, licenses, duplicate versions)
	cd drudge && cargo deny check

eval: ## Behavioral regression gate (stack needed; runs data/eval/run_eval.py when present)
	./scripts/eval-gate.sh

bench-llm: ## Compare LLM distillation quality (default tier: 16gb)
	@python3 scripts/bench-llm.py --tier 16gb

bench-llm-tier: ## Compare LLM distillation quality by RAM tier: make bench-llm-tier TIER=32gb
	@python3 scripts/bench-llm.py --tier "${TIER:-16gb}"

bench-embed: ## Benchmark local embedding model (dim, latency, sanity)
	@python3 scripts/bench-embed.py

anchor-shadow: ## Anchor hit rate: how many code edits revisit a file the vault already knows (PRD §8 Q8)
	@python3 scripts/anchor-shadow.py --days $(or $(DAYS),30)

usage: ## Token and model usage read from local transcripts (make usage GROUP=day,model SINCE=2026-08-26)
	@python3 scripts/usage.py --group "$(or $(GROUP),model)" $(if $(SINCE),--since $(SINCE),) $(if $(UNTIL),--until $(UNTIL),)

peek: ## Local read-only view of what gets injected and whether it lands (127.0.0.1:7788)
	@python3 scripts/peek.py

psql: ## Connect directly to Postgres (requires vector mode / --profile vector)
	$(COMPOSE) exec boring-postgres psql -U boring -d boring

backup-db: ## Backup pgvector DB to data/backups/ (custom format; keeps latest 7)
	@mkdir -p data/backups
	@./scripts/backup-db.sh

restore-db: ## Restore pgvector DB from latest backup (interactive; stop drudge, drop/recreate DB)
	@./scripts/restore-db.sh

compact: ## Run VACUUM/REINDEX/prune/orphan-GC against the pgvector DB
	cd drudge && BORING_VECTOR=on cargo run -- compact

reset: ## ⚠️ Reset including Postgres data (re-ingested from source)
	@printf '⚠️  This deletes ./data/pgdata (the vector DB). vault/ markdown is kept. Continue? [y/N] '; \
	  read ans; [ "$$ans" = y ] || [ "$$ans" = Y ] || { echo "aborted."; exit 1; }
	@case "$$(printf '%s' "$${BORING_VECTOR:-off}" | tr '[:upper:]' '[:lower:]')" in \
	  on|1|true|yes) $(COMPOSE) --profile vector down ;; \
	  *) $(COMPOSE) down ;; \
	esac
	rm -rf ./data/pgdata
	@echo "DB reset — startup sync re-ingests after make up"
