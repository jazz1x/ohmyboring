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
#   7b) sh-unit  — morning-card guardrails (doctor says whether the card went out)
#   8) sh-unit   — provider/model guardrails (verify-llm embedding shape)
#   9) sh-unit   — morning-card guardrails (failure notice to the owner's DM)
#   10) sh-unit  — nightly-maintenance guardrails (doctor failure DM)
# No bypassing (git commit --no-verify) — on failure, fix the root cause (don't paper over the symptom).
set -eu
PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-${TMPDIR:-/tmp}/oh-my-boring-pyc}"
export PYTHONPYCACHEPREFIX
mkdir -p "$PYTHONPYCACHEPREFIX"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EVENT_LOG="$ROOT/src/ohmyboring/adapters/events.py"
guard_run_id="guard-$(date +%Y%m%dT%H%M%S)-$$"
guard_started_at="$(date +%s)"
# Test events go to this temp file, never the live sink. The guard's own result event still
# goes to the real sink (log_guard_event unsets the variable), then the file is removed.
export BORING_EVENT_ISOLATED_LOG="$(mktemp -t boring-guard-events.XXXXXX)"

log_guard_event() {
  status="$1"
  shift
  if [ -f "$EVENT_LOG" ]; then
    if ! env -u BORING_EVENT_ISOLATED_LOG uv run --locked python "$EVENT_LOG" --record guard structural_guard "$status" \
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
  rm -f "$BORING_EVENT_ISOLATED_LOG"
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
find agents hooks scripts data/eval -name '*.py' -type f -print0 | xargs -0 -n1 uv run --locked python -m py_compile
echo "4b) python lint + format (ruff, rules in ruff.toml)…"
command -v ruff >/dev/null 2>&1 || { echo "ruff not installed — pip install -r requirements-dev.txt (or: uvx ruff)"; exit 1; }
ruff check .
ruff format --check .
echo "5) python unit tests…"
uv run --locked python src/ohmyboring/test_config.py
uv run --locked python src/ohmyboring/ingest/test_note.py
uv run --locked python src/ohmyboring/ingest/test_chunk.py
uv run --locked python src/ohmyboring/distill/test_graph.py
uv run --locked python src/ohmyboring/distill/test_prompts.py
uv run --locked python src/ohmyboring/distill/test_run.py
uv run --locked python src/ohmyboring/distill/test_resolution.py
uv run --locked python src/ohmyboring/distill/test_polish.py
uv run --locked python src/ohmyboring/weekly/test_graph.py
uv run --locked python src/ohmyboring/weekly/test_run.py
uv run --locked python src/ohmyboring/weekly/test_pins.py
uv run --locked python agents/shared/test_agent_wiring.py
uv run --locked python agents/shared/test_rules_core.py
uv run --locked python agents/shared/test_distill_core.py
uv run --locked python agents/shared/test_distill_queue.py
uv run --locked python agents/shared/test_markers.py
uv run --locked python agents/shared/test_transcript.py
uv run --locked python agents/shared/test_recall_core.py
uv run --locked python agents/shared/test_label_core.py
uv run --locked python agents/shared/test_uptake_core.py
uv run --locked python agents/shared/test_verdict_core.py
uv run --locked python src/ohmyboring/adapters/test_embed.py
uv run --locked python src/ohmyboring/adapters/test_engine.py
uv run --locked python src/ohmyboring/adapters/test_events.py
uv run --locked python src/ohmyboring/adapters/test_llm.py
uv run --locked python src/ohmyboring/adapters/test_slack.py
uv run --locked python src/ohmyboring/adapters/test_workflow_contract.py
uv run --locked python src/ohmyboring/remember/test_parse.py
uv run --locked python src/ohmyboring/remember/test_render.py
uv run --locked python src/ohmyboring/remember/test_graph.py
uv run --locked python src/ohmyboring/remember/test_pii.py
uv run --locked python src/ohmyboring/remember/test_dedup.py
uv run --locked python src/ohmyboring/remember/test_index.py
uv run --locked python src/ohmyboring/remember/test_writer.py
uv run --locked python src/ohmyboring/sync/test_sync.py
uv run --locked python src/ohmyboring/gap/test_gap.py
uv run --locked python src/ohmyboring/test_result.py
uv run --locked python agents/door/test_door.py
uv run --locked python agents/door/test_door_search.py
uv run --locked python agents/memory/test_retriever.py
uv run --locked python agents/memory/test_store.py
uv run --locked python agents/memory/test_deep_agent.py
uv run --locked python agents/claude-code/test_hooks.py
uv run --locked python agents/schedulers/test_collectors.py
uv run --locked python agents/codex/test_codex.py
uv run --locked python agents/hermes/test_briefing_format.py
uv run --locked python agents/hermes/test_ingest_worker.py
uv run --locked python agents/hermes/test_card_triggers.py
uv run --locked python agents/hermes/plugins/boring-card/test_boring_card.py
uv run --locked python agents/hermes/plugins/boring-memory/test_boring_memory.py
uv run --locked python agents/slack/test_card_types.py
uv run --locked python agents/slack/test_card_registers.py
uv run --locked python agents/slack/test_card_advice.py
uv run --locked python agents/slack/test_card_advice_write.py
uv run --locked python agents/slack/test_card_delegate.py
uv run --locked python agents/slack/test_card_verdicts.py
uv run --locked python agents/slack/test_card_repair_judge.py
uv run --locked python agents/slack/test_card_view.py
uv run --locked python agents/slack/test_card_press.py
uv run --locked python agents/slack/test_card_graph.py
uv run --locked python agents/slack/test_weekly_card.py
uv run --locked python agents/slack/test_secretary.py
uv run --locked python agents/slack/test_secretary_core.py
uv run --locked python agents/shared/test_vault_note.py
uv run --locked python scripts/test_python_deps.py
uv run --locked python scripts/test_runbook.py
uv run --locked python scripts/test_changelog.py
uv run --locked python scripts/test_data_steward.py
uv run --locked python scripts/test_vault_cleanup_gate.py
uv run --locked python scripts/test_usage.py
uv run --locked python scripts/test_rule_firings.py
uv run --locked python scripts/test_anchor_shadow.py
uv run --locked python scripts/test_rework_shadow.py
uv run --locked python scripts/test_uptake_verdict.py
uv run --locked python scripts/test_retention.py
uv run --locked python scripts/test_self_verify_contract.py
uv run --locked python scripts/test_peek.py
uv run --locked python scripts/test_readme_locale_parity.py
uv run --locked python scripts/test_package_skeleton.py
uv run --locked python src/ohmyboring/recall/test_named.py
uv run --locked python src/ohmyboring/recall/test_recall.py
uv run --locked python src/ohmyboring/entrypoints/http/test_mcp_recall.py
uv run --locked python src/ohmyboring/search/test_rank.py
uv run --locked python src/ohmyboring/search/test_hits.py
uv run --locked python src/ohmyboring/search/test_retriever.py
uv run --locked python src/ohmyboring/search/test_pg.py
uv run --locked python src/ohmyboring/registers/test_pg.py
uv run --locked python src/ohmyboring/verdict/test_verdict.py
uv run --locked python scripts/test_verdict_parity.py
uv run --locked python src/ohmyboring/events/test_events.py
uv run --locked python scripts/test_events_parity.py
uv run --locked python src/ohmyboring/search/test_redact.py
echo "6) shell destructive-path guardrails (restore-db)…"
sh scripts/test_restore_db.sh
echo "7) shell readiness gate guardrails (doctor --strict)…"
sh scripts/test_doctor.sh
sh scripts/test_drudge_health_readiness.sh
sh scripts/test_doctor_maintenance.sh
sh scripts/test_doctor_card.sh
sh scripts/test_doctor_door.sh
sh scripts/test_doctor_vector.sh
echo "8) shell LLM/provider guardrails (verify-llm)…"
sh scripts/test_verify_llm.sh
echo "9) shell morning-card guardrails (failure notice)…"
sh scripts/test_schedule_card.sh
echo "10) shell nightly-maintenance guardrails (doctor DM)…"
sh scripts/test_schedule_maintenance.sh
echo "✅ 구조 게이트 통과 — 컴파일러/clippy/test + Python adapters 무위반."
