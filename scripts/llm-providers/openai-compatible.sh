#!/bin/sh
# Provider bootstrap: generic OpenAI-compatible server (vLLM, llama.cpp server, remote OpenAI, …).
# Nothing to pull. We ping `/v1/models` as a best-effort reachability hint, but DO NOT fail the boot:
# a remote/authed endpoint may answer 401 to an unauthenticated probe (the engine sends the Bearer key
# from `api_key_env`), and remote availability is outside our control. Warn, don't block.
# Args: <base_url(/v1)> <chat_model> <embed_model> <bootstrap>
set -eu
BASE_URL="$1"
# host.docker.internal resolves only inside containers; from the host it must be localhost.
BASE_URL=$(printf '%s' "$BASE_URL" | sed 's#host\.docker\.internal#localhost#')

if curl -sf "${BASE_URL}/models" >/dev/null 2>&1; then
  echo "  ✓ OpenAI-compatible server reachable at ${BASE_URL}"
else
  echo "  ⓘ Could not anonymously reach ${BASE_URL}/models (server down, or it requires auth — the"
  echo "    engine will still send the API key from api_key_env). Continuing without blocking."
fi
