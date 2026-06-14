#!/bin/sh
# Behavioral regression gate (full stack) — before big changes/deploys. drudge must be up on :7700.
# Two-tier pattern: structural gate (guard.sh, no stack) / behavioral gate (here, stack needed:
# vector+graph recall regression).
#
# STATUS: the eval harness (data/eval/run_eval.py + fixtures) is not committed yet. Until it exists,
# this gate is a documented no-op rather than a vapor command that errors — it checks the stack is up,
# then skips with a clear message. (Don't claim a floor we don't actually measure.)
set -eu
cd "$(dirname "$0")/.."
if ! curl -s -m3 http://localhost:7700/health >/dev/null 2>&1; then
  echo "drudge not running (:7700). Run 'make up' first."
  exit 1
fi
if [ -f data/eval/run_eval.py ]; then
  python3 data/eval/run_eval.py --check
else
  echo "⏭  eval harness not present (data/eval/run_eval.py) — behavioral gate skipped."
  echo "    Add fixtures + run_eval.py to enable recall@1/MRR/answer-keyword floors."
fi
