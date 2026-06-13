#!/usr/bin/env bash
# 셋업(클론-앤-런): 호스트 Ollama 확인 → 모델 pull → 전체 스택 기동 → health 대기.
#   make up  호출용. 사용은 make ask / make sync / make smoke.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || cp .env.example .env  # Slack 토큰만 — 코어는 .env 없이도 돈다
LLM="${DRUDGE_LLM_MODEL:-gemma4:12b}"
EMB="${DRUDGE_EMBED_MODEL:-bge-m3}"
OLLAMA_LOCAL="${OLLAMA_HOST:-http://host.docker.internal:11434}"
OLLAMA_LOCAL="${OLLAMA_LOCAL/host.docker.internal/localhost}"

echo "▶ Ollama 확인 (${OLLAMA_LOCAL}) …"
if ! curl -sf "${OLLAMA_LOCAL}/api/tags" >/dev/null; then
  command -v ollama >/dev/null || { echo "  ✗ ollama 미설치 → https://ollama.com (또는 brew install ollama)"; exit 1; }
  echo "  Ollama 기동 …"; ollama serve >/tmp/ollama.log 2>&1 &
  until curl -sf "${OLLAMA_LOCAL}/api/tags" >/dev/null; do sleep 1; done
fi
echo "  ✓ Ollama OK"

for m in "$LLM" "$EMB"; do
  if ! curl -sf "${OLLAMA_LOCAL}/api/tags" | grep -q "\"${m%%:*}"; then
    echo "▶ 모델 pull: $m (수 GB)"; ollama pull "$m"
  fi
done

# hermes-agent(뇌) = 기본 코어. 단 이미지는 레포 미포함(외부 빌드) → 선확인해 cryptic 실패 방지.
if ! docker image inspect hermes-agent >/dev/null 2>&1; then
  cat <<'MSG'
  ✗ hermes-agent 이미지 없음 — 이 스택의 에이전트(적재를 모는 뇌)는 외부 Nous Hermes Agent 이미지가 필요하다.
    1) Nous Hermes Agent 를 받아 `docker build -t hermes-agent .` 로 빌드
    2) ~/.hermes 에 config(자격증명·메모리) 준비
    그 뒤 다시 `make up`. (코어 RAG만 먼저 보려면: docker compose up -d postgres drudge)
MSG
  exit 1
fi

# DRUDGE_VECTOR=on 이면 pgvector(vector+graph) 프로필 동반 기동. 기본(off)은 wiki 1급 — postgres 미기동.
PROFILES=""
case "$(printf '%s' "${DRUDGE_VECTOR:-off}" | tr '[:upper:]' '[:lower:]')" in
  on | 1 | true | yes) PROFILES="--profile vector"; echo "▶ vector 모드 (pgvector 동반)";;
  *) echo "▶ wiki 1급 모드 (pgvector 미기동 — graph+vector 쓰려면 DRUDGE_VECTOR=on)";;
esac

echo "▶ 빌드 + 기동 (drudge + hermes-agent${PROFILES:+ + postgres}) …"
docker compose $PROFILES up -d --build

echo "▶ drudge health 대기 …"
for _ in $(seq 1 60); do curl -sf -m3 http://localhost:7700/health >/dev/null 2>&1 && break; sleep 3; done

cat <<'EOF'

✓ 셋업 완료. 첫 적재(startup sync)는 백그라운드로 진행된다(수 분).
  make smoke         end-to-end 확인
  make ask Q="..."   질의 1회
  make sync          수동 적재(compile→ingest→extract)
  make logs          엔진 로그
  에이전트(hermes-agent)가 MCP(:7700/mcp)로 drudge 를 물어 적재·회수·스킬생성을 몬다.
  (Slack 쓰려면 .env 에 토큰 채우고 docker compose up -d hermes-agent)
EOF
