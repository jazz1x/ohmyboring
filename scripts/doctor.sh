#!/bin/sh
# Diagnose the self-augmentation write-door. The distill SessionEnd hook runs async
# and logs failures to stderr, but a down stack or bad BORING_URL can still drop sessions.
# This surfaces that signal: a clear OK/✗ per dependency plus proof the write-door is
# actually firing (newest distilled note + newest SessionEnd hook marker).
#   make doctor   or   ./scripts/doctor.sh
#
# Read-only: GET /health, GET /api/tags, `docker compose ps`, and mtime reads. No mutation.
# Exit non-zero only when drudge is down (the one hard dependency for the write door); the
# other lines are advisory so the user sees the WHOLE picture in one run, not just the first
# failure. POSIX sh (dash) has no pipefail → every check reports its own result explicitly.
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

ok()   { echo "✓ $1"; }
bad()  { echo "✗ $1"; }

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

drudge_down=0

# (a0) .env permissions — secrets live here.
if [ -f "$BORING_HOME/.env" ]; then
    perms=$(stat -c '%a' "$BORING_HOME/.env" 2>/dev/null || stat -f '%Lp' "$BORING_HOME/.env" 2>/dev/null)
    case "$perms" in
      600) ok ".env permissions are $perms" ;;
      *) bad ".env permissions are $perms — should be 600 (run: chmod 600 $BORING_HOME/.env)" ;;
    esac
fi

# (a1) Claude Code hooks — the async write-door into ~/.claude/settings.json.
settings="$HOME/.claude/settings.json"
if [ -f "$settings" ]; then
    if grep -q "$BORING_HOME/hooks/distill-session.py" "$settings" && grep -q "$BORING_HOME/hooks/recall.py" "$settings"; then
        ok "Claude Code hooks wired in $settings"
    else
        bad "Claude Code hooks missing in $settings — run install.sh"
    fi
else
    bad "Claude Code settings not found at $settings — run install.sh"
fi

# (a2) engine /health — the deterministic write gate the hook POSTs `remember` to.
if [ "$(curl -s -o /dev/null -w '%{http_code}' -m5 "$BORING_URL/health" 2>/dev/null)" = "200" ]; then
    ok "engine /health 200 ($BORING_URL) — write door reachable"
else
    bad "engine /health unreachable ($BORING_URL) — distilled sessions are being DROPPED"
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
        bad "Ollama unreachable ($NATIVE) — distillation can't generate notes (start: ollama serve)"
    fi
elif curl -sf -m5 "$LLM_BASE/models" >/dev/null 2>&1; then
    ok "$PROVIDER reachable ($LLM_BASE)"
else
    bad "$PROVIDER endpoint unreachable ($LLM_BASE) — distillation can't generate notes (server up? needs auth?)"
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
        printf '%s\n' "$ps" | grep -qi 'restarting' \
            && bad "a container is RESTARTING (crash-loop) — check 'make logs'"
    else
        bad "no compose containers found in $BORING_HOME (stack not started? set BORING_HOME?)"
    fi
else
    bad "docker not on PATH — can't inspect container status"
fi

# (d1) Newest distilled note — proof the write door produced output. The hook writes notes
# as vault/wiki/wiki-*.md, so the newest mtime is the last successful distillation.
note=$(newest "$BORING_HOME/vault/wiki/wiki-*.md")
if [ -n "$note" ]; then
    ok "newest distilled note: $(mtime_human "$note")"
    echo "    $note"
else
    bad "no distilled notes in $BORING_HOME/vault/wiki/ — nothing written yet"
fi

# (d2) Newest SessionEnd hook marker — proof the hook itself fired (it stamps MARK_DIR after
# a successful remember). A fresh note but a stale marker (or vice-versa) localizes the break.
mark=$(newest "$MARK_DIR/*.ts")
if [ -n "$mark" ]; then
    ok "newest SessionEnd hook marker: $(mtime_human "$mark")"
    echo "    $mark"
else
    bad "no hook markers in $MARK_DIR — the SessionEnd hook has not fired (installed in ~/.claude/settings.json?)"
fi

echo
if [ "$drudge_down" -eq 0 ]; then
    ok "doctor: drudge is up — the write door is open."
    exit 0
fi
bad "doctor: drudge is DOWN — sessions are silently dropped until it's back up."
exit 1
