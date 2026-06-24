#!/bin/sh
# Provider bootstrap: Ollama. Ensure the daemon is up, then pull the chat + embed models.
# The engine talks OpenAI `/v1`; Ollama's *native* API (used for /api/tags + `ollama pull`) lives at
# the host root (no /v1), so we strip a trailing /v1 here.
# Args: <base_url(/v1)> <chat_model> <embed_model> <bootstrap(auto|manual)>
set -eu
BASE_URL="$1"
CHAT="$2"
EMB="$3"
BOOTSTRAP="${4:-auto}"
DIR="$(dirname "$0")"

NATIVE="${BASE_URL%/v1}"
NATIVE="${NATIVE%/}"
# host.docker.internal resolves only inside containers; from the host it must be localhost.
NATIVE=$(printf '%s' "$NATIVE" | sed 's#host\.docker\.internal#localhost#')

# bootstrap=manual: never start/pull — only confirm reachability (user owns the daemon).
if [ "$BOOTSTRAP" = manual ]; then
  if curl -sf "${NATIVE}/api/tags" >/dev/null 2>&1; then
    echo "  ✓ Ollama reachable at ${NATIVE} (bootstrap=manual — not pulling)"
    exit 0
  fi
  echo "  ✗ Ollama not reachable at ${NATIVE} and bootstrap=manual — start it yourself: ollama serve"
  exit 1
fi

echo "▶ Ensuring Ollama is running …"
OLLAMA_HOST="$NATIVE" "$DIR/../ensure-ollama.sh"
echo "  ✓ Ollama OK"

for m in "$CHAT" "$EMB"; do
  # Match the exact tag (Ollama implies :latest for a bare name). Matching only the family
  # prefix would wrongly skip the pull when a different tag of the same family is present.
  case "$m" in *:*) want="$m" ;; *) want="$m:latest" ;; esac
  if ! curl -sf "${NATIVE}/api/tags" | grep -q "\"${want}\""; then
    echo "▶ Pulling model: $m (several GB)"
    ollama pull "$m"
  fi
done
