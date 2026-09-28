#!/bin/sh
# Register and run a card, unattended — same shape as schedule-maintenance.sh, one launchd
# label per unattended job. Two jobs share every mechanism (install/uninstall/status, the
# failure notice, the log markers): `card` is the morning card, `weekly` the weekly card.
#
#   ./scripts/schedule-card.sh run [job]        # execute now, unattended (job default: card)
#   ./scripts/schedule-card.sh install [job]    # register the job (macOS launchd)
#   ./scripts/schedule-card.sh uninstall [job]  # remove the registration
#   ./scripts/schedule-card.sh status [job]     # show registration state + last log line
set -u

BORING_HOME="${BORING_HOME:-$HOME/oh-my-boring}"
DOOR_URL="${BORING_DOOR_URL:-http://127.0.0.1:7710}"

JOB="${2:-card}"
case "$JOB" in
    card)
        LABEL="com.ohmyboring.morning-card"
        RUN_MARK="morning card"
        SCRIPT="agents/slack/card.py"
        NOTICE="오늘 아침 카드를 못 보냈다"
        NEEDS_DOOR=1
        SCHEDULE_PLIST='
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>0</integer>'
        ;;
    weekly)
        LABEL="com.ohmyboring.weekly-card"
        RUN_MARK="weekly card"
        SCRIPT="agents/slack/weekly_card.py"
        NOTICE="이번 주 주간 카드를 못 보냈다"
        NEEDS_DOOR=0
        SCHEDULE_PLIST='
        <key>Weekday</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>'
        ;;
    *)
        echo "✗ unknown job: $JOB (want: card or weekly)" >&2
        exit 1
        ;;
esac
LOG="/tmp/${LABEL}.log"

usage() {
    cat <<EOF
Usage: $0 {run|install|uninstall|status} [job]

  job       card (default) — the morning card, every day at 08:00, needs the door up
            weekly — the weekly briefing card, Monday 09:00, reads the vault first

  run       Run the job once, unattended. The card fails visibly (exit 2) if the
            door ($DOOR_URL) is not answering — this script never starts one. The
            weekly has no door pre-check: its signal is the vault, and a broken
            engine is the poster's own failure, not a reason to skip a quiet week.
  install   Register the job (macOS launchd).
  uninstall Remove the registration.
  status    Show whether the job is registered, and its last log line.
EOF
}

# The failure notice and the .env load live in scripts/lib/ — one copy both schedulers
# source; copied notices and loaders drift.
LIB_DIR="$(cd "$(dirname "$0")/lib" && pwd)"
# shellcheck source=lib/dot_env.sh
. "$LIB_DIR/dot_env.sh"
# shellcheck source=lib/slack_notify.sh
. "$LIB_DIR/slack_notify.sh"

run_job() {
    cd "$BORING_HOME" || { echo "✗ cannot cd to $BORING_HOME"; exit 1; }
    # launchd runs this with none of the interactive shell's env — the shared loader
    # (scripts/lib/dot_env.sh) sources ./.env the same way `make card` does.
    load_dot_env
    if [ "$NEEDS_DOOR" = "1" ]; then
        door_url="${BORING_DOOR_URL:-$DOOR_URL}"
        if ! curl -sf "${door_url}/health" >/dev/null 2>&1; then
            echo "✗ door not answering at ${door_url}/health — start it (make door-up) before the card can run" >&2
            notify_failure 2 "문(${door_url})이 응답하지 않음"
            exit 2
        fi
    fi
    echo "=== ${RUN_MARK} started at $(date '+%Y-%m-%dT%H:%M:%S%z') ==="
    # launchd's own PATH has no notion of Homebrew (python3 resolves to the PATH-less
    # system stub, which has no langgraph) — PYTHON3 is set in the plist's own
    # EnvironmentVariables at install time so this never depends on launchd's PATH.
    err_log=$(mktemp)
    "${PYTHON3:-python3}" "$SCRIPT" 2>"$err_log"
    status=$?
    cat "$err_log" >&2
    if [ "$status" -ne 0 ]; then
        reason=$(sed -e '/^[[:space:]]/d' -e '/^$/d' "$err_log" | tail -n 1 | cut -c1-200)
        notify_failure "$status" "${reason:-사유 없음}"
    fi
    rm -f "$err_log"
    echo "=== ${RUN_MARK} finished at $(date '+%Y-%m-%dT%H:%M:%S%z') (exit $status) ==="
    exit "$status"
}

escaped_boring_home() {
    printf '%s' "$BORING_HOME" | sed 's/ /\\ /g'
}

install_macos() {
    plist="$HOME/Library/LaunchAgents/${LABEL}.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    # Resolved once, here, in the installer's own interactive PATH — launchd's own PATH
    # never sees Homebrew, so the plist carries the absolute interpreter instead of a name.
    python3_bin="$(command -v python3 || true)"
    if [ -z "$python3_bin" ]; then
        echo "✗ no python3 on PATH — cannot resolve the interpreter to pin into the plist"
        exit 1
    fi
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>cd $(escaped_boring_home) &amp;&amp; ./scripts/schedule-card.sh run ${JOB}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHON3</key>
        <string>${python3_bin}</string>
        <key>BORING_HOME</key>
        <string>${BORING_HOME}</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict>${SCHEDULE_PLIST}
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>${LOG}</string>
    <key>StandardErrorPath</key>
    <string>${LOG}</string>
</dict>
</plist>
EOF
    # launchctl load never replaces an already-loaded job — a reinstall must boot out the
    # old definition first, then bootstrap the new plist, and verify what actually loaded.
    if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
        if ! launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
            echo "✗ bootout failed for gui/$(id -u)/${LABEL}"
            exit 1
        fi
    fi
    if ! launchctl bootstrap "gui/$(id -u)" "$plist" >/dev/null 2>&1; then
        echo "✗ bootstrap failed for $plist"
        exit 1
    fi
    if ! launchctl print "gui/$(id -u)/${LABEL}" | grep -qF "$BORING_HOME"; then
        echo "✗ loaded definition does not match $plist"
        exit 1
    fi
    echo "✓ bootstrapped $plist"
}

uninstall_macos() {
    plist="$HOME/Library/LaunchAgents/${LABEL}.plist"
    if [ -f "$plist" ]; then
        launchctl unload "$plist" >/dev/null 2>&1 || launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
        rm -f "$plist"
        echo "✓ removed $plist"
    else
        echo "ⓘ $plist not found"
    fi
}

status_macos() {
    plist="$HOME/Library/LaunchAgents/${LABEL}.plist"
    if [ -f "$plist" ]; then
        echo "✓ registered: $plist"
        launchctl list | grep "$LABEL" || echo "ⓘ plist exists but not loaded"
    else
        echo "✗ not registered"
    fi
    if [ -f "$LOG" ]; then
        echo "last log line: $(tail -n 1 "$LOG")"
    else
        echo "ⓘ no log yet ($LOG)"
    fi
}

case "${1:-}" in
    run) run_job ;;
    install)
        case "$(uname -s)" in
            Darwin) install_macos ;;
            *) echo "✗ unsupported OS: $(uname -s) — the ${RUN_MARK} is macOS/launchd only for now"; exit 1 ;;
        esac
        ;;
    uninstall)
        case "$(uname -s)" in
            Darwin) uninstall_macos ;;
            *) echo "✗ unsupported OS: $(uname -s)"; exit 1 ;;
        esac
        ;;
    status)
        case "$(uname -s)" in
            Darwin) status_macos ;;
            *) echo "✗ unsupported OS: $(uname -s)"; exit 1 ;;
        esac
        ;;
    *) usage; exit 1 ;;
esac
