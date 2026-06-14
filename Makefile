.PHONY: help up down build logs ask sync smoke models guard deny eval psql reset

help: ## List commands
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:.*## / — /' | sort

up: ## Setup + start (check Ollama, pull models, build, start everything)
	./start.sh

down: ## Stop the stack (keeps data in ./data)
	docker compose down

build: ## Build images
	docker compose build

logs: ## drudge engine logs
	docker compose logs -f drudge

models: ## Pull Ollama models (gemma4:12b + bge-m3)
	ollama pull gemma4:12b && ollama pull bge-m3

ask: ## Single query   make ask Q="question"
	@[ -n "$(Q)" ] || { echo 'usage: make ask Q="question"'; exit 1; }
	@curl -s -m120 localhost:7700/ask -H 'content-type: application/json' \
	  -d "$$(jq -nc --arg q "$(Q)" '{question:$$q}')" | jq -r '.answer'

sync: ## Manual ingest (compile→ingest→extract)
	@curl -s -m600 -X POST localhost:7700/sync | jq .

remember: ## Save + ingest a memo immediately   make remember M="content"
	@[ -n "$(M)" ] || { echo 'usage: make remember M="content"'; exit 1; }
	@mkdir -p vault/raw
	@f="vault/raw/memo-$$(date +%Y%m%d-%H%M%S).md"; \
	  printf '# Memo — %s\n> user record · origin: personal · type: memo\n\n- %s\n' "$$(date '+%Y-%m-%d %H:%M')" "$(M)" > "$$f"; \
	  curl -s -m600 -X POST localhost:7700/sync >/dev/null && echo "✅ Remembered → $$f"

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
	docker compose down
	rm -rf ./data/pgdata
	@echo "DB reset — startup sync re-ingests after make up"
