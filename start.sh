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
# Source .env so that variables like BORING_VECTOR are visible to this script.
set -a; . .env; set +a

# Fail fast if a required host tool is missing — BEFORE pulling GBs of models or
# starting docker compose, so the user gets a clear hint instead of a cryptic exit 127.
./scripts/preflight-deps.sh

# --- LLM provider bootstrap (provider-dispatch; SSOT = boring.json `llm` block) ---
# The engine is OpenAI-compatible & backend-agnostic, so only the *bootstrap* differs per provider:
# Ollama pulls models, LM Studio expects them loaded in-app, a remote endpoint needs nothing. We read
# the connection from boring.json (jq = a verified hard dep) and let runtime env override it
# (BORING_LLM_* env override). embed_model is
# the engine's policy SSOT (boring.json only, never env), so the pull always targets the configured one.
PROVIDER=$(jq -r '.llm.provider // "ollama"' boring.json 2>/dev/null || echo ollama)
BOOTSTRAP=$(jq -r '.llm.bootstrap // "auto"' boring.json 2>/dev/null || echo auto)
CFG_BASE=$(jq -r '.llm.base_url // "http://host.docker.internal:11434/v1"' boring.json 2>/dev/null || echo "http://host.docker.internal:11434/v1")
CFG_CHAT=$(jq -r '.llm.model // "gemma4:12b"' boring.json 2>/dev/null || echo gemma4:12b)
CFG_EMB=$(jq -r '.llm.embed_model // .embed_model // "bge-m3"' boring.json 2>/dev/null || echo bge-m3)

LLM_URL="${BORING_LLM_BASE_URL:-$CFG_BASE}"
LLM="${BORING_LLM_MODEL:-$CFG_CHAT}"
EMB="$CFG_EMB"

PROVIDER_SCRIPT="./scripts/llm-providers/${PROVIDER}.sh"
if [ -x "$PROVIDER_SCRIPT" ]; then
  echo "▶ LLM provider: ${PROVIDER} (bootstrap=${BOOTSTRAP}) → ${LLM_URL}"
  "$PROVIDER_SCRIPT" "$LLM_URL" "$LLM" "$EMB" "$BOOTSTRAP"
else
  echo "ⓘ Unknown LLM provider '${PROVIDER}' (no ${PROVIDER_SCRIPT}) — skipping bootstrap. Ensure ${LLM_URL} is reachable."
fi

# hermes-agent (the brain) is part of the default stack, but its image isn't in this repo
# (external Nous Hermes Agent build). If it's missing we fall back to CORE-ONLY (ohmyboring,
# the RAG engine) so a first-timer can try `make ask` immediately — set BORING_CORE_ONLY=1 to
# skip the agent on purpose.
# Compose SERVICE name = boring-agent; the IMAGE it runs is the external `hermes-agent` build.
AGENT="boring-agent"
if [ -n "${BORING_CORE_ONLY:-}" ] || ! docker image inspect hermes-agent >/dev/null 2>&1; then
  AGENT=""
  if [ -z "${BORING_CORE_ONLY:-}" ]; then
    cat <<'MSG'
  ⓘ hermes-agent image not found — starting CORE ONLY (ohmyboring RAG engine). `make ask` works.
    The optional Slack/agent layer is third-party — build the `hermes-agent` image per its
    official docs (https://hermes-agent.org), point its ~/.hermes/config.yaml at ohmyboring's MCP
    (http://boring-drudge:7700/mcp), then re-run `make up`. See README "Optional: hermes-agent".
    Set BORING_CORE_ONLY=1 to skip this message intentionally.
MSG
  fi
fi

# If BORING_VECTOR=on, also start the pgvector (vector+graph) profile.
# Default (off) is wiki-first — postgres not started.
PROFILES=""
case "$(printf '%s' "${BORING_VECTOR:-off}" | tr '[:upper:]' '[:lower:]')" in
  on | 1 | true | yes) PROFILES="--profile vector"; echo "▶ vector mode (with pgvector)";;
  *) echo "▶ wiki-first mode (pgvector not started — set BORING_VECTOR=on to use graph+vector)";;
esac

# Ensure sensitive directories are not world-readable before Docker creates them.
mkdir -p vault/raw vault/wiki data/pgdata
chmod 700 vault vault/raw vault/wiki data data/pgdata 2>/dev/null || true

echo "▶ Building + starting (boring-drudge${AGENT:+ + boring-agent}${PROFILES:+ + boring-postgres}) …"
# Some Docker Desktop installs have a broken `docker compose` plugin while the
# standalone `docker-compose` binary works. Fall back transparently.
if docker compose version 2>&1 | grep -q "Docker Compose"; then
  COMPOSE="docker compose"
else
  COMPOSE="docker-compose"
fi
$COMPOSE $PROFILES up -d --build boring-drudge $AGENT

echo "▶ Waiting for boring-drudge health …"
for _ in $(seq 1 60); do curl -sf -m3 http://127.0.0.1:7700/health >/dev/null 2>&1 && break; sleep 3; done
if ! curl -sf -m3 http://127.0.0.1:7700/health >/dev/null 2>&1; then
  echo "  ✗ drudge did not become healthy within 3 minutes."
  echo "    Run: docker compose logs drudge"
  exit 1
fi

cat <<'EOF'

✓ Setup complete. The first ingest (startup sync) runs in the BACKGROUND (a few minutes) — early
  queries may return little until it finishes. Watch it complete:
    curl -s localhost:7700/health   # sync:"running"→"idle", corpus_count climbs
  make smoke         end-to-end check
  make ask Q="..."   single query
  make sync          deterministic re-ingest of the vault (embed→graph→relates_to)
  make logs          engine logs
  The core self-augmentation loop runs without hermes-agent. If built, hermes-agent can drive
  ohmyboring over MCP (:7700/mcp) for advanced orchestration, recall, and skill creation.
  (To use Slack, fill in tokens in .env and run docker compose up -d hermes-agent)
EOF
