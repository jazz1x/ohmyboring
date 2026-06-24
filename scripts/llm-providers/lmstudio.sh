#!/bin/sh
# Provider bootstrap: LM Studio. OpenAI-compatible `/v1` — models are loaded in the LM Studio app or
# via the `lms` CLI, so there is nothing to pull. We only health-check `/v1/models` and, if the server
# is down, fail fast with a clear hint (start the server first, then re-run `make up`).
# Args: <base_url(/v1)> <chat_model> <embed_model> <bootstrap>
set -eu
BASE_URL="$1"
CHAT="$2"
EMB="$3"
# host.docker.internal resolves only inside containers; from the host it must be localhost.
BASE_URL=$(printf '%s' "$BASE_URL" | sed 's#host\.docker\.internal#localhost#')

if curl -sf "${BASE_URL}/models" >/dev/null 2>&1; then
  echo "  ✓ LM Studio reachable at ${BASE_URL} (OpenAI-compatible /v1)"
  exit 0
fi

cat <<MSG
  ✗ LM Studio not reachable at ${BASE_URL}.
    1. Open LM Studio → Developer (or use the CLI: 'lms server start').
    2. Start the local server (default http://localhost:1234/v1).
    3. Load your chat model ($CHAT) and embedding model ($EMB).
    Then re-run 'make up'.
MSG
exit 1
