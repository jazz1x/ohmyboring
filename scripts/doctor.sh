#!/bin/sh
# Diagnose the self-augmentation write-door. The distill SessionEnd hook runs async
# and logs failures to stderr, but a down stack or bad BORING_URL can still drop sessions.
# This surfaces that signal: a clear OK/✗ per dependency plus proof the write-door is
# actually firing (newest distilled note + newest SessionEnd hook marker).
#   make doctor   or   ./scripts/doctor.sh
#
# Read-only: GET /health, GET /api/tags, `docker compose ps`, mtime reads,
# and Codex queue/worker status scans. No mutation.
# Default mode exits non-zero only when drudge is down; the other lines are advisory so the
# user sees the WHOLE picture in one run, not just the first failure. Use --strict for the
# release/briefing readiness gate: every failed dependency/check makes the command fail.
# POSIX sh (dash) has no pipefail → every check reports its own result explicitly.
set -u

# Defaults mirror the hook (distill-session.py): BORING_URL + BORING_HOME + the marker dir,
# so the diagnostic inspects exactly what the hook writes to.
BORING_URL="${BORING_URL:-http://127.0.0.1:7700}"
OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
BORING_HOME="${BORING_HOME:-$HOME/oh-my-boring}"
MARK_DIR="${HOME}/.cache/boring-distill"

# host.docker.internal resolves only inside containers; from the host it must be localhost,
# or this host-side reachability check fails even when Ollama is healthy. dash has no
# ${VAR/a/b} substitution, so rewrite via sed (same approach as scripts/ensure-ollama.sh).
OLLAMA_HOST=$(printf '%s' "$OLLAMA_HOST" | sed 's#host\.docker\.internal#localhost#')

# --fix mode: attempt safe auto-repair for mechanical problems, then re-run read-only.
FIX=0
STRICT=0
for arg in "$@"; do
    case "$arg" in
        --fix) FIX=1 ;;
        --strict) STRICT=1 ;;
    esac
done

# Track which checks failed so --fix knows what to repair.
failed_env=0
failed_hooks=0
failed_engine=0
failed_ollama=0
failed_containers=0
failed_note=0
failed_marker=0
failed_codex=0

ok()   { echo "✓ $1"; }
bad()  { echo "✗ $1"; }

# Safe auto-repair helpers. Dangerous ops (reset/restore) are intentionally absent.
fix_env() {
    echo "  → fixing .env permissions: chmod 600 $BORING_HOME/.env"
    chmod 600 "$BORING_HOME/.env"
}

fix_hooks() {
    echo "  → fixing agent hooks: python3 $BORING_HOME/agents/shared/agent_wiring.py --install --boring-home $BORING_HOME"
    python3 "$BORING_HOME/agents/shared/agent_wiring.py" --install --boring-home "$BORING_HOME"
}

fix_engine() {
    echo "  → fixing engine: make up in $BORING_HOME"
    (cd "$BORING_HOME" && make up >/tmp/omb-doctor-fix-engine.log 2>&1)
}

fix_ollama() {
    echo "  → fixing Ollama: $BORING_HOME/scripts/ensure-ollama.sh"
    "$BORING_HOME/scripts/ensure-ollama.sh"
}

fix_containers() {
    echo "  → fixing containers: make down && make up in $BORING_HOME"
    (cd "$BORING_HOME" && make down >/tmp/omb-doctor-fix-containers.log 2>&1 && make up >>/tmp/omb-doctor-fix-containers.log 2>&1)
}

# Human-readable mtime of a file, portable across GNU coreutils and BSD/macOS stat.
# Display only (no epoch math), so neither `date -d @` (GNU) nor `date -r` (BSD) is needed.
mtime_human() {
    stat -c '%y' "$1" 2>/dev/null && return 0   # GNU coreutils
    stat -f '%Sm' "$1" 2>/dev/null && return 0  # BSD / macOS
    echo "unknown"
}

# Newest file matching a glob, or empty. `ls -t` (POSIX) avoids non-portable find -printf;
# the glob is passed unquoted so the shell expands it.
newest() {
    # shellcheck disable=SC2086
    ls -t $1 2>/dev/null | head -n 1
}

newest_session_marker() {
    # shellcheck disable=SC2086
    for f in $(ls -t $1 2>/dev/null); do
        case "$(basename "$f")" in
            codex-*) continue ;;
        esac
        printf '%s\n' "$f"
        return 0
    done
}

drudge_down=0

# (a0) .env permissions — secrets live here.
if [ -f "$BORING_HOME/.env" ]; then
    perms=$(stat -c '%a' "$BORING_HOME/.env" 2>/dev/null || stat -f '%Lp' "$BORING_HOME/.env" 2>/dev/null)
    case "$perms" in
      600) ok ".env permissions are $perms" ;;
      *) bad ".env permissions are $perms — should be 600 (run: chmod 600 $BORING_HOME/.env)"; failed_env=1 ;;
    esac
fi

# (a1) Claude Code hooks — the async write-door into ~/.claude/settings.json.
settings="$HOME/.claude/settings.json"
if [ -f "$settings" ]; then
    if grep -q "$BORING_HOME/hooks/distill-session.py" "$settings" && grep -q "$BORING_HOME/hooks/recall.py" "$settings"; then
        ok "Claude Code hooks wired in $settings"
    else
        bad "Claude Code hooks missing in $settings — run install.sh"; failed_hooks=1
    fi
else
    bad "Claude Code settings not found at $settings — run install.sh"; failed_hooks=1
fi

# (a2) engine /health — the deterministic write gate the hook POSTs `remember` to.
if [ "$(curl -s -o /dev/null -w '%{http_code}' -m5 "$BORING_URL/health" 2>/dev/null)" = "200" ]; then
    ok "engine /health 200 ($BORING_URL) — write door reachable"
else
    bad "engine /health unreachable ($BORING_URL) — distilled sessions are being DROPPED"; failed_engine=1
    drudge_down=1
fi

# (b) LLM endpoint — the model that generates the curated note before remember is called.
# Provider-aware (boring.json `llm` block): Ollama exposes /api/tags, OpenAI-compatible servers
# (LM Studio, vLLM, remote OpenAI) expose /v1/models. Env overrides boring.json (BORING_LLM_BASE_URL).
BORING="${BORING_CONFIG:-$BORING_HOME/boring.json}"
PROVIDER=$(jq -r '.llm.provider // "ollama"' "$BORING" 2>/dev/null || echo ollama)
LLM_BASE=$(jq -r '.llm.base_url // "http://host.docker.internal:11434/v1"' "$BORING" 2>/dev/null || echo "http://host.docker.internal:11434/v1")
LLM_BASE="${BORING_LLM_BASE_URL:-$LLM_BASE}"
LLM_BASE=$(printf '%s' "$LLM_BASE" | sed 's#host\.docker\.internal#localhost#')
if [ "$PROVIDER" = ollama ]; then
    NATIVE="${LLM_BASE%/v1}"; NATIVE="${NATIVE%/}"
    NATIVE="${OLLAMA_HOST:-$NATIVE}"  # explicit OLLAMA_HOST still wins
    if curl -sf -m5 "$NATIVE/api/tags" >/dev/null 2>&1; then
        ok "Ollama reachable ($NATIVE)"
    else
        bad "Ollama unreachable ($NATIVE) — distillation can't generate notes (start: ollama serve)"; failed_ollama=1
    fi
elif curl -sf -m5 "$LLM_BASE/models" >/dev/null 2>&1; then
    ok "$PROVIDER reachable ($LLM_BASE)"
else
    bad "$PROVIDER endpoint unreachable ($LLM_BASE) — distillation can't generate notes (server up? needs auth?)"; failed_ollama=1
fi

# (c) Container status — surface crash-loops the HTTP probes alone would miss.
if command -v docker >/dev/null 2>&1; then
    if docker compose version 2>&1 | grep -q "Docker Compose"; then
      COMPOSE="docker compose"
    else
      COMPOSE="docker-compose"
    fi
    ps=$(cd "$BORING_HOME" 2>/dev/null && $COMPOSE ps --format '{{.Name}} {{.Status}}' 2>/dev/null)
    if [ -n "$ps" ]; then
        ok "containers ($COMPOSE ps in $BORING_HOME):"
        printf '%s\n' "$ps" | sed 's/^/    /'
        if printf '%s\n' "$ps" | grep -qi 'restarting'; then
            bad "a container is RESTARTING (crash-loop) — check 'make logs'"; failed_containers=1
        fi
    else
        bad "no compose containers found in $BORING_HOME (stack not started? set BORING_HOME?)"; failed_containers=1
    fi
else
    bad "docker not on PATH — can't inspect container status"; failed_containers=1
fi

# (d1) Newest distilled note — proof the write door produced output. The hook writes notes
# as vault/wiki/wiki-*.md, so the newest mtime is the last successful distillation.
note=$(newest "$BORING_HOME/vault/wiki/wiki-*.md")
if [ -n "$note" ]; then
    ok "newest distilled note: $(mtime_human "$note")"
    echo "    $note"
else
    bad "no distilled notes in $BORING_HOME/vault/wiki/ — nothing written yet"; failed_note=1
fi

# (d2) Newest SessionEnd hook marker — proof the hook itself fired (it stamps MARK_DIR after
# a successful remember). A fresh note but a stale marker (or vice-versa) localizes the break.
mark=$(newest_session_marker "$MARK_DIR/*.ts")
if [ -n "$mark" ]; then
    ok "newest Claude/Kimi SessionEnd hook marker: $(mtime_human "$mark")"
    echo "    $mark"
else
    bad "no Claude/Kimi hook markers in $MARK_DIR — the SessionEnd hook has not fired (installed in ~/.claude/settings.json?)"; failed_marker=1
fi

# (d3) Codex worker/queue status — Codex has no SessionEnd hook, so the write-door
# is a hermes cron worker scanning ~/.codex/sessions. Keep the collector status as
# an internal read-only probe and expose it here with the rest of doctor.
codex_status="$BORING_HOME/agents/codex/collect-sessions.py"
if [ -f "$codex_status" ]; then
    ok "Codex session ingestion status:"
    codex_args="--status"
    [ "$STRICT" -eq 1 ] && codex_args="--status --strict"
    if BORING_HOME="$BORING_HOME" BORING_VAULT_DIR="${BORING_VAULT_DIR:-$BORING_HOME/vault}" python3 "$codex_status" $codex_args; then
        :
    else
        bad "Codex session ingestion status failed"; failed_codex=1
    fi
else
    bad "Codex collector not found at $codex_status"; failed_codex=1
fi

if [ "$FIX" -eq 1 ]; then
    echo
    echo "Applying fixes..."
    [ "$failed_env" -eq 1 ] && fix_env
    [ "$failed_hooks" -eq 1 ] && fix_hooks
    [ "$failed_engine" -eq 1 ] && fix_engine
    [ "$failed_ollama" -eq 1 ] && fix_ollama
    [ "$failed_containers" -eq 1 ] && fix_containers
    echo
    echo "Re-running doctor to verify..."
    if [ "$STRICT" -eq 1 ]; then
        exec "$0" --strict
    fi
    exec "$0"
fi

echo
if [ "$STRICT" -eq 1 ]; then
    failures="${failed_env}${failed_hooks}${failed_engine}${failed_ollama}${failed_containers}${failed_note}${failed_marker}${failed_codex}"
    if [ "$failures" = "00000000" ]; then
        ok "readiness: all doctor checks passed — briefing/write-door dependencies are ready."
        exit 0
    fi
    bad "readiness: one or more doctor checks failed — do not rely on tomorrow's briefing until fixed."
    exit 1
fi

if [ "$drudge_down" -eq 0 ]; then
    ok "doctor: drudge is up — the write door is open."
    exit 0
fi
bad "doctor: drudge is DOWN — sessions are silently dropped until it's back up."
exit 1
