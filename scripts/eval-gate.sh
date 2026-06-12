#!/bin/sh
# 행동 회귀 게이트 (풀 스택) — 큰 변경·배포 전. hermes-rs 가 :7700 에 떠 있어야 함.
# company-kb-bot scripts/eval-gate.sh 의 2계층 패턴 정렬:
#   구조 게이트(guard.sh) = 스택 불필요 / 행동 게이트(여기) = 스택 필요(벡터+그래프 회수 회귀).
# run_eval --check: recall@1/MRR/답변키워드 바닥선 미달 시 비0 종료 → 진행 중단.
set -eu
cd "$(dirname "$0")/.."
if ! curl -s -m3 http://localhost:7700/health >/dev/null 2>&1; then
  echo "❌ hermes-rs 미가동(:7700). 'docker compose up -d' 먼저."
  exit 1
fi
python3 data/eval/run_eval.py --check
