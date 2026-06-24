#!/bin/sh
# Structural gate (stack-free) — pre-commit + local. Enforces here the
# *mechanically enforceable* parts of PHILOSOPHY.md/RUST-STYLE.md. No stack (pg/ollama) needed.
#   1) rustfmt   — formatting (linear readability)
#   2) clippy -D — §A no-unwrap/expect/panic, todo, unreachable + ADT (wildcard), pedantic
#   3) test      — guardrail tests
#   4) py-compile — syntax gate for all Python touched by pre-commit
#   5) py-unit   — network-free Python regression tests (incl. destructive-path planners)
#   6) sh-unit   — destructive shell-path guardrails (restore-db drop ordering)
# No bypassing (git commit --no-verify) — on failure, fix the root cause (don't paper over the symptom).
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/drudge"
echo "1) rustfmt --check…"
cargo fmt --check
echo "2) clippy (-D warnings)…"
cargo clippy --quiet --all-targets -- -D warnings
echo "3) test…"
cargo test --quiet
cd "$ROOT"
echo "4) python py-compile (agents + hooks + scripts + data/eval)…"
find agents hooks scripts data/eval -name '*.py' -type f -print0 | xargs -0 -n1 python3 -m py_compile
echo "5) python unit tests…"
python3 agents/shared/test_boring_config.py
python3 agents/shared/test_agent_wiring.py
python3 agents/shared/test_transcript.py
python3 agents/claude-code/test_hooks.py
python3 agents/kimi/test_kimi.py
python3 agents/hermes/test_ingest_worker.py
python3 scripts/test_data_steward.py
python3 scripts/test_retention.py
echo "6) shell destructive-path guardrails (restore-db)…"
sh scripts/test_restore_db.sh
echo "✅ 구조 게이트 통과 — 컴파일러/clippy/test + Python adapters 무위반."
