#!/bin/sh
# Behavioral regression gate (full stack) — before big changes/deploys. drudge must be up on :7700.
# Two-tier pattern: structural gate (guard.sh, no stack) / behavioral gate (here, stack needed:
# vector+graph recall regression).
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

URL="${BORING_URL:-http://localhost:7700}"
EVENT_LOG="$ROOT/agents/shared/event_log.py"
eval_run_id="eval-$(date +%Y%m%dT%H%M%S)-$$"
eval_started_at="$(date +%s)"
eval_fixtures_copied=0

log_eval_event() {
  status="$1"
  shift
  if [ -f "$EVENT_LOG" ]; then
    if ! python3 "$EVENT_LOG" --record eval eval_gate "$status" \
      --field "run_id=$eval_run_id" "$@"; then
      echo "⚠ eval event log write failed" >&2
    fi
  fi
}

cleanup() {
  if [ "$eval_fixtures_copied" -eq 1 ]; then
    rm -f vault/wiki/eval-*.md
    echo "▶ Cleaned up eval fixtures from vault/wiki"
    if curl -s -m600 -X POST "$URL/sync" >/dev/null 2>&1; then
      echo "▶ Re-synced vault after eval cleanup"
    else
      echo "⚠ eval cleanup sync failed; run 'make sync' before relying on briefings" >&2
    fi
  fi
}

finish_eval_event() {
  rc=$?
  cleanup
  duration_s="$(($(date +%s) - eval_started_at))"
  if [ "$rc" -eq 0 ]; then
    log_eval_event ok --field "duration_s=$duration_s" --field "fixtures_copied=$eval_fixtures_copied"
  else
    log_eval_event failed --field "duration_s=$duration_s" --field "fixtures_copied=$eval_fixtures_copied" --field "exit_code=$rc"
  fi
  exit "$rc"
}

trap finish_eval_event EXIT

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
eval_fixtures_copied=1

echo "▶ Syncing eval corpus …"
curl -s -m600 -X POST "$URL/sync" >/dev/null

echo "▶ Running eval …"
python3 data/eval/run_eval.py
