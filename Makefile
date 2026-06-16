.PHONY: help up down build logs agent-logs ask sync smoke models guard deny eval psql reset

help: ## List commands
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:.*## / — /' | sort

up: ## Setup + start (check Ollama, pull models, build, start everything)
	./start.sh

down: ## Stop the whole stack, including Postgres when vector mode was used (keeps ./data)
	@case "$$(printf '%s' "$${DRUDGE_VECTOR:-off}" | tr '[:upper:]' '[:lower:]')" in \
	  on|1|true|yes) docker compose --profile vector down ;; \
	  *) docker compose down ;; \
	esac

build: ## Build images
	docker compose build

logs: ## drudge engine logs
	docker compose logs -f drudge

agent-logs: ## hermes-agent logs (MCP connection diagnostics)
	docker compose logs -f hermes-agent

models: ## Pull Ollama models (gemma4:12b + bge-m3)
	ollama pull gemma4:12b && ollama pull bge-m3

ask: ## Single query   make ask Q="question"
	@[ -n "$(Q)" ] || { echo 'usage: make ask Q="question"'; exit 1; }
	@curl -s -m120 127.0.0.1:7700/ask -H 'content-type: application/json' \
	  -d "$$(jq -nc --arg q "$(Q)" '{question:$$q}')" | jq -r '.answer'

sync: ## Deterministic re-ingest of the vault (vault/wiki → embed → graph → relates_to)
	@curl -s -m600 -X POST 127.0.0.1:7700/sync | jq .

remember: ## Save + ingest a note immediately   make remember M="content" [T="title"]
	@[ -n "$(M)" ] || { echo 'usage: make remember M="content" [T="title"]'; exit 1; }
	@curl -s -m600 -X POST 127.0.0.1:7700/mcp -H 'content-type: application/json' \
	  -d "$$(jq -nc --arg t "$${T:-$(M)}" --arg b "$(M)" \
	    '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:"remember",arguments:{title:$$t,body:$$b}}}')" \
	  | jq -r '.result.content[0].text // .error.message'

collect: ## Lazily collect past sessions (one at a time)   make collect [N=3]
	@COLLECT_LIMIT=$${N:-1} python3 hooks/collect-sessions.py

smoke: ## end-to-end smoke test
	./scripts/smoke.sh

guard: ## Structural gate (fmt+clippy+test)
	./scripts/guard.sh

deny: ## Supply-chain gate (cargo-deny: vulnerabilities, licenses, duplicate versions)
	cd drudge && cargo deny check

eval: ## Behavioral regression gate (run_eval --check, stack needed)
	./scripts/eval-gate.sh

psql: ## Connect directly to Postgres (inspect graph node/edge)
	docker compose exec postgres psql -U boring -d boring

reset: ## ⚠️ Reset including Postgres data (re-ingested from source)
	@printf '⚠️  This deletes ./data/pgdata (the vector DB). vault/ markdown is kept. Continue? [y/N] '; \
	  read ans; [ "$$ans" = y ] || [ "$$ans" = Y ] || { echo "aborted."; exit 1; }
	@case "$$(printf '%s' "$${DRUDGE_VECTOR:-off}" | tr '[:upper:]' '[:lower:]')" in \
	  on|1|true|yes) docker compose --profile vector down ;; \
	  *) docker compose down ;; \
	esac
	rm -rf ./data/pgdata
	@echo "DB reset — startup sync re-ingests after make up"
