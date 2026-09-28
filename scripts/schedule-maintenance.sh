#!/bin/sh
# Register and run unattended housekeeping for oh-my-boring.
# Combines data-steward (vault hygiene) and retention (raw transcript lifecycle) into a
# single daily job, then closes with a read-only doctor pass that DMs the owner on ✗.
# On macOS it uses launchd; on Linux it uses the user's crontab.
#
#   ./scripts/schedule-maintenance.sh run       # execute now, unattended
#   ./scripts/schedule-maintenance.sh install   # register daily job
#   ./scripts/schedule-maintenance.sh uninstall # remove daily job
#   ./scripts/schedule-maintenance.sh status    # show registration state
set -u

BORING_HOME="${BORING_HOME:-$HOME/oh-my-boring}"
LABEL="com.ohmyboring.maintenance"
LOG="/tmp/${LABEL}.log"

# The failure notice and the .env load live in scripts/lib/ — one copy both schedulers
# source; copied notices and loaders drift.
LIB_DIR="$(cd "$(dirname "$0")/lib" && pwd)"
# shellcheck source=lib/dot_env.sh
. "$LIB_DIR/dot_env.sh"
# shellcheck source=lib/slack_notify.sh
. "$LIB_DIR/slack_notify.sh"

usage() {
    cat <<EOF
Usage: $0 {run|install|uninstall|status}

  run       Run data-steward, retention, recall labels, code-sync, then a read-only doctor.
  install   Register daily automatic maintenance (macOS launchd / Linux cron).
  uninstall Remove the registration.
  status    Show whether daily maintenance is registered.
EOF
}

run_maintenance() {
    cd "$BORING_HOME" || { echo "✗ cannot cd to $BORING_HOME"; exit 1; }
    # launchd/cron run this with none of the interactive shell's env — the shared loader
    # (scripts/lib/dot_env.sh) sources ./.env, the same way schedule-card.sh does.
    load_dot_env
    echo "=== oh-my-boring maintenance started at $(date) ==="
    echo "--- data-steward ---"
    python3 scripts/data-steward.py --fix --yes
    # --fix cannot repair this one: which repo a folder-named project belongs to takes the
    # session's cwd, and `re-work` fronted two repos (2026-09-28). So it reports, and says so.
    splits=$(python3 scripts/data-steward.py --json --checkout-roots "$HOME/Development:$HOME/orca/workspaces" |
        python3 -c 'import json, sys; print(", ".join("%s(%d)→%s" % (s["project"], s["notes"], "/".join(s["repos"])) for s in json.load(sys.stdin)["folder_slug_splits"]))') ||
        echo "(folder-name check failed — continuing)"
    if [ -n "$splits" ]; then
        echo "folder-named projects: $splits"
        notify_slack "폴더 이름으로 잡힌 프로젝트 — ${splits}"
    fi
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
    # Close the loop this job used to leave open: twice (2026-09-21 hermes Slack socket failed
    # 267,201 times over ten days; 2026-09-26→27 ~20 h of reconnect loop) doctor could have
    # caught it — but nothing ran doctor at 03:00, so nobody was told. Strict and read-only:
    # --strict turns any ✗ into a non-zero exit, and --fix is deliberately never passed — the
    # nightly pass reports; a human or an interactive --fix decides.
    echo "--- doctor ---"
    doctor_out=$(sh scripts/doctor.sh --strict 2>&1)
    doctor_rc=$?
    printf '%s\n' "$doctor_out"
    if [ "$doctor_rc" -ne 0 ]; then
        x_count=$(printf '%s\n' "$doctor_out" | grep -c '^✗ ' || true)
        first_x=$(printf '%s\n' "$doctor_out" | sed -n 's/^✗ //p' | head -n 1 | cut -c1-150)
        [ -n "$first_x" ] || first_x="✗ 줄 없이 비정상 종료"
        if [ "$x_count" -gt 1 ]; then
            notify_slack "새벽 점검에서 ✗ ${x_count}개 — ${first_x}(…외 $((x_count - 1))개)"
        else
            notify_slack "새벽 점검에서 ✗ ${x_count}개 — ${first_x}"
        fi
    fi
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
