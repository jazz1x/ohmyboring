#!/bin/sh
# Diagnose the self-augmentation write-door — the distill SessionEnd hook runs async
# and ends in `except: pass`, so a down stack or bad DRUDGE_URL silently drops sessions
# with no signal. This surfaces that signal: a clear OK/✗ per dependency plus proof the
# write-door is actually firing (newest distilled note + newest SessionEnd hook marker).
#   make doctor   or   ./scripts/doctor.sh
#
# Read-only: GET /health, GET /api/tags, `docker compose ps`, and mtime reads. No mutation.
# Exit non-zero only when drudge is down (the one hard dependency for the write door); the
# other lines are advisory so the user sees the WHOLE picture in one run, not just the first
# failure. POSIX sh (dash) has no pipefail → every check reports its own result explicitly.
set -u

# Defaults mirror the hook (distill-session.py): DRUDGE_URL + OMB_HOME + the marker dir,
# so the diagnostic inspects exactly what the hook writes to.
DRUDGE_URL="${DRUDGE_URL:-http://127.0.0.1:7700}"
OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
OMB_HOME="${OMB_HOME:-$HOME/oh-my-boring}"
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

# (a) drudge /health — the deterministic write gate the hook POSTs `remember` to.
if [ "$(curl -s -o /dev/null -w '%{http_code}' -m5 "$DRUDGE_URL/health" 2>/dev/null)" = "200" ]; then
    ok "drudge /health 200 ($DRUDGE_URL) — write door reachable"
else
    bad "drudge /health unreachable ($DRUDGE_URL) — distilled sessions are being DROPPED"
    drudge_down=1
fi

# (b) Ollama — the local LLM that generates the curated note before remember is called.
if curl -sf -m5 "$OLLAMA_HOST/api/tags" >/dev/null 2>&1; then
    ok "Ollama reachable ($OLLAMA_HOST)"
else
    bad "Ollama unreachable ($OLLAMA_HOST) — distillation can't generate notes (set OLLAMA_HOST?)"
fi

# (c) Container status — surface crash-loops the HTTP probes alone would miss.
if command -v docker >/dev/null 2>&1; then
    ps=$(cd "$OMB_HOME" 2>/dev/null && docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null)
    if [ -n "$ps" ]; then
        ok "containers (docker compose ps in $OMB_HOME):"
        printf '%s\n' "$ps" | sed 's/^/    /'
        printf '%s\n' "$ps" | grep -qi 'restarting' \
            && bad "a container is RESTARTING (crash-loop) — check 'make logs'"
    else
        bad "no compose containers found in $OMB_HOME (stack not started? set OMB_HOME?)"
    fi
else
    bad "docker not on PATH — can't inspect container status"
fi

# (d1) Newest distilled note — proof the write door produced output. The hook writes notes
# as vault/wiki/wiki-*.md, so the newest mtime is the last successful distillation.
note=$(newest "$OMB_HOME/vault/wiki/wiki-*.md")
if [ -n "$note" ]; then
    ok "newest distilled note: $(mtime_human "$note")"
    echo "    $note"
else
    bad "no distilled notes in $OMB_HOME/vault/wiki/ — nothing written yet"
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
