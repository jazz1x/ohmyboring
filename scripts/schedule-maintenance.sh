#!/bin/sh
# Register and run unattended housekeeping for oh-my-boring.
# Combines data-steward (vault hygiene) and retention (raw transcript lifecycle) into a
# single daily job. On macOS it uses launchd; on Linux it uses the user's crontab.
#
#   ./scripts/schedule-maintenance.sh run       # execute now, unattended
#   ./scripts/schedule-maintenance.sh install   # register daily job
#   ./scripts/schedule-maintenance.sh uninstall # remove daily job
#   ./scripts/schedule-maintenance.sh status    # show registration state
set -u

BORING_HOME="${BORING_HOME:-$HOME/oh-my-boring}"
LABEL="com.ohmyboring.maintenance"
LOG="/tmp/${LABEL}.log"

usage() {
    cat <<EOF
Usage: $0 {run|install|uninstall|status}

  run       Execute data-steward --fix --yes + retention --apply --yes now.
  install   Register daily automatic maintenance (macOS launchd / Linux cron).
  uninstall Remove the registration.
  status    Show whether daily maintenance is registered.
EOF
}

run_maintenance() {
    cd "$BORING_HOME" || { echo "✗ cannot cd to $BORING_HOME"; exit 1; }
    echo "=== oh-my-boring maintenance started at $(date) ==="
    echo "--- data-steward ---"
    python3 scripts/data-steward.py --fix --yes
    echo "--- retention ---"
    python3 scripts/retention.py --apply --yes
    # Injection precision is the product's first-class metric and it cannot be computed from
    # distances — only from labels on real injections. Collecting them by hand never happened,
    # so the daily run takes a bounded sample. Bounded on purpose: the local model judges 24
    # hits in roughly a minute, and a backlog sweep would make this run unbounded. Failure here
    # must not fail housekeeping — a missing day of labels is a smaller loss than a skipped
    # retention pass.
    echo "--- recall labels ---"
    python3 scripts/label-recall.py --judge --queries 8 --hits 3 || echo "(label pass failed — continuing)"
    # The code index is a separate corpus and it goes stale silently — nothing else re-runs it,
    # and doctor only notices after the fact. Failure must not fail housekeeping either.
    echo "--- code-sync ---"
    ./drudge/target/release/drudge code-sync || echo "(code-sync failed — continuing)"
    echo "=== maintenance finished at $(date) ==="
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
        <string>cd $(escaped_boring_home) &amp;&amp; ./scripts/schedule-maintenance.sh run</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>3</integer>
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
}

install_linux() {
    cron_cmd="0 3 * * * cd $(escaped_boring_home) && ./scripts/schedule-maintenance.sh run >${LOG} 2>&1"
    (crontab -l 2>/dev/null | grep -v "$LABEL"; echo "# ${LABEL}"; echo "$cron_cmd") | crontab -
    echo "✓ registered in user crontab"
}

uninstall_linux() {
    crontab -l 2>/dev/null | grep -v "$LABEL" | crontab -
    echo "✓ removed from user crontab"
}

status_linux() {
    if crontab -l 2>/dev/null | grep -q "$LABEL"; then
        echo "✓ registered in user crontab"
        crontab -l | grep "$LABEL"
    else
        echo "✗ not registered"
    fi
}

case "${1:-}" in
    run) run_maintenance ;;
    install)
        case "$(uname -s)" in
            Darwin) install_macos ;;
            Linux) install_linux ;;
            *) echo "✗ unsupported OS: $(uname -s)"; exit 1 ;;
        esac
        ;;
    uninstall)
        case "$(uname -s)" in
            Darwin) uninstall_macos ;;
            Linux) uninstall_linux ;;
            *) echo "✗ unsupported OS: $(uname -s)"; exit 1 ;;
        esac
        ;;
    status)
        case "$(uname -s)" in
            Darwin) status_macos ;;
            Linux) status_linux ;;
            *) echo "✗ unsupported OS: $(uname -s)"; exit 1 ;;
        esac
        ;;
    *) usage; exit 1 ;;
esac
