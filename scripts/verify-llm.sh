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
MODEL=$(jq -r '.llm.model // "qwen3:14b"' "$BORING")
EMBED_MODEL=$(jq -r '.llm.embed_model // .embed_model // "bge-m3"' "$BORING")
EMBED_DIM=$(jq -r '.llm.embed_dim // .embed_dim // "1024"' "$BORING")
API_KEY_ENV=$(jq -r '.llm.api_key_env // empty' "$BORING")

BASE_URL="${BORING_LLM_BASE_URL:-$BASE_URL}"
MODEL="${BORING_LLM_MODEL:-$MODEL}"

# host.docker.internal does not resolve from the host; rewrite to localhost for verification.
HOST_URL=$(printf '%s' "$BASE_URL" | sed 's#host\.docker\.internal#localhost#')

errors=0
probe_embeddings=0

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
            if printf '%s' "$tags" | jq -e --arg m "$MODEL" '.models[]? | select(.name == $m or .name == ($m + ":latest"))' >/dev/null 2>&1; then
                ok "chat model '$MODEL' is available"
            else
                bad "chat model '$MODEL' not found — run: ollama pull $MODEL"
                errors=$((errors + 1))
            fi
            if printf '%s' "$tags" | jq -e --arg m "$EMBED_MODEL" '.models[]? | select(.name == $m or .name == ($m + ":latest"))' >/dev/null 2>&1; then
                ok "embed model '$EMBED_MODEL' is available"
                probe_embeddings=1
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
        models=$(
            python3 - "$HOST_URL" "$API_KEY_ENV" <<'PY' 2>&1
import json
import os
import sys
import urllib.error
import urllib.request

base_url, api_key_env = sys.argv[1:3]
api_key = os.environ.get(api_key_env or "", "")
headers = {}
if api_key:
    headers["authorization"] = f"Bearer {api_key}"
req = urllib.request.Request(f"{base_url.rstrip('/')}/models", headers=headers, method="GET")
try:
    with urllib.request.urlopen(req, timeout=30) as res:
        body = json.loads(res.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    detail = e.read().decode("utf-8", errors="ignore")[:300]
    print(f"HTTP {e.code} {detail}", file=sys.stderr)
    raise SystemExit(3 if e.code == 401 else 2)
except Exception as e:
    print(str(e), file=sys.stderr)
    raise SystemExit(2)
print(json.dumps(body))
PY
        )
        models_rc=$?
        if [ "$models_rc" -eq 0 ]; then
            ok "endpoint reachable at $HOST_URL/models"
            if [ -n "$API_KEY_ENV" ] && [ -n "$(printenv "$API_KEY_ENV" 2>/dev/null)" ]; then
                ok "api key env '$API_KEY_ENV' is set"
            fi
            if printf '%s' "$models" | jq -e --arg m "$MODEL" '.data[]? | select(.id == $m)' >/dev/null 2>&1; then
                ok "chat model '$MODEL' is available"
            else
                bad "chat model '$MODEL' not found at $HOST_URL — load the model in the server"
                errors=$((errors + 1))
            fi
            if printf '%s' "$models" | jq -e --arg m "$EMBED_MODEL" '.data[]? | select(.id == $m)' >/dev/null 2>&1; then
                ok "embed model '$EMBED_MODEL' is available"
                probe_embeddings=1
            else
                bad "embed model '$EMBED_MODEL' not found at $HOST_URL — load the model in the server"
                errors=$((errors + 1))
            fi
        elif [ "$models_rc" -eq 3 ]; then
            ok "endpoint reachable at $HOST_URL/models (auth required)"
            if [ -z "$API_KEY_ENV" ]; then
                bad "llm.api_key_env is required because endpoint requires authentication"
            elif [ -z "$(printenv "$API_KEY_ENV" 2>/dev/null)" ]; then
                bad "api key env '$API_KEY_ENV' is not set — export $API_KEY_ENV=..."
            else
                bad "endpoint rejected api key env '$API_KEY_ENV' at $HOST_URL/models"
            fi
            errors=$((errors + 1))
        else
            bad "endpoint unreachable or invalid at $HOST_URL/models: $models"
            errors=$((errors + 1))
        fi
        ;;
    *)
        info "unknown provider '$PROVIDER' — skipping reachability/model checks"
        ;;
esac

# (3) embed_dim sanity check against known models.
if [ "$EMBED_MODEL" = "bge-m3" ]; then
    KNOWN_DIM=1024
elif [ "$EMBED_MODEL" = "nomic-embed-text" ] || [ "$EMBED_MODEL" = "text-embedding-nomic-embed-text-v1.5" ]; then
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

# (4) Actual embedding endpoint probe. Model ids and known dims are not enough:
# LM Studio/OpenAI-compatible endpoints can list a model id but still return a different vector shape.
if [ "$probe_embeddings" -eq 1 ]; then
    probe_out=$(
        python3 - "$HOST_URL" "$EMBED_MODEL" "$API_KEY_ENV" <<'PY' 2>&1
import json
import os
import sys
import urllib.error
import urllib.request

base_url, model, api_key_env = sys.argv[1:4]
api_key = os.environ.get(api_key_env or "", "")
headers = {"content-type": "application/json"}
if api_key:
    headers["authorization"] = f"Bearer {api_key}"
payload = json.dumps({
    "model": model,
    "input": ["ohmyboring embedding dimension probe"],
    "encoding_format": "float",
}).encode("utf-8")
req = urllib.request.Request(
    f"{base_url.rstrip('/')}/embeddings",
    data=payload,
    headers=headers,
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=60) as res:
        body = json.loads(res.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    detail = e.read().decode("utf-8", errors="ignore")[:300]
    print(f"HTTP {e.code} {detail}", file=sys.stderr)
    raise SystemExit(2)
except Exception as e:
    print(str(e), file=sys.stderr)
    raise SystemExit(2)
data = body.get("data") or []
if not data or not isinstance(data[0].get("embedding"), list):
    print("embedding response missing data[0].embedding", file=sys.stderr)
    raise SystemExit(2)
print(len(data[0]["embedding"]))
PY
    )
    probe_rc=$?
    if [ "$probe_rc" -eq 0 ]; then
        if [ "$probe_out" = "$EMBED_DIM" ]; then
            ok "actual embedding dimension is $probe_out"
        else
            bad "actual embedding dimension mismatch: endpoint returned $probe_out, boring.json has $EMBED_DIM — update llm.embed_dim and run 'make reset'"
            errors=$((errors + 1))
        fi
    else
        bad "embedding endpoint probe failed for '$EMBED_MODEL' at $HOST_URL: $probe_out"
        errors=$((errors + 1))
    fi
fi

echo
if [ "$errors" -eq 0 ]; then
    ok "verify-llm: configuration looks consistent."
    exit 0
fi
bad "verify-llm: $errors issue(s) found — fix before running make up / make sync"
exit 1
