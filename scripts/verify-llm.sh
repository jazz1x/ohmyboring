#!/bin/sh
# Verify LLM configuration before `make up` or after editing boring.json.
# Catches: missing provider script, unreachable endpoint, missing models,
# embed_dim mismatch, missing API key env. Exits non-zero on any hard failure.
set -u

BORING_HOME="${BORING_HOME:-$HOME/oh-my-boring}"
BORING="${BORING_CONFIG:-$BORING_HOME/boring.json}"

ok()   { echo "✓ $1"; }
bad()  { echo "✗ $1"; }
info() { echo "ⓘ $1"; }

if ! command -v jq >/dev/null 2>&1; then
    bad "jq not found — install: brew install jq / apt-get install jq"
    exit 1
fi

if [ ! -f "$BORING" ]; then
    bad "config not found: $BORING"
    exit 1
fi

# Read policy SSOT from boring.json. Env overrides runtime URL/model only.
PROVIDER=$(jq -r '.llm.provider // "ollama"' "$BORING")
BASE_URL=$(jq -r '.llm.base_url // "http://host.docker.internal:11434/v1"' "$BORING")
MODEL=$(jq -r '.llm.model // "gemma4:12b"' "$BORING")
EMBED_MODEL=$(jq -r '.llm.embed_model // .embed_model // "bge-m3"' "$BORING")
EMBED_DIM=$(jq -r '.llm.embed_dim // .embed_dim // "1024"' "$BORING")
API_KEY_ENV=$(jq -r '.llm.api_key_env // empty' "$BORING")

BASE_URL="${BORING_LLM_BASE_URL:-$BASE_URL}"
MODEL="${BORING_LLM_MODEL:-$MODEL}"

# host.docker.internal does not resolve from the host; rewrite to localhost for verification.
HOST_URL=$(printf '%s' "$BASE_URL" | sed 's#host\.docker\.internal#localhost#')

errors=0

info "provider=$PROVIDER model=$MODEL embed_model=$EMBED_MODEL embed_dim=$EMBED_DIM"
echo

# (1) Provider bootstrap script exists.
PROVIDER_SCRIPT="$BORING_HOME/scripts/llm-providers/${PROVIDER}.sh"
if [ -x "$PROVIDER_SCRIPT" ]; then
    ok "provider script found: $PROVIDER_SCRIPT"
else
    bad "provider script missing: $PROVIDER_SCRIPT"
    errors=$((errors + 1))
fi

# (2) Reachability + model availability, provider-aware.
case "$PROVIDER" in
    ollama)
        NATIVE="${HOST_URL%/v1}"; NATIVE="${NATIVE%/}"
        tags=$(curl -sf -m5 "$NATIVE/api/tags" 2>/dev/null)
        if [ -n "$tags" ]; then
            ok "Ollama reachable at $NATIVE"
            if printf '%s' "$tags" | jq -e '.models[]? | select(.name == "'"$MODEL"'")' >/dev/null 2>&1; then
                ok "chat model '$MODEL' is available"
            else
                bad "chat model '$MODEL' not found — run: ollama pull $MODEL"
                errors=$((errors + 1))
            fi
            if printf '%s' "$tags" | jq -e '.models[]? | select(.name == "'"$EMBED_MODEL"'")' >/dev/null 2>&1; then
                ok "embed model '$EMBED_MODEL' is available"
            else
                bad "embed model '$EMBED_MODEL' not found — run: ollama pull $EMBED_MODEL"
                errors=$((errors + 1))
            fi
        else
            bad "Ollama unreachable at $NATIVE — start: ollama serve"
            errors=$((errors + 1))
        fi
        ;;
    lmstudio|openai-compatible)
        code=$(curl -s -o /dev/null -w '%{http_code}' -m5 "$HOST_URL/models" 2>/dev/null)
        case "$code" in
            200)
                ok "endpoint reachable at $HOST_URL/models"
                models=$(curl -sf -m5 "$HOST_URL/models" 2>/dev/null)
                if printf '%s' "$models" | jq -e '.data[]? | select(.id == "'"$MODEL"'")' >/dev/null 2>&1; then
                    ok "chat model '$MODEL' is available"
                else
                    bad "chat model '$MODEL' not found at $HOST_URL — load the model in the server"
                    errors=$((errors + 1))
                fi
                if printf '%s' "$models" | jq -e '.data[]? | select(.id == "'"$EMBED_MODEL"'")' >/dev/null 2>&1; then
                    ok "embed model '$EMBED_MODEL' is available"
                else
                    bad "embed model '$EMBED_MODEL' not found at $HOST_URL — load the model in the server"
                    errors=$((errors + 1))
                fi
                ;;
            401)
                ok "endpoint reachable at $HOST_URL/models (auth required)"
                info "model listing skipped because endpoint requires authentication"
                ;;
            000)
                bad "endpoint unreachable at $HOST_URL/models — is the server running?"
                errors=$((errors + 1))
                ;;
            *)
                bad "endpoint returned HTTP $code at $HOST_URL/models"
                errors=$((errors + 1))
                ;;
        esac
        if [ -n "$API_KEY_ENV" ]; then
            if eval "[ -n \"\$$API_KEY_ENV\" ]"; then
                ok "api key env '$API_KEY_ENV' is set"
            else
                bad "api key env '$API_KEY_ENV' is not set — export $API_KEY_ENV=..."
                errors=$((errors + 1))
            fi
        fi
        ;;
    *)
        info "unknown provider '$PROVIDER' — skipping reachability/model checks"
        ;;
esac

# (3) embed_dim sanity check against known models.
if [ "$EMBED_MODEL" = "bge-m3" ]; then
    KNOWN_DIM=1024
elif [ "$EMBED_MODEL" = "nomic-embed-text" ]; then
    KNOWN_DIM=768
elif [ "$EMBED_MODEL" = "text-embedding-3-small" ]; then
    KNOWN_DIM=1536
else
    KNOWN_DIM=unknown
fi

if [ "$KNOWN_DIM" != "unknown" ]; then
    if [ "$KNOWN_DIM" = "$EMBED_DIM" ]; then
        ok "embed_dim matches $EMBED_MODEL ($KNOWN_DIM)"
    else
        bad "embed_dim mismatch: $EMBED_MODEL expects $KNOWN_DIM, but boring.json has $EMBED_DIM — update llm.embed_dim and run 'make reset'"
        errors=$((errors + 1))
    fi
else
    info "unknown embed model '$EMBED_MODEL' — verify llm.embed_dim manually"
fi

echo
if [ "$errors" -eq 0 ]; then
    ok "verify-llm: configuration looks consistent."
    exit 0
fi
bad "verify-llm: $errors issue(s) found — fix before running make up / make sync"
exit 1
