#!/bin/sh
# Behavioral regression gate (full stack) — before big changes/deploys. drudge must be up on :7700.
# Two-tier pattern: structural gate (guard.sh, no stack) / behavioral gate (here, stack needed:
# vector+graph recall regression).
set -eu
cd "$(dirname "$0")/.."

URL="${BORING_URL:-${DRUDGE_URL:-http://localhost:7700}}"

if ! curl -s -m3 "$URL/health" >/dev/null 2>&1; then
  echo "engine not running ($URL). Run 'make up' first."
  exit 1
fi

if [ ! -f data/eval/run_eval.py ]; then
  echo "⏭  eval harness not present (data/eval/run_eval.py) — behavioral gate skipped."
  echo "    Add fixtures + run_eval.py to enable recall@1/MRR/answer-keyword floors."
  exit 0
fi

# Inject eval fixtures into the live vault, sync, run eval, then clean up.
# This keeps the eval corpus in git (data/eval/fixtures/) without polluting vault/wiki permanently.
echo "▶ Copying eval fixtures into vault/wiki …"
cp data/eval/fixtures/eval-*.md vault/wiki/
cleanup() {
  rm -f vault/wiki/eval-*.md
  echo "▶ Cleaned up eval fixtures from vault/wiki"
}
trap cleanup EXIT

echo "▶ Syncing eval corpus …"
curl -s -m600 -X POST "$URL/sync" >/dev/null

echo "▶ Running eval …"
python3 data/eval/run_eval.py
