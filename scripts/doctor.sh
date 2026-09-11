#!/bin/sh
# Diagnose the self-augmentation write-door. The distill SessionEnd hook runs async
# and logs failures to stderr, but a down stack or bad BORING_URL can still drop sessions.
# This surfaces that signal: a clear OK/✗ per dependency plus proof the write-door is
# actually firing (newest distilled note + newest SessionEnd hook marker).
#   make doctor   or   ./scripts/doctor.sh
#
# Mostly read-only: probes /health, provider/model shape, compose status, mtimes,
# and Codex queue/worker status. The marker-dir check writes and removes one
# owner-only sentinel file so strict readiness can prove queue state is writable.
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
# Shared /health readiness judge (db_healthy), so every gate asks the same question.
DOCTOR_LIB_DIR="$(cd "$(dirname "$0")/lib" && pwd)"
# shellcheck source=lib/drudge_health_readiness.sh
. "$DOCTOR_LIB_DIR/drudge_health_readiness.sh"

EVENT_LOG="$BORING_HOME/agents/shared/event_log.py"
doctor_run_id="doctor-$(date +%Y%m%dT%H%M%S)-$$"
doctor_started_at="$(date +%s)"

# Track which checks failed so --fix knows what to repair.
failed_env=0
failed_hooks=0
failed_engine=0
failed_ollama=0
failed_containers=0
failed_note=0
failed_marker=0
failed_midpoint=0
failed_codex=0
failed_resolution=0
failed_freshness=0
failed_maintenance=0

ok()   { echo "✓ $1"; }
bad()  { echo "✗ $1"; }
# Non-fatal: something worth acting on that does not mean the write door is shut.
warn() { echo "! $1"; }

log_doctor_event() {
    status="$1"
    event_name="doctor"
    [ "$STRICT" -eq 1 ] && event_name="readiness"
    duration_s="$(($(date +%s) - doctor_started_at))"
    if [ -f "$EVENT_LOG" ]; then
        if ! python3 "$EVENT_LOG" --record doctor "$event_name" "$status" \
            --field "run_id=$doctor_run_id" \
            --field "strict=$STRICT" \
            --field "duration_s=$duration_s" \
            --field "failed_env=$failed_env" \
            --field "failed_hooks=$failed_hooks" \
            --field "failed_engine=$failed_engine" \
            --field "failed_ollama=$failed_ollama" \
            --field "failed_containers=$failed_containers" \
            --field "failed_note=$failed_note" \
            --field "failed_marker=$failed_marker" \
            --field "failed_codex=$failed_codex" \
            --field "failed_resolution=$failed_resolution" \
            --field "failed_freshness=$failed_freshness" \
            --field "failed_maintenance=$failed_maintenance" \
            --field "drudge_down=$drudge_down"; then
            echo "⚠ doctor event log write failed" >&2
        fi
    fi
}

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

mtime_epoch() {
    stat -c '%Y' "$1" 2>/dev/null && return 0   # GNU coreutils
    stat -f '%m' "$1" 2>/dev/null && return 0   # BSD / macOS
    echo 0
}

int_seconds() {
    raw="$1"
    case "$raw" in
      ''|*[!0-9]*)
        return 1
        ;;
    esac
    if [ "$raw" -gt 0 ] 2>/dev/null; then
        printf '%s\n' "$raw"
        return 0
    fi
    return 1
}

is_ignored_marker() {
    marker_name="${1##*/}"
    case "$marker_name" in
      codex-rollout-*) return 0 ;;
    esac
    return 1
}

resolve_docker() {
    if [ -n "${DOCKER_BIN:-}" ] && [ -x "$DOCKER_BIN" ]; then
        printf '%s\n' "$DOCKER_BIN"
        return 0
    fi
    if command -v docker >/dev/null 2>&1; then
        command -v docker
        return 0
    fi
    for candidate in /opt/homebrew/bin/docker /usr/local/bin/docker /Applications/Docker.app/Contents/Resources/bin/docker; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
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
    # Match on `hooks/<script>.py`, not on a full path. install.sh may register the tilde
    # form (`~/oh-my-boring/hooks/...`), which the shell expands at run time but which a
    # literal grep for the expanded path never matches — the hooks fire while doctor calls
    # them missing. A false ✗ here is worse than no check: it teaches the reader to skip
    # doctor, and this one shipped that way while both hooks were working.
    # Deliberately not anchored on the install directory name either: it is `oh-my-boring`
    # on this host, a symlink target elsewhere, and `boring` in the doctor test fixture.
    if grep -q "hooks/distill-session.py" "$settings" && grep -q "hooks/recall.py" "$settings"; then
        ok "Claude Code hooks wired in $settings"
    else
        bad "Claude Code hooks missing in $settings — run install.sh"; failed_hooks=1
    fi
else
    bad "Claude Code settings not found at $settings — run install.sh"; failed_hooks=1
fi

# (a2) engine /health — the deterministic write gate the hook POSTs `remember` to.
# 200 only proves the engine answers; db_healthy proves it can still write. During the
# 2026-07-25 outage every write failed for days while /health kept returning 200.
if [ "$(curl -s -o /dev/null -w '%{http_code}' -m5 "$BORING_URL/health" 2>/dev/null)" = "200" ]; then
    if check_drudge_db_healthy "$BORING_URL"; then
        ok "engine /health 200 ($BORING_URL) — write door reachable"
        # (a2b) deploy drift — merging is not deploying. On 2026-08-14 ten merged PRs were
        # not running: the image predated them and nobody noticed for two days. Compare what
        # the container says it was built from against the checkout, by sha and not by
        # timestamp — a timestamp answers "when", never "what", and gets timezones wrong.
        running_sha="$(curl -sf -m5 "$BORING_URL/health" 2>/dev/null \
            | sed -n 's/.*"build_sha":"\([0-9a-f]*\)".*/\1/p')"
        head_sha="$(git -C "$BORING_HOME" rev-parse HEAD 2>/dev/null || true)"
        if [ -z "$running_sha" ]; then
            warn "engine reports no build_sha — image predates the stamp, or was built without it. Rebuild with 'make build' to make drift detectable."
        elif [ -z "$head_sha" ]; then
            warn "cannot read HEAD at $BORING_HOME — drift not checked (engine is running ${running_sha%%????????*}…)"
        elif [ "$running_sha" = "$head_sha" ]; then
            ok "engine runs the checked-out commit ($(printf '%.8s' "$running_sha"))"
        elif [ -z "$(git -C "$BORING_HOME" diff --name-only "$running_sha" HEAD -- drudge/ 2>/dev/null)" ] \
             && git -C "$BORING_HOME" cat-file -e "$running_sha^{commit}" 2>/dev/null; then
            # The image is built from `drudge/`. Commits that touch only Python, hooks or docs
            # cannot make it stale, and calling them drift is how a failing line stops being read
            # — the same way the warning this check replaced stopped being read. Seven such
            # commits on 2026-09-07 had this reporting a stale engine with no Rust changed.
            # The guard is `cat-file -e`: an unknown sha means the comparison could not be made,
            # and "could not check" must fall through to the failure below, never to an ok.
            ok "engine matches the checkout for drudge/ (image $(printf '%.8s' "$running_sha"), HEAD $(printf '%.8s' "$head_sha"); later commits touch no Rust)"
        else
            # Was a warning until 2026-08-25, and the warning did not work: #221 shipped the
            # only instrument that can measure injection precision, sat merged-but-not-running,
            # and collected zero labels while readiness stayed green. A gate that reports the
            # product is stale without failing teaches the reader to scroll past it. Readiness
            # answers "can I trust tomorrow's briefing" — an engine running older code than the
            # checkout cannot be trusted to, so this is a failure, not a note.
            bad "DEPLOY DRIFT — engine runs $(printf '%.8s' "$running_sha"), checkout is at $(printf '%.8s' "$head_sha"). Merged work is not live until 'make build' + restart."
            failed_engine=1
        fi

        # (a2b2) the host CLI binary. `make build` builds the image; the binary that
        # `schedule-maintenance.sh` runs daily for code-sync is a separate artifact that only
        # `cargo build --release` refreshes, and the drift check above asks the engine — so a
        # stale host binary ran old logic against live data with every gate green. Checked only
        # when it exists: a wiki-first install never builds it.
        host_cli="$BORING_HOME/drudge/target/release/drudge"
        if [ -x "$host_cli" ] && [ -n "$head_sha" ]; then
            host_sha="$("$host_cli" version 2>/dev/null | tail -1)"
            case "$host_sha" in
                "$head_sha") ok "host CLI built from the checked-out commit ($(printf '%.8s' "$host_sha"))" ;;
                unstamped|"") warn "host CLI reports no build stamp — rebuild with 'cargo build --release' to make its drift detectable" ;;
                *)
                    # Same blind spot the engine check had until #290: this binary is built from
                    # `drudge/`, so a commit touching only Python, hooks or docs cannot age it.
                    # Fixing one of the two and not the other is how one value in two places
                    # goes wrong again — the very pattern §8 D8 names. `cat-file -e` guards the
                    # comparison: an unknown sha means it could not be made, and that falls
                    # through to the failure rather than to an ok.
                    if [ -z "$(git -C "$BORING_HOME" diff --name-only "$host_sha" HEAD -- drudge/ 2>/dev/null)" ] \
                       && git -C "$BORING_HOME" cat-file -e "$host_sha^{commit}" 2>/dev/null; then
                        ok "host CLI matches the checkout for drudge/ (binary $(printf '%.8s' "$host_sha"), HEAD $(printf '%.8s' "$head_sha"); later commits touch no Rust)"
                    else
                        bad "HOST CLI DRIFT — $host_cli was built from $(printf '%.8s' "$host_sha"), checkout is at $(printf '%.8s' "$head_sha"). Daily code-sync runs that older binary."
                        failed_engine=1
                    fi
                    ;;
            esac
        fi

        # (a2c) config the running binary cannot read. boring.json is bind-mounted, so an edit is
        # visible to the container immediately but is only *parsed* at startup — a config written
        # for a newer binary sits harmless until something restarts the engine, and then it is a
        # crash loop under `restart: unless-stopped`. That is not hypothetical: adding python and
        # shell code_index sources before the binary that understood them was deployed took the
        # engine down on 2026-08-25, days after the edit. Ask the running binary to parse what is
        # on disk now, while it is still up and can be asked.
        if [ -n "${DOCTOR_COMPOSE:-$(command -v docker)}" ] && [ "${BORING_SKIP_CONFIG_PROBE:-0}" != 1 ]; then
            if docker compose exec -T boring-drudge drudge audit >/dev/null 2>&1; then
                ok "running engine can parse the config on disk"
            else
                config_err="$(docker compose exec -T boring-drudge drudge audit 2>&1 | head -2 | tr '\n' ' ')"
                bad "the running engine CANNOT parse boring.json — the next restart will crash-loop, not start: $config_err"
                failed_engine=1
            fi
        fi
    else
        bad "engine /health 200 but postgres is degraded ($BORING_URL) — distilled sessions are being DROPPED"; failed_engine=1
        drudge_down=1
    fi
else
    bad "engine /health unreachable ($BORING_URL) — distilled sessions are being DROPPED"; failed_engine=1
    drudge_down=1
fi

# (b) LLM/provider contract — the model that generates notes and the embedding shape
# used by vector mode. This delegates to the SSOT verifier so readiness cannot drift
# into a weaker endpoint-only check.
BORING="${BORING_CONFIG:-$BORING_HOME/boring.json}"
VERIFY_LLM="$BORING_HOME/scripts/verify-llm.sh"
if [ -x "$VERIFY_LLM" ]; then
    verify_out=$(BORING_HOME="$BORING_HOME" BORING_CONFIG="$BORING" "$VERIFY_LLM" 2>&1)
    verify_rc=$?
    if [ "$verify_rc" -eq 0 ]; then
        ok "LLM provider/model/embed contract verified"
    else
        bad "LLM provider/model/embed contract failed — run: make verify-llm"
        failed_ollama=1
    fi
    printf '%s\n' "$verify_out" | while IFS= read -r line; do
        [ -n "$line" ] && echo "    $line"
    done
else
    bad "verify-llm not found/executable at $VERIFY_LLM"; failed_ollama=1
fi

# (c) Container status — surface crash-loops the HTTP probes alone would miss.
if docker_bin="$(resolve_docker)"; then
    compose_label="$docker_bin compose"
    if "$docker_bin" compose version >/dev/null 2>&1; then
        ps=$(cd "$BORING_HOME" 2>/dev/null && "$docker_bin" compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null)
    elif command -v docker-compose >/dev/null 2>&1; then
        compose_label="docker-compose"
        ps=$(cd "$BORING_HOME" 2>/dev/null && docker-compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null)
    else
        ps=""
        bad "docker found at $docker_bin but Docker Compose is unavailable"; failed_containers=1
    fi
    if [ -n "$ps" ]; then
        ok "containers ($compose_label ps in $BORING_HOME):"
        printf '%s\n' "$ps" | sed 's/^/    /'
        if printf '%s\n' "$ps" | grep -qi 'restarting'; then
            bad "a container is RESTARTING (crash-loop) — check 'make logs'"; failed_containers=1
        fi
    else
        bad "no compose containers found in $BORING_HOME (stack not started? set BORING_HOME?)"; failed_containers=1
    fi
else
    bad "docker not found — can't inspect container status (set DOCKER_BIN or install Docker CLI)"; failed_containers=1
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

# (d2b) Marker dir and stale marker health — proof the queue can still progress.
if mkdir -p "$MARK_DIR" 2>/dev/null; then
    marker_probe="$MARK_DIR/.doctor-write-test.$$"
    if (umask 077; : > "$marker_probe") 2>/dev/null; then
        rm -f "$marker_probe"
        marker_writable=1
    else
        marker_writable=0
    fi
else
    marker_writable=0
fi

pending_ttl_raw="${BORING_READINESS_PENDING_TTL:-${INGEST_PENDING_TTL:-1800}}"
if pending_ttl="$(int_seconds "$pending_ttl_raw")"; then
    :
else
    pending_ttl=0
    bad "invalid pending marker TTL '$pending_ttl_raw' — set BORING_READINESS_PENDING_TTL or INGEST_PENDING_TTL to a positive integer second count"
    failed_marker=1
fi
retry_ttl_raw="${BORING_READINESS_RETRY_TTL:-${INGEST_RETRY_TTL:-$pending_ttl_raw}}"
if retry_ttl="$(int_seconds "$retry_ttl_raw")"; then
    :
else
    retry_ttl=0
    bad "invalid retry marker TTL '$retry_ttl_raw' — set BORING_READINESS_RETRY_TTL or INGEST_RETRY_TTL to a positive integer second count"
    failed_marker=1
fi
now_s="$(date +%s)"
stale_pending=0
stale_retry=0
dead_letter=0

for marker_file in "$MARK_DIR"/*.pending; do
    [ -e "$marker_file" ] || continue
    is_ignored_marker "$marker_file" && continue
    marker_age=$((now_s - $(mtime_epoch "$marker_file")))
    [ "$marker_age" -lt 0 ] && marker_age=0
    if [ "$pending_ttl" -gt 0 ] && [ "$marker_age" -gt "$pending_ttl" ]; then
        stale_pending=$((stale_pending + 1))
    fi
done
for marker_file in "$MARK_DIR"/*.retry; do
    [ -e "$marker_file" ] || continue
    is_ignored_marker "$marker_file" && continue
    marker_age=$((now_s - $(mtime_epoch "$marker_file")))
    [ "$marker_age" -lt 0 ] && marker_age=0
    if [ "$retry_ttl" -gt 0 ] && [ "$marker_age" -gt "$retry_ttl" ]; then
        stale_retry=$((stale_retry + 1))
    fi
done
for marker_file in "$MARK_DIR"/*.dead "$MARK_DIR"/*.dead-letter; do
    [ -e "$marker_file" ] || continue
    is_ignored_marker "$marker_file" && continue
    dead_letter=$((dead_letter + 1))
done

echo "marker_health writable=$marker_writable stale_pending=$stale_pending stale_retry=$stale_retry dead_letter=$dead_letter pending_ttl_s=$pending_ttl retry_ttl_s=$retry_ttl dir=$MARK_DIR"
if [ "$marker_writable" -ne 1 ]; then
    bad "marker dir is not writable — autonomous ingest cannot persist queue state"; failed_marker=1
fi
if [ "$stale_pending" -gt 0 ] || [ "$stale_retry" -gt 0 ] || [ "$dead_letter" -gt 0 ]; then
    bad "marker health failed — stale pending/retry or dead-letter markers need attention"; failed_marker=1
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

# (d4) Recent resolution quality failures — these mean the write-door is reachable but
# the distilled note was too shallow even after the one repair attempt.
event_log_probe="$BORING_HOME/agents/shared/event_log.py"
if [ -f "$event_log_probe" ]; then
    if BORING_EVENT_LOG="${BORING_EVENT_LOG:-$HOME/.cache/oh-my-boring/events.ndjson}" python3 "$event_log_probe" --recent-resolution-failures --max 3; then
        ok "no recent resolution quality failures"
    else
        bad "recent resolution quality failures found — inspect make events or the BORING_EVENT_LOG fallback before briefing"; failed_resolution=1
    fi
else
    bad "event log probe not found at $event_log_probe"; failed_resolution=1
fi

# (d4b) Gate staleness. A gate that stops running looks exactly like a gate that passes:
# eval_graphrag_gate emitted twenty `ok` rows over four days in July, then went silent for five
# weeks, and those stale rows were later read as evidence the graph was still covered. Silence
# is the failure this names.
if [ -f "$event_log_probe" ]; then
    if BORING_EVENT_LOG="${BORING_EVENT_LOG:-$HOME/.cache/oh-my-boring/events.ndjson}" python3 "$event_log_probe" --stale-gates; then
        ok "watched gates ran recently"
    else
        bad "a watched gate has gone stale — it is not failing, it stopped running"; failed_resolution=1
    fi
fi

# (d6) Code index freshness. last_synced_at older than BORING_CODE_INDEX_MAX_DAYS (default 7)
# is a warning, not a strict failure — the index can still be read; it is just no longer current.
max_days="${BORING_CODE_INDEX_MAX_DAYS:-7}"
stale=$("$BORING_HOME/drudge/target/release/drudge" code-status 2>/dev/null | while IFS= read -r line; do
    ts=$(printf '%s' "$line" | sed -n 's/.* synced=\([0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}T[0-9]\{2\}:[0-9]\{2\}:[0-9]\{2\}\)Z.*/\1/p')
    [ -n "$ts" ] || continue
    epoch=$(date -d "${ts}Z" +%s 2>/dev/null || date -j -f '%Y-%m-%dT%H:%M:%S' "$ts" +%s 2>/dev/null || echo 0)
    age=$(( (now_s - epoch) / 86400 ))
    [ "$age" -gt "$max_days" ] && printf '%s ' "$(printf '%s' "$line" | awk '{print $1}')"
done)
if [ -z "$stale" ]; then
    ok "code index is fresh (within ${max_days} days)"
else
    warn "code index is stale (> ${max_days} days): $stale"
fi

# (d5) Freshness window for briefing content. Existence alone is too weak: a green
# stack with stale notes still cannot answer "can I trust tomorrow morning's briefing?"
note_max_hours_raw="${BORING_READINESS_NOTE_MAX_HOURS:-48}"
if note_max_hours="$(int_seconds "$note_max_hours_raw")"; then
    note_max_s=$((note_max_hours * 3600))
else
    note_max_s=0
    bad "invalid note freshness window '$note_max_hours_raw' — set BORING_READINESS_NOTE_MAX_HOURS to a positive integer hour count"
    failed_freshness=1
fi
if [ -n "$note" ] && [ "$note_max_s" -gt 0 ]; then
    note_age=$((now_s - $(mtime_epoch "$note")))
    [ "$note_age" -lt 0 ] && note_age=0
    echo "note_freshness age_s=$note_age max_s=$note_max_s path=$note"
    if [ "$note_age" -gt "$note_max_s" ]; then
        bad "newest note is stale for briefing freshness — run/verify ingestion before relying on briefing"
        failed_freshness=1
    fi
fi

# (d5c) Double-recorded injections. The recall hook was registered twice under two spellings of
# the same path, so every prompt wrote two ledger rows (#245). The uptake rate is blind to that —
# numerator and denominator both double — but `total_prompts` is a pre-registered sample floor
# (docs/PRD.md §2), and a floor met at half the evidence it names is not the floor registered.
# Any future double-fire (another adapter, a hand-added hook) lands the same way and is invisible
# everywhere else.
ledger_probe="$BORING_HOME/agents/shared/uptake_core.py"
if [ -f "$ledger_probe" ] && [ "${BORING_SKIP_LEDGER_PROBE:-0}" != 1 ]; then
    if dup_out="$(python3 "$ledger_probe" --duplicate-injections 2>/dev/null)"; then
        ok "injection ledger has no double-recorded prompts (${dup_out##*total_rows=})"
    else
        bad "DOUBLE-RECORDED INJECTIONS — $dup_out. One prompt logged twice means the recall hook fired twice; the uptake rate hides it while the sample floor counts double."
        failed_hooks=1
    fi
fi

wiring="$BORING_HOME/agents/shared/agent_wiring.py"
# (a2c) A compact that never finishes. `REINDEX CONCURRENTLY claim` had been failing on every
# run against a container's default 64 MB of /dev/shm (#304) and the only trace was one
# `eprintln!` in the docker log — /health did not carry it, this file did not ask, and the
# scheduler stamped its window as though the run had worked. Weeks of a maintenance job doing
# nothing, with every gate green.
#
# The engine now keeps the last failure and omits the field entirely once a run succeeds, so
# the field's presence is the alarm and a healthy engine reads exactly as it did before.
if [ "$drudge_down" -eq 0 ]; then
    compact_fail="$(curl -sf -m5 "$BORING_URL/health" 2>/dev/null \
        | sed -n 's/.*"compact_failure":{\([^}]*\)}.*/\1/p')"
    if [ -z "$compact_fail" ]; then
        ok "maintenance: last compact finished (no compact_failure on /health)"
    else
        compact_n="$(printf '%s' "$compact_fail" | sed -n 's/.*"consecutive":\([0-9]*\).*/\1/p')"
        compact_at="$(printf '%s' "$compact_fail" | sed -n 's/.*"at":"\([^"]*\)".*/\1/p')"
        compact_err="$(printf '%s' "$compact_fail" | sed -n 's/.*"error":"\(.*\)"$/\1/p')"
        bad "COMPACT FAILING — ${compact_n:-?} consecutive, last at ${compact_at:-unknown}: ${compact_err:-no message}. VACUUM/REINDEX/prune/orphan-GC are all not running; the tables only grow and invalid index leftovers accumulate."
        failed_maintenance=1
    fi
fi

# (a2d) The repo's own gate, unwired. `.pre-commit-config.yaml` and the `pre-commit` binary were
# both present on 2026-09-09 while `.git/hooks/` held nothing but samples, so `scripts/guard.sh`
# was something a person remembered to run and then read the result of by hand. That is where the
# day's worst mistake came from: guard printed `exit=1` on the first line of its log, the wrapper
# printed `exited with code 0` on the last, and the last line is what got read and believed.
#
# A gate whose verdict a human carries is not an edge. This checks that the edge exists.
if [ -f "$BORING_HOME/.pre-commit-config.yaml" ]; then
    if [ -x "$BORING_HOME/.git/hooks/pre-commit" ]; then
        ok "pre-commit hook installed — guard runs on commit, not on memory"
    else
        bad "PRE-COMMIT HOOK MISSING — .pre-commit-config.yaml exists but .git/hooks/pre-commit does not. Run 'pre-commit install'. Until then every gate result is one a person has to read and carry."
        failed_maintenance=1
    fi
fi

# (d5e) The same hook registered twice, read from the configs rather than from the damage.
# (d5c) below finds a hook that FIRED twice, which means it only speaks after the duplicate has
# written rows, and it stays silent forever for an adapter registered twice that never runs. Kimi
# was in exactly that state on 2026-09-02 — both hooks under two spellings of the same path, and
# no ledger evidence at all because kimi had produced no sessions.
if [ -f "$wiring" ]; then
    if reg_out="$(python3 "$wiring" --check-registrations --boring-home "$BORING_HOME" 2>&1)"; then
        ok "no hook of ours is registered twice (${reg_out##*checked_configs=})"
    else
        bad "HOOK REGISTERED TWICE — $reg_out. Every prompt fires it twice, which leaves the rate unchanged and halves the evidence behind the sample floor (PRD §2)."
        failed_hooks=1
    fi
fi

# (d5f) The window's one-time progress gate (docs/PRD.md §2). Registered there as prose on
# 2026-09-02, which on its own fires nothing — "written in the PRD, absent from the code" is the
# same class as the seven merges that never reached production.
#
# The dates and the floor live in `verdict_core` (the PRD-transcription test keeps them honest);
# this only maps exit codes, so the shell owns no calendar. `failed_midpoint` is deliberately NOT
# `failed_hooks`: `--fix` runs `fix_hooks` on that flag and would "repair" perfectly healthy hook
# registrations while swallowing the signal.
verdict_cli="$BORING_HOME/scripts/uptake-verdict.py"
if [ -f "$verdict_cli" ] && [ "${BORING_SKIP_MIDPOINT:-0}" != 1 ]; then
    midpoint_out="$(python3 "$verdict_cli" --midpoint 2>&1)"
    # Exit codes only. This used to read the message text to tell "floor met" from "not due", so
    # the wording of a Korean sentence was the only thing separating them — rephrasing it would
    # have silently turned one into the other.
    case $? in
        0) ok "midpoint: ${midpoint_out##*\[midpoint\] }" ;;
        5) : ;;   # outside the gate's window: silent by contract, one-time gate
        3) bad "MIDPOINT SHORTFALL — $midpoint_out"; failed_midpoint=1 ;;
        4) warn "midpoint undetermined — $midpoint_out. Not a shortfall in the sample; a shortfall in what can be read." ;;
        *) warn "midpoint check could not run — $midpoint_out" ;;
    esac
fi

# (d5d) Can the uptake detector see a use it is handed on a plate? Treatment and control both
# sitting at zero is equally the signature of a channel nobody used and of a scorer that sees
# nothing, and the rates cannot separate them — so docs/PRD.md §2 refuses a "not working" reading
# until sensitivity is shown. This is that proof, run daily rather than once on verdict day, so a
# blind stretch is dated instead of discovered at the end. It scores a phrase from a really
# injected hit against a synthetic assistant turn; it writes nothing.
if [ -f "$ledger_probe" ] && [ "${BORING_SKIP_LEDGER_PROBE:-0}" != 1 ]; then
    if probe_out="$(python3 "$ledger_probe" --sensitivity-probe 2>/dev/null)"; then
        case "$probe_out" in
            *uptake_sensitivity=ok*) ok "uptake detector is sensitive (${probe_out#*reason=})" ;;
            *) warn "uptake sensitivity undetermined — $probe_out. Not a fault; the ledger carries nothing to probe with yet." ;;
        esac
    else
        bad "UPTAKE DETECTOR IS BLIND — $probe_out. An injected phrase handed back verbatim was not counted, so a zero uptake rate says nothing about the channel (PRD §2)."
        failed_hooks=1
    fi
fi

# (d5e) The other half of §2's instrument check. (d5d) asks whether the detector can see a use;
# this asks whether it sees uses that are not there — each transcript scored against a *different*
# session's ledger hits, which it never received. Whatever comes back is what coincidence alone
# produces on this corpus, and if it reaches the treatment rate the measure is reading topic
# overlap rather than use. Measured 2026-09-08: 0 of 2145.
#
# Ran by hand on 2026-09-07 and nowhere else, which is the arrangement that fails on the day
# nobody is at the keyboard — the same reason (d5d) is a daily line and not a verdict-day ritual.
if [ -f "$ledger_probe" ] && [ "${BORING_SKIP_LEDGER_PROBE:-0}" != 1 ]; then
    if check_out="$(python3 "$ledger_probe" --self-check 2>/dev/null)"; then
        case "$check_out" in
            *uptake_self_check=ok*) ok "uptake self-check ran (${check_out#*cross=})" ;;
            *) warn "uptake self-check undetermined — $check_out. Not a fault; too few sessions or transcripts to pair." ;;
        esac
    else
        bad "UPTAKE SELF-CHECK FAILED — $check_out. Hits a session never received are being counted as used, so the treatment rate is measuring overlap (PRD §2)."
        failed_hooks=1
    fi
fi

# (d5g) (d5d) asked through `transcript.extract` rather than around it — see #311.
if [ -f "$ledger_probe" ] && [ "${BORING_SKIP_LEDGER_PROBE:-0}" != 1 ]; then
    if pipe_out="$(python3 "$ledger_probe" --pipeline-probe 2>/dev/null)"; then
        case "$pipe_out" in
            *uptake_pipeline_probe=ok*) ok "uptake survives the reader (${pipe_out#*reason=})" ;;
            *) warn "uptake pipeline probe undetermined — $pipe_out. Not a fault; no ledger session has a transcript to read." ;;
        esac
    else
        bad "UPTAKE PIPELINE BLIND — $pipe_out. A use the detector can match does not survive transcript.extract, so every uptake score is 0 for a reason that has nothing to do with the channel (PRD §2)."
        failed_hooks=1
    fi
fi

# (d5b) The briefing scripts hermes actually runs. `make build` redeploys the engine image, but
# the scripts hermes executes live in ~/.hermes/scripts and only the installer copies them there
# — so merging a briefing change does not deliver it. Measured on 2026-08-26: the installed
# copies were twelve days old, five merged PRs had never reached the 08:01 cron, and every other
# gate was green the whole time. The installer owns the list of files it writes, so ask it
# instead of keeping a second list here that can fall behind exactly the same way.
hermes_scripts_dir="$HOME/.hermes/scripts"
if [ -d "$hermes_scripts_dir" ] && [ -f "$wiring" ]; then
    script_list="$(mktemp)"
    if python3 "$wiring" --list-hermes-scripts --boring-home "$BORING_HOME" >"$script_list" 2>/dev/null; then
        hermes_drift=0
        hermes_total=0
        while IFS= read -r src; do
            [ -n "$src" ] || continue
            hermes_total=$((hermes_total + 1))
            installed="$hermes_scripts_dir/$(basename "$src")"
            if [ ! -f "$installed" ]; then
                bad "hermes never received $(basename "$src") — the cron briefing imports it and dies on ImportError"
                hermes_drift=$((hermes_drift + 1))
            elif ! cmp -s "$src" "$installed"; then
                bad "DEPLOY DRIFT — $installed differs from the checkout. Merging is not deploying; the cron briefing still runs the old copy."
                hermes_drift=$((hermes_drift + 1))
            fi
        done <"$script_list"
        if [ "$hermes_drift" -eq 0 ]; then
            ok "hermes briefing scripts match the checkout ($hermes_total files)"
        else
            failed_hooks=1
        fi
    else
        warn "cannot resolve the hermes script list — '$wiring --list-hermes-scripts' failed, so deploy drift is not checked"
    fi
    rm -f "$script_list"
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
    failures="${failed_env}${failed_hooks}${failed_engine}${failed_ollama}${failed_containers}${failed_note}${failed_marker}${failed_codex}${failed_resolution}${failed_freshness}${failed_midpoint}${failed_maintenance}"
    if [ "$failures" = "000000000000" ]; then
        ok "readiness: all doctor checks passed — briefing/write-door dependencies are ready."
        log_doctor_event ok
        exit 0
    fi
    bad "readiness: one or more doctor checks failed — do not rely on tomorrow's briefing until fixed."
    log_doctor_event failed
    exit 1
fi

if [ "$drudge_down" -eq 0 ]; then
    ok "doctor: drudge is up — the write door is open."
    log_doctor_event ok
    exit 0
fi
bad "doctor: drudge is DOWN — sessions are silently dropped until it's back up."
log_doctor_event failed
exit 1
