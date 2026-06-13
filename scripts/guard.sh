#!/bin/sh
# Structural gate (stack-free) — pre-commit + local. Enforces here the
# *mechanically enforceable* parts of PHILOSOPHY.md/RUST-STYLE.md. No stack (pg/ollama) needed.
#   1) rustfmt   — formatting (linear readability)
#   2) clippy -D — §A no-unwrap/expect/panic, todo, unreachable + ADT (wildcard), pedantic
#   3) test      — guardrail tests
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
echo "✅ 구조 게이트 통과 — 컴파일러/clippy/test 무위반."
