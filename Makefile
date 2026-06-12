.PHONY: help up down build logs ask sync smoke models guard eval psql reset

help: ## 명령 목록
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:.*## / — /' | sort

up: ## 셋업+기동 (Ollama 확인·모델 pull·빌드·전체 기동)
	./start.sh

down: ## 스택 정지 (데이터 ./data 유지)
	docker compose down

build: ## 이미지 빌드
	docker compose build

logs: ## drudge 엔진 로그
	docker compose logs -f drudge

models: ## Ollama 모델 pull (gemma4:12b + bge-m3)
	ollama pull gemma4:12b && ollama pull bge-m3

ask: ## 질의 1회   make ask Q="질문"
	@[ -n "$(Q)" ] || { echo 'usage: make ask Q="질문"'; exit 1; }
	@curl -s -m120 localhost:7700/ask -H 'content-type: application/json' \
	  -d "$$(jq -nc --arg q "$(Q)" '{question:$$q}')" | jq -r '.answer'

sync: ## 수동 적재 (compile→ingest→extract)
	@curl -s -m600 -X POST localhost:7700/sync | jq .

remember: ## 메모 즉시 저장+적재   make remember M="내용"
	@[ -n "$(M)" ] || { echo 'usage: make remember M="내용"'; exit 1; }
	@mkdir -p vault/raw
	@f="vault/raw/memo-$$(date +%Y%m%d-%H%M%S).md"; \
	  printf '# 메모 — %s\n> 사용자 기록 · origin: personal · type: memo\n\n- %s\n' "$$(date '+%Y-%m-%d %H:%M')" "$(M)" > "$$f"; \
	  curl -s -m600 -X POST localhost:7700/sync >/dev/null && echo "✅ 기억함 → $$f"

collect: ## 과거 세션 lazy 수집 (1개씩)   make collect [N=3]
	@COLLECT_LIMIT=$${N:-1} python3 hooks/collect-sessions.py

smoke: ## end-to-end 스모크 테스트
	./scripts/smoke.sh

guard: ## 구조 게이트 (fmt+clippy+test)
	./scripts/guard.sh

eval: ## 행동 회귀 게이트 (run_eval --check, 스택 필요)
	./scripts/eval-gate.sh

psql: ## Postgres 직접 접속 (그래프 node/edge 들여다보기)
	docker compose exec postgres psql -U omb -d omb

reset: ## ⚠️ Postgres 데이터까지 초기화 (소스에서 재적재됨)
	docker compose down
	rm -rf ./data/pgdata
	@echo "DB 초기화 — make up 후 startup sync 가 재적재"
