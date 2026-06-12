#!/usr/bin/env bash
# 셋업(클론-앤-런): 호스트 Ollama 확인 → 모델 pull → 전체 스택 기동 → health 대기.
#   make up  호출용. 사용은 make ask / make sync / make smoke.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || cp .env.example .env  # Slack 토큰만 — 코어는 .env 없이도 돈다
LLM="${HERMES_LLM_MODEL:-gemma4:12b}"
EMB="${HERMES_EMBED_MODEL:-bge-m3}"
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

echo "▶ 빌드 + 기동 (postgres + hermes-rs + joseph) …"
docker compose up -d --build

echo "▶ hermes-rs health 대기 …"
for _ in $(seq 1 60); do curl -sf -m3 http://localhost:7700/health >/dev/null 2>&1 && break; sleep 3; done

cat <<'EOF'

✓ 셋업 완료. 첫 적재(startup sync)는 백그라운드로 진행된다(수 분).
  make smoke         end-to-end 확인
  make ask Q="..."   질의 1회
  make sync          수동 적재(compile→ingest→extract)
  make logs          엔진 로그
  (Slack 쓰려면 .env 에 토큰 채우고 docker compose up -d joseph)
EOF
