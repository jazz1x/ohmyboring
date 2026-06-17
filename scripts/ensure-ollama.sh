#!/bin/sh
# Ensure Ollama is reachable before starting the engine.
# If OLLAMA_HOST is set we use it; otherwise default to local Ollama.
# When Ollama is not responding, we try to start it in the background once.
# This is a best-effort helper; if it fails, the user is told to run `ollama serve` manually.
set -eu

HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"

is_running() {
    curl -sf "${HOST}/api/tags" >/dev/null 2>&1
}

if is_running; then
    echo "ⓘ Ollama is already running at ${HOST}"
    exit 0
fi

if command -v ollama >/dev/null 2>&1; then
    echo "ⓘ Ollama not responding at ${HOST}. Starting 'ollama serve' in the background..."
    nohup ollama serve >/tmp/ollama-ensure.log 2>&1 &
    # Wait up to 15s for the server to come up.
    for _ in $(seq 1 15); do
        sleep 1
        if is_running; then
            echo "✓ Ollama started."
            exit 0
        fi
    done
fi

cat <<MSG
✗ Could not reach or start Ollama at ${HOST}.

Please start it manually:

    ollama serve

Or set OLLAMA_HOST if it runs elsewhere:

    export OLLAMA_HOST=http://host:11434
MSG
exit 1
