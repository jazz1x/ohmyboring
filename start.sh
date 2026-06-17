#!/usr/bin/env bash
# Setup (clone-and-run): check host Ollama → pull models → start the whole stack → wait for health.
#   Called by make up. Use via make ask / make sync / make smoke.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || cp .env.example .env  # Slack tokens only — the core runs without .env
chmod 600 .env 2>/dev/null || true
# Create policy config from example if missing. User edits it to set language, repo rules, source dirs.
[ -f boring.json ] || cp boring.example.json boring.json
chmod 644 boring.json 2>/dev/null || true
# Source .env so that variables like DRUDGE_VECTOR are visible to this script.
set -a; . .env; set +a
LLM="${DRUDGE_LLM_MODEL:-gemma4:12b}"
EMB="${DRUDGE_EMBED_MODEL:-bge-m3}"
OLLAMA_LOCAL="${OLLAMA_HOST:-http://host.docker.internal:11434}"
OLLAMA_LOCAL="${OLLAMA_LOCAL/host.docker.internal/localhost}"

echo "▶ Ensuring Ollama is running …"
./scripts/ensure-ollama.sh
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
    The optional Slack/agent layer is third-party — build the `hermes-agent` image per its
    official docs (https://hermes-agent.org), point its ~/.hermes/config.yaml at drudge's MCP
    (http://drudge:7700/mcp), then re-run `make up`. See README "Optional: hermes-agent".
    Set OMB_CORE_ONLY=1 to skip this message intentionally.
MSG
  fi
fi

# If DRUDGE_VECTOR=on, also start the pgvector (vector+graph) profile. Default (off) is wiki-first — postgres not started.
PROFILES=""
case "$(printf '%s' "${DRUDGE_VECTOR:-off}" | tr '[:upper:]' '[:lower:]')" in
  on | 1 | true | yes) PROFILES="--profile vector"; echo "▶ vector mode (with pgvector)";;
  *) echo "▶ wiki-first mode (pgvector not started — set DRUDGE_VECTOR=on to use graph+vector)";;
esac

# Ensure sensitive directories are not world-readable before Docker creates them.
mkdir -p vault/raw vault/wiki data/pgdata
chmod 700 vault vault/raw vault/wiki data data/pgdata 2>/dev/null || true

echo "▶ Building + starting (drudge${AGENT:+ + hermes-agent}${PROFILES:+ + postgres}) …"
docker compose $PROFILES up -d --build drudge $AGENT

echo "▶ Waiting for drudge health …"
for _ in $(seq 1 60); do curl -sf -m3 http://127.0.0.1:7700/health >/dev/null 2>&1 && break; sleep 3; done
if ! curl -sf -m3 http://127.0.0.1:7700/health >/dev/null 2>&1; then
  echo "  ✗ drudge did not become healthy within 3 minutes."
  echo "    Run: docker compose logs drudge"
  exit 1
fi

cat <<'EOF'

✓ Setup complete. The first ingest (startup sync) runs in the background (a few minutes).
  make smoke         end-to-end check
  make ask Q="..."   single query
  make sync          deterministic re-ingest of the vault (embed→graph→relates_to)
  make logs          engine logs
  The core self-augmentation loop runs without hermes-agent. If built, hermes-agent can drive
  drudge over MCP (:7700/mcp) for advanced orchestration, recall, and skill creation.
  (To use Slack, fill in tokens in .env and run docker compose up -d hermes-agent)
EOF
