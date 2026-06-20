.PHONY: help up down build logs agent-logs ask sync remember collect smoke e2e doctor models ollama hermes-build guard deny eval psql reset

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
	@case "$$(printf '%s' "$${DRUDGE_VECTOR:-off}" | tr '[:upper:]' '[:lower:]')" in \
	  on|1|true|yes) $(COMPOSE) --profile vector down ;; \
	  *) $(COMPOSE) down ;; \
	esac

build: ## Build images
	$(COMPOSE) build

logs: ## engine logs
	$(COMPOSE) logs -f drudge

agent-logs: ## hermes-agent logs (MCP connection diagnostics)
	$(COMPOSE) logs -f hermes-agent

models: ## Pull Ollama models (DRUDGE_LLM_MODEL + DRUDGE_EMBED_MODEL, defaults gemma4:12b + bge-m3)
	ollama pull "${DRUDGE_LLM_MODEL:-gemma4:12b}" && ollama pull "${DRUDGE_EMBED_MODEL:-bge-m3}"

ask: ## Single query   make ask Q="question"
	@command -v jq >/dev/null 2>&1 || { echo 'jq not found — install: brew install jq / apt-get install jq'; exit 1; }
	@[ -n "$(Q)" ] || { echo 'usage: make ask Q="question"'; exit 1; }
	@code=$$(curl -s -m120 -o /tmp/omb-ask.$$$$ -w '%{http_code}' "$${DRUDGE_URL:-http://127.0.0.1:7700}/ask" \
	  -H 'content-type: application/json' \
	  -d "$$(jq -nc --arg q "$(Q)" '{question:$$q}')"); \
	  body=$$(cat /tmp/omb-ask.$$$$ 2>/dev/null); rm -f /tmp/omb-ask.$$$$; \
	  echo "$$body" | jq -r '.answer // .error // "ask failed"'; \
	  [ "$$code" = 200 ] || { echo "ask failed: HTTP $$code" >&2; exit 1; }

sync: ## Deterministic re-ingest of the vault (vault/wiki → embed → graph → relates_to)
	@command -v jq >/dev/null 2>&1 || { echo 'jq not found — install: brew install jq / apt-get install jq'; exit 1; }
	@curl -s -m600 -X POST "$${DRUDGE_URL:-http://127.0.0.1:7700}/sync" | jq .

remember: ## Save + ingest a note immediately   make remember M="content" [T="title"]
	@command -v jq >/dev/null 2>&1 || { echo 'jq not found — install: brew install jq / apt-get install jq'; exit 1; }
	@[ -n "$(M)" ] || { echo 'usage: make remember M="content" [T="title"]'; exit 1; }
	@curl -s -m600 -X POST "$${DRUDGE_URL:-http://127.0.0.1:7700}/mcp" -H 'content-type: application/json' \
	  -d "$$(jq -nc --arg t "$${T:-$(M)}" --arg b "$(M)" \
	    '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:"remember",arguments:{title:$$t,body:$$b}}}')" \
	  | jq -r '.result.content[0].text // .error.message'

collect: ## Lazily collect past sessions (one at a time)   make collect [N=1]
	@COLLECT_LIMIT=$${N:-1} python3 agents/schedulers/collect-sessions.py

smoke: ## end-to-end smoke test
	./scripts/smoke.sh

e2e: ## wiki-mode end-to-end (remember→recall round-trip + vector-off reject); skips if stack down
	./scripts/e2e.sh

doctor: ## Diagnose the distill write-door (drudge/Ollama/containers + newest note & hook marker)
	./scripts/doctor.sh

guard: ## Structural gate (fmt+clippy+test+py-compile+py-unit-tests)
	./scripts/guard.sh

deny: ## Supply-chain gate (cargo-deny: vulnerabilities, licenses, duplicate versions)
	cd drudge && cargo deny check

eval: ## Behavioral regression gate (stack needed; runs data/eval/run_eval.py when present)
	./scripts/eval-gate.sh

psql: ## Connect directly to Postgres (requires vector mode / --profile vector)
	$(COMPOSE) exec postgres psql -U boring -d boring

reset: ## ⚠️ Reset including Postgres data (re-ingested from source)
	@printf '⚠️  This deletes ./data/pgdata (the vector DB). vault/ markdown is kept. Continue? [y/N] '; \
	  read ans; [ "$$ans" = y ] || [ "$$ans" = Y ] || { echo "aborted."; exit 1; }
	@case "$$(printf '%s' "$${DRUDGE_VECTOR:-off}" | tr '[:upper:]' '[:lower:]')" in \
	  on|1|true|yes) $(COMPOSE) --profile vector down ;; \
	  *) $(COMPOSE) down ;; \
	esac
	rm -rf ./data/pgdata
	@echo "DB reset — startup sync re-ingests after make up"
