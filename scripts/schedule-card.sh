#!/bin/sh
# Register and run the morning card, unattended, at 08:00 — same shape as
# schedule-maintenance.sh, one launchd label per unattended job.
#
#   ./scripts/schedule-card.sh run       # execute now, unattended (needs the door up)
#   ./scripts/schedule-card.sh install   # register the daily 08:00 job (macOS launchd)
#   ./scripts/schedule-card.sh uninstall # remove the registration
#   ./scripts/schedule-card.sh status    # show registration state + last log line
set -u

BORING_HOME="${BORING_HOME:-$HOME/oh-my-boring}"
LABEL="com.ohmyboring.morning-card"
LOG="/tmp/${LABEL}.log"
DOOR_URL="${BORING_DOOR_URL:-http://127.0.0.1:7710}"

usage() {
    cat <<EOF
Usage: $0 {run|install|uninstall|status}

  run       Run the morning card once, unattended. Fails visibly (exit 2) if the
            door ($DOOR_URL) is not answering — this script never starts one.
  install   Register the daily 08:00 job (macOS launchd).
  uninstall Remove the registration.
  status    Show whether the daily job is registered, and its last log line.
EOF
}

run_card() {
    if ! curl -sf "${DOOR_URL}/health" >/dev/null 2>&1; then
        echo "✗ door not answering at ${DOOR_URL}/health — start it (make door-up) before the card can run" >&2
        exit 2
    fi
    cd "$BORING_HOME" || { echo "✗ cannot cd to $BORING_HOME"; exit 1; }
    echo "=== morning card started at $(date) ==="
    python3 agents/slack/card.py
    status=$?
    echo "=== morning card finished at $(date) (exit $status) ==="
    exit "$status"
}

escaped_boring_home() {
    printf '%s' "$BORING_HOME" | sed 's/ /\\ /g'
}

install_macos() {
    plist="$HOME/Library/LaunchAgents/${LABEL}.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
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
        <string>cd $(escaped_boring_home) &amp;&amp; ./scripts/schedule-card.sh run</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>0</integer>
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
    if launchctl load "$plist" >/dev/null 2>&1; then
        echo "✓ loaded $plist"
    elif launchctl bootstrap "gui/$(id -u)" "$plist" >/dev/null 2>&1; then
        echo "✓ bootstrapped $plist"
    else
        echo "✗ could not load $plist (run 'make doctor' to check permissions)"
        exit 1
    fi
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
    run) run_card ;;
    install)
        case "$(uname -s)" in
            Darwin) install_macos ;;
            *) echo "✗ unsupported OS: $(uname -s) — the morning card is macOS/launchd only for now"; exit 1 ;;
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
