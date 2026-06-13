#!/usr/bin/env bash
# Setup (clone-and-run): check host Ollama → pull models → start the whole stack → wait for health.
#   Called by make up. Use via make ask / make sync / make smoke.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || cp .env.example .env  # Slack tokens only — the core runs without .env
LLM="${DRUDGE_LLM_MODEL:-gemma4:12b}"
EMB="${DRUDGE_EMBED_MODEL:-bge-m3}"
OLLAMA_LOCAL="${OLLAMA_HOST:-http://host.docker.internal:11434}"
OLLAMA_LOCAL="${OLLAMA_LOCAL/host.docker.internal/localhost}"

echo "▶ Checking Ollama (${OLLAMA_LOCAL}) …"
if ! curl -sf "${OLLAMA_LOCAL}/api/tags" >/dev/null; then
  command -v ollama >/dev/null || { echo "  ✗ ollama not installed → https://ollama.com (or brew install ollama)"; exit 1; }
  echo "  Starting Ollama …"; ollama serve >/tmp/ollama.log 2>&1 &
  until curl -sf "${OLLAMA_LOCAL}/api/tags" >/dev/null; do sleep 1; done
fi
echo "  ✓ Ollama OK"

for m in "$LLM" "$EMB"; do
  if ! curl -sf "${OLLAMA_LOCAL}/api/tags" | grep -q "\"${m%%:*}"; then
    echo "▶ Pulling model: $m (several GB)"; ollama pull "$m"
  fi
done

# hermes-agent (the brain) = default core. But the image isn't in the repo (external build) → check first to avoid cryptic failures.
if ! docker image inspect hermes-agent >/dev/null 2>&1; then
  cat <<'MSG'
  ✗ hermes-agent image missing — this stack's agent (the brain that drives ingestion) needs the external Nous Hermes Agent image.
    1) Get Nous Hermes Agent and build it with `docker build -t hermes-agent .`
    2) Prepare config (credentials · memory) in ~/.hermes
    Then run `make up` again. (To see the core RAG first: docker compose up -d postgres drudge)
MSG
  exit 1
fi

# If DRUDGE_VECTOR=on, also start the pgvector (vector+graph) profile. Default (off) is wiki-first — postgres not started.
PROFILES=""
case "$(printf '%s' "${DRUDGE_VECTOR:-off}" | tr '[:upper:]' '[:lower:]')" in
  on | 1 | true | yes) PROFILES="--profile vector"; echo "▶ vector mode (with pgvector)";;
  *) echo "▶ wiki-first mode (pgvector not started — set DRUDGE_VECTOR=on to use graph+vector)";;
esac

echo "▶ Building + starting (drudge + hermes-agent${PROFILES:+ + postgres}) …"
docker compose $PROFILES up -d --build

echo "▶ Waiting for drudge health …"
for _ in $(seq 1 60); do curl -sf -m3 http://localhost:7700/health >/dev/null 2>&1 && break; sleep 3; done

cat <<'EOF'

✓ Setup complete. The first ingest (startup sync) runs in the background (a few minutes).
  make smoke         end-to-end check
  make ask Q="..."   single query
  make sync          manual ingest (compile→ingest→extract)
  make logs          engine logs
  The agent (hermes-agent) drives drudge over MCP (:7700/mcp) for ingestion, recall, and skill creation.
  (To use Slack, fill in tokens in .env and run docker compose up -d hermes-agent)
EOF
