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
  # Match the exact tag (Ollama implies :latest for a bare name). Matching only the family
  # prefix would wrongly skip the pull when a different tag of the same family is present.
  case "$m" in *:*) want="$m" ;; *) want="$m:latest" ;; esac
  if ! curl -sf "${OLLAMA_LOCAL}/api/tags" | grep -q "\"${want}\""; then
    echo "▶ Pulling model: $m (several GB)"; ollama pull "$m"
  fi
done

# hermes-agent (the brain) is part of the default stack, but its image isn't in this repo
# (external Nous Hermes Agent build). If it's missing we fall back to CORE-ONLY (drudge,
# the RAG engine) so a first-timer can try `make ask` immediately — set OMB_CORE_ONLY=1 to
# skip the agent on purpose.
AGENT="hermes-agent"
if [ -n "${OMB_CORE_ONLY:-}" ] || ! docker image inspect hermes-agent >/dev/null 2>&1; then
  AGENT=""
  if [ -z "${OMB_CORE_ONLY:-}" ]; then
    cat <<'MSG'
  ⓘ hermes-agent image not found — starting CORE ONLY (drudge RAG engine). `make ask` works.
    To enable the autonomous agent later: build the external Nous Hermes Agent
    (`docker build -t hermes-agent .`), prepare ~/.hermes config, then `make up` again.
MSG
  fi
fi

# If DRUDGE_VECTOR=on, also start the pgvector (vector+graph) profile. Default (off) is wiki-first — postgres not started.
PROFILES=""
case "$(printf '%s' "${DRUDGE_VECTOR:-off}" | tr '[:upper:]' '[:lower:]')" in
  on | 1 | true | yes) PROFILES="--profile vector"; echo "▶ vector mode (with pgvector)";;
  *) echo "▶ wiki-first mode (pgvector not started — set DRUDGE_VECTOR=on to use graph+vector)";;
esac

echo "▶ Building + starting (drudge${AGENT:+ + hermes-agent}${PROFILES:+ + postgres}) …"
docker compose $PROFILES up -d --build drudge $AGENT

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
