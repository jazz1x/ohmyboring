#!/bin/sh
# Structural gate (stack-free) — pre-commit + local. Enforces here the
# *mechanically enforceable* parts of PHILOSOPHY.md/RUST-STYLE.md. No stack (pg/ollama) needed.
#   1) rustfmt   — formatting (linear readability)
#   2) clippy -D — §A no-unwrap/expect/panic, todo, unreachable + ADT (wildcard), pedantic
#   3) test      — guardrail tests
#   4) py-compile — syntax gate for all Python touched by pre-commit
#   5) py-unit   — network-free Python regression tests (incl. destructive-path planners)
#   6) sh-unit   — destructive shell-path guardrails (restore-db drop ordering)
#   7) sh-unit   — readiness gate guardrails (doctor --strict exit semantics)
#   8) sh-unit   — provider/model guardrails (verify-llm embedding shape)
# No bypassing (git commit --no-verify) — on failure, fix the root cause (don't paper over the symptom).
set -eu
PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-${TMPDIR:-/tmp}/oh-my-boring-pyc}"
export PYTHONPYCACHEPREFIX
mkdir -p "$PYTHONPYCACHEPREFIX"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EVENT_LOG="$ROOT/agents/shared/event_log.py"
guard_run_id="guard-$(date +%Y%m%dT%H%M%S)-$$"
guard_started_at="$(date +%s)"

log_guard_event() {
  status="$1"
  shift
  if [ -f "$EVENT_LOG" ]; then
    if ! python3 "$EVENT_LOG" --record guard structural_guard "$status" \
      --field "run_id=$guard_run_id" "$@"; then
      echo "⚠ guard event log write failed" >&2
    fi
  fi
}

finish_guard_event() {
  rc=$?
  duration_s="$(($(date +%s) - guard_started_at))"
  if [ "$rc" -eq 0 ]; then
    log_guard_event ok --field "duration_s=$duration_s"
  else
    log_guard_event failed --field "duration_s=$duration_s" --field "exit_code=$rc"
  fi
  exit "$rc"
}

trap finish_guard_event EXIT

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
python3 agents/shared/test_distill_core.py
python3 agents/shared/test_event_log.py
python3 agents/shared/test_workflow_contract.py
python3 agents/shared/test_markers.py
python3 agents/shared/test_resolution_quality.py
python3 agents/shared/test_transcript.py
python3 agents/shared/test_recall_core.py
python3 agents/claude-code/test_hooks.py
python3 agents/kimi/test_kimi.py
python3 agents/schedulers/test_collectors.py
python3 agents/hermes/test_ingest_worker.py
python3 scripts/test_data_steward.py
python3 scripts/test_vault_cleanup_gate.py
python3 scripts/test_retention.py
python3 scripts/test_self_verify_contract.py
echo "6) shell destructive-path guardrails (restore-db)…"
sh scripts/test_restore_db.sh
echo "7) shell readiness gate guardrails (doctor --strict)…"
sh scripts/test_doctor.sh
echo "8) shell LLM/provider guardrails (verify-llm)…"
sh scripts/test_verify_llm.sh
echo "✅ 구조 게이트 통과 — 컴파일러/clippy/test + Python adapters 무위반."
