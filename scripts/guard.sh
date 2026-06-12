#!/bin/sh
# 구조 게이트 (스택-프리) — pre-commit + 로컬. PHILOSOPHY.md/RUST-STYLE.md 의
# *기계적으로 강제 가능한* 부분을 여기서 막는다. 스택(pg/ollama) 불필요.
#   1) rustfmt   — 형식(선형성 가독)
#   2) clippy -D — §A no-unwrap/expect/panic·todo·unreachable + ADT(wildcard)·pedantic
#   3) test      — 가드레일 테스트
# 우회(git commit --no-verify) 금지 — 실패 시 근본원인을 고친다(증상 무마 X).
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/hermes-rs"
echo "1) rustfmt --check…"
cargo fmt --check
echo "2) clippy (-D warnings)…"
cargo clippy --quiet --all-targets -- -D warnings
echo "3) test…"
cargo test --quiet
echo "✅ 구조 게이트 통과 — 컴파일러/clippy/test 무위반."
