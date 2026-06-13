#!/bin/sh
# Behavioral regression gate (full stack) — before big changes/deploys. drudge must be up on :7700.
# Aligned with the two-tier pattern of company-kb-bot scripts/eval-gate.sh:
#   structural gate (guard.sh) = no stack needed / behavioral gate (here) = stack needed (vector+graph recall regression).
# run_eval --check: exits non-zero if recall@1/MRR/answer-keyword floors aren't met → halts progress.
set -eu
cd "$(dirname "$0")/.."
if ! curl -s -m3 http://localhost:7700/health >/dev/null 2>&1; then
  echo "❌ drudge 미가동(:7700). 'docker compose up -d' 먼저."
  exit 1
fi
python3 data/eval/run_eval.py --check
