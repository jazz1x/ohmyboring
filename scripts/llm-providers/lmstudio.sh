#!/bin/sh
# Provider bootstrap: LM Studio. OpenAI-compatible `/v1` — models are loaded in the LM Studio app or
# via the `lms` CLI, so there is nothing to pull. We verify `/v1/models` includes both configured
# model ids; otherwise setup would look complete while the first ask/distill fails.
# Args: <base_url(/v1)> <chat_model> <embed_model> <bootstrap>
set -eu
BASE_URL="$1"
CHAT="$2"
EMB="$3"
# host.docker.internal resolves only inside containers; from the host it must be localhost.
BASE_URL=$(printf '%s' "$BASE_URL" | sed 's#host\.docker\.internal#localhost#')

if models=$(curl -sf "${BASE_URL}/models" 2>/dev/null); then
  missing=0
  if printf '%s' "$models" | jq -e --arg m "$CHAT" '.data[]? | select(.id == $m)' >/dev/null 2>&1; then
    echo "  ✓ LM Studio chat model loaded: ${CHAT}"
  else
    echo "  ✗ LM Studio chat model not loaded: ${CHAT}"
    missing=1
  fi
  if printf '%s' "$models" | jq -e --arg m "$EMB" '.data[]? | select(.id == $m)' >/dev/null 2>&1; then
    echo "  ✓ LM Studio embedding model loaded: ${EMB}"
  else
    echo "  ✗ LM Studio embedding model not loaded: ${EMB}"
    missing=1
  fi
  if [ "$missing" -eq 0 ]; then
    echo "  ✓ LM Studio reachable at ${BASE_URL} (OpenAI-compatible /v1)"
    exit 0
  fi
fi

cat <<MSG
  ✗ LM Studio is not ready at ${BASE_URL}.
    1. Open LM Studio → Developer (or use the CLI: 'lms server start').
    2. Start the local server (default http://localhost:1234/v1).
    3. Load your chat model ($CHAT) and embedding model ($EMB).
    Then re-run 'make up'.
MSG
exit 1
