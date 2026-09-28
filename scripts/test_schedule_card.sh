#!/bin/sh
# A failed card sends one line to the DM; a healthy run stays silent. curl, launchctl and the
# two poster scripts are stubs, so the suite never reaches the real Slack API or launchd, and
# the two jobs (card, weekly) are exercised through the same script under test.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fails=0

home="$tmp/home"
mkdir -p "$home/agents/slack" "$tmp/bin"

# The fixture home's .env — run_card sources exactly this file. PYTHON3=/bin/sh turns the stub
# card.py (a shell script) into the "interpreter" the wrapper invokes.
cat > "$home/.env" <<EOF
SLACK_BOT_TOKEN=test-token-not-real
SLACK_CARD_CHANNEL=C0123456789
BORING_DOOR_URL=http://127.0.0.1:7799
PYTHON3=/bin/sh
EOF

cat > "$home/agents/slack/card.py" <<'EOF'
#!/bin/sh
# Stub for the morning card: prints CARD_STUB_STDERR to stderr, exits CARD_STUB_EXIT,
# and always names itself on stdout so a run proves which script the job executed.
echo "card-stub-ran"
[ -n "${CARD_STUB_STDERR:-}" ] && printf '%s\n' "$CARD_STUB_STDERR" >&2
exit "${CARD_STUB_EXIT:-0}"
EOF

cat > "$home/agents/slack/weekly_card.py" <<'EOF'
#!/bin/sh
# Stub for the weekly card: prints WEEKLY_STUB_STDERR to stderr, exits WEEKLY_STUB_EXIT,
# and always names itself on stdout so a run proves which script the job executed.
echo "weekly-stub-ran"
[ -n "${WEEKLY_STUB_STDERR:-}" ] && printf '%s\n' "$WEEKLY_STUB_STDERR" >&2
exit "${WEEKLY_STUB_EXIT:-0}"
EOF

cat > "$tmp/bin/curl" <<'EOF'
#!/bin/sh
# Stub curl: records every argv line, then answers per the knobs. /health fails when
# STUB_DOOR_DOWN=1 (curl's exit 7: connection refused); the Slack post's body says ok:false
# when STUB_SLACK_FAIL=1 — Slack replies HTTP 200 either way, as the real API does.
printf '%s\n' "$@" >> "$STUB_CURL_LOG"
case " $* " in
    *"/health"*)
        [ "${STUB_DOOR_DOWN:-0}" = "1" ] && exit 7
        exit 0 ;;
esac
if [ "${STUB_SLACK_FAIL:-0}" = "1" ]; then
    printf '%s' '{"ok":false,"error":"stubbed failure"}'
else
    printf '%s' '{"ok":true,"channel":"C0123456789","ts":"1.0"}'
fi
EOF
chmod +x "$tmp/bin/curl"

cat > "$tmp/bin/launchctl" <<'EOF'
#!/bin/sh
# Stub launchctl: records every argv line; `print` answers one line containing BORING_HOME so
# the installer's verification grep has something true to match, and always exits 0 so the
# bootout-then-bootstrap reinstall path is exercised.
printf '%s\n' "$@" >> "$STUB_LAUNCHCTL_LOG"
if [ "${1:-}" = "print" ]; then
    echo "program = ${BORING_HOME}/scripts/schedule-card.sh"
fi
exit 0
EOF
chmod +x "$tmp/bin/launchctl"

# Stub uname: install/uninstall refuse anything but Darwin, and CI runs on Linux.
cat > "$tmp/bin/uname" <<'EOF'
#!/bin/sh
echo Darwin
EOF
chmod +x "$tmp/bin/uname"

pass() { echo "ok - $1"; }
fail() { echo "FAIL: $1"; fails=$((fails + 1)); }

SCRIPT_UNDER_TEST="$ROOT/scripts/schedule-card.sh"
rc=0
out=""

run_case() {
    rc=0
    out=$(env "$@" BORING_HOME="$home" PATH="$tmp/bin:$PATH" STUB_CURL_LOG="$tmp/curl.log" \
        sh "$SCRIPT_UNDER_TEST" run 2>&1) || rc=$?
}

run_weekly_case() {
    rc=0
    out=$(env "$@" BORING_HOME="$home" PATH="$tmp/bin:$PATH" STUB_CURL_LOG="$tmp/curl.log" \
        sh "$SCRIPT_UNDER_TEST" run weekly 2>&1) || rc=$?
}

run_install_case() { # <job> [env…]
    job="$1"; shift
    rc=0
    out=$(env "$@" BORING_HOME="$home" HOME="$home" PATH="$tmp/bin:$PATH" \
        STUB_CURL_LOG="$tmp/curl.log" STUB_LAUNCHCTL_LOG="$tmp/launchctl.log" \
        sh "$SCRIPT_UNDER_TEST" install "$job" 2>&1) || rc=$?
}

notice_check() { # <label> <want-rc> <pattern>… — every pattern must appear in the recorded curl argv
    label="$1"; want="$2"; shift 2
    bad=""
    if [ "$rc" != "$want" ]; then bad="rc $rc (want $want)"; fi
    for pat in "$@"; do
        if ! grep -qF -- "$pat" "$tmp/curl.log"; then
            bad="${bad:+$bad, }notice missing: $pat"
        fi
    done
    if [ -z "$bad" ]; then pass "$label"; else fail "$label — $bad"; fi
}

# (a) the card refuses with its own reason line on stderr: exactly one notice, exit stays 3.
: > "$tmp/curl.log"
run_case CARD_STUB_EXIT=3 'CARD_STUB_STDERR=[card] 카드 거부: boom'
n_post=$(grep -c 'chat\.postMessage' "$tmp/curl.log" || true)
notice_check "a refused card (exit 3) sends one notice with the refusal line" 3 \
    'exit 3' '카드 거부: boom'
if [ "$n_post" != "1" ]; then
    fail "a refused card — expected exactly 1 chat.postMessage, saw $n_post"
fi

# (b) the door is down: the notice carries exit 2 and the door URL, exit stays 2.
: > "$tmp/curl.log"
run_case STUB_DOOR_DOWN=1
n_post=$(grep -c 'chat\.postMessage' "$tmp/curl.log" || true)
notice_check "a dead door (exit 2) sends one notice naming the door" 2 \
    'exit 2' '문(http://127.0.0.1:7799)이 응답하지 않음'
if [ "$n_post" != "1" ]; then
    fail "a dead door — expected exactly 1 chat.postMessage, saw $n_post"
fi

# (c) control: a healthy run sends nothing, exits 0, and actually ran the card's own script.
: > "$tmp/curl.log"
run_case
bad=""
if [ "$rc" != "0" ]; then bad="rc $rc (want 0)"; fi
if grep -q 'chat\.postMessage' "$tmp/curl.log"; then
    bad="${bad:+$bad, }a failure notice was sent"
fi
if ! printf '%s' "$out" | grep -qF 'card-stub-ran'; then
    bad="${bad:+$bad, }the card's script never ran"
fi
if [ -z "$bad" ]; then
    pass "a healthy run (exit 0) sends no notice"
else
    fail "a healthy run — $bad"
fi

# (d) the notice itself fails (Slack body ok:false): stderr says so, exit stays the card's 3.
: > "$tmp/curl.log"
run_case CARD_STUB_EXIT=3 'CARD_STUB_STDERR=[card] 카드 거부: boom' STUB_SLACK_FAIL=1
bad=""
if [ "$rc" != "3" ]; then bad="rc $rc (want 3)"; fi
if ! printf '%s' "$out" | grep -qF '✗ 실패 알림도 못 보냈다'; then
    bad="${bad:+$bad, }stderr is missing the 알림 실패 marker"
fi
if [ -z "$bad" ]; then
    pass "a failed notice is reported on stderr and the card's exit code survives"
else
    fail "a failed notice — $bad"
fi

# ── the weekly job ────────────────────────────────────────────────────────────
# The notices are Korean; taking them from the script under test itself keeps this file
# byte-exact without a second copy. The contract they pin: the weekly DM says the weekly
# wording, never the card's.
card_notice=$(sed -n 's/^[[:space:]]*NOTICE="\(.*\)"$/\1/p' "$ROOT/scripts/schedule-card.sh" | head -n 1)
weekly_notice=$(sed -n 's/^[[:space:]]*NOTICE="\(.*\)"$/\1/p' "$ROOT/scripts/schedule-card.sh" | tail -n 1)
if [ "$card_notice" = "$weekly_notice" ]; then
    fail "the two jobs' notices are identical — the weekly DM cannot be told from the card's"
fi

# (w1) a healthy weekly run sends nothing, exits 0, ran the weekly poster (not the card's
# script), and never probes the door it does not need (its signal is the vault; a broken
# engine is the poster's own failure line).
: > "$tmp/curl.log"
run_weekly_case
bad=""
if [ "$rc" != "0" ]; then bad="rc $rc (want 0)"; fi
if grep -q 'chat\.postMessage' "$tmp/curl.log"; then
    bad="${bad:+$bad, }a failure notice was sent"
fi
if ! printf '%s' "$out" | grep -qF 'weekly-stub-ran'; then
    bad="${bad:+$bad, }the weekly poster never ran"
fi
if printf '%s' "$out" | grep -qF 'card-stub-ran'; then
    bad="${bad:+$bad, }the weekly job ran the card's script"
fi
if grep -q '/health' "$tmp/curl.log"; then
    bad="${bad:+$bad, }the weekly run probed the door it does not need"
fi
if [ -z "$bad" ]; then
    pass "a healthy weekly run (exit 0) sends no notice and does not touch the door"
else
    fail "a healthy weekly run — $bad"
fi

# (w2) a refused weekly poster: exactly one notice, in the weekly's own words, exit stays 3.
: > "$tmp/curl.log"
run_weekly_case WEEKLY_STUB_EXIT=3 'WEEKLY_STUB_STDERR=weekly poster exploded'
n_post=$(grep -c 'chat\.postMessage' "$tmp/curl.log" || true)
notice_check "a refused weekly (exit 3) sends one weekly-worded notice with the refusal line" 3 \
    "$weekly_notice" 'exit 3' 'weekly poster exploded'
if [ "$n_post" != "1" ]; then
    fail "a refused weekly — expected exactly 1 chat.postMessage, saw $n_post"
fi
if grep -qF -- "$card_notice" "$tmp/curl.log"; then
    fail "a refused weekly — the notice used the card's wording"
fi

# (w3) install: the weekly job registers under its own label, Monday 09:00, its own log and
# poster, and the installer's launchctl verification ran against that label.
rm -f "$home/Library/LaunchAgents/com.ohmyboring.weekly-card.plist" \
    "$home/Library/LaunchAgents/com.ohmyboring.morning-card.plist"
: > "$tmp/launchctl.log"
run_install_case weekly
weekly_plist="$home/Library/LaunchAgents/com.ohmyboring.weekly-card.plist"
bad=""
if [ "$rc" != "0" ]; then bad="rc $rc (want 0)"; fi
if [ ! -f "$weekly_plist" ]; then
    bad="${bad:+$bad, }weekly plist not written"
else
    grep -qF 'com.ohmyboring.weekly-card' "$weekly_plist" || bad="${bad:+$bad, }label missing"
    grep -qF '<key>Weekday</key>' "$weekly_plist" || bad="${bad:+$bad, }Monday schedule missing"
    weekday_hour=$(sed -n '/<key>Weekday</,/<integer>1</p' "$weekly_plist")
    [ -n "$weekday_hour" ] || bad="${bad:+$bad, }Weekday 1 missing"
    grep -A1 '<key>Hour</key>' "$weekly_plist" | grep -qF '<integer>9</integer>' \
        || bad="${bad:+$bad, }09:00 missing"
    grep -qF '/tmp/com.ohmyboring.weekly-card.log' "$weekly_plist" || bad="${bad:+$bad, }weekly log missing"
    grep -qF 'run weekly' "$weekly_plist" || bad="${bad:+$bad, }plist does not run the weekly job"
fi
if ! grep -qF "gui/$(id -u)/com.ohmyboring.weekly-card" "$tmp/launchctl.log"; then
    bad="${bad:+$bad, }install verification did not print the weekly label"
fi
if [ -f "$home/Library/LaunchAgents/com.ohmyboring.morning-card.plist" ]; then
    bad="${bad:+$bad, }installing the weekly touched the card's label"
fi
if [ -z "$bad" ]; then
    pass "install weekly registers com.ohmyboring.weekly-card, Monday 09:00, verified against its own label"
else
    fail "install weekly — $bad"
fi

# (w4) the card job still registers through the same code: daily 08:00, no weekday key.
rm -f "$home/Library/LaunchAgents/com.ohmyboring.morning-card.plist"
: > "$tmp/launchctl.log"
run_install_case card
card_plist="$home/Library/LaunchAgents/com.ohmyboring.morning-card.plist"
bad=""
if [ "$rc" != "0" ]; then bad="rc $rc (want 0)"; fi
if [ ! -f "$card_plist" ]; then
    bad="${bad:+$bad, }card plist not written"
else
    grep -qF 'com.ohmyboring.morning-card' "$card_plist" || bad="${bad:+$bad, }label missing"
    if grep -qF '<key>Weekday</key>' "$card_plist"; then
        bad="${bad:+$bad, }the card grew a weekday"
    fi
    grep -A1 '<key>Hour</key>' "$card_plist" | grep -qF '<integer>8</integer>' \
        || bad="${bad:+$bad, }08:00 missing"
fi
if [ -z "$bad" ]; then
    pass "install card still registers the daily 08:00 morning card through the same code"
else
    fail "install card — $bad"
fi


# Non-vacuous proof: delete the notice call from a scratch copy — case (a) must then fail.
# A test that would pass with the notice removed is not testing the notice.
# The literal "$status" is the fixed string grep -F must match — no expansion wanted.
# shellcheck disable=SC2016
notify_call='notify_failure "$status"'
notify_line=$(grep -nF -- "$notify_call" "$ROOT/scripts/schedule-card.sh" | cut -d: -f1)
grep -vF -- "$notify_call" "$ROOT/scripts/schedule-card.sh" > "$tmp/mutant.sh"
SCRIPT_UNDER_TEST="$tmp/mutant.sh"
: > "$tmp/curl.log"
run_case CARD_STUB_EXIT=3 'CARD_STUB_STDERR=[card] 카드 거부: boom'
SCRIPT_UNDER_TEST="$ROOT/scripts/schedule-card.sh"
if [ "$rc" = "3" ] && ! grep -q 'chat\.postMessage' "$tmp/curl.log"; then
    pass "mutation: deleting notify_failure (line $notify_line) makes case (a) send nothing — the test is not vacuous"
else
    fail "mutation: case (a) still sees a notice without the call — the test proves nothing"
fi

# Non-vacuous proof for the weekly label: a mutant that registers the weekly job under the
# card's label must fail case (w3) — its plist never appears where the weekly belongs.
sed 's/com\.ohmyboring\.weekly-card/com.ohmyboring.morning-card/g' \
    "$ROOT/scripts/schedule-card.sh" > "$tmp/label-mutant.sh"
SCRIPT_UNDER_TEST="$tmp/label-mutant.sh"
rm -f "$home/Library/LaunchAgents/com.ohmyboring.weekly-card.plist" \
    "$home/Library/LaunchAgents/com.ohmyboring.morning-card.plist"
: > "$tmp/launchctl.log"
run_install_case weekly
SCRIPT_UNDER_TEST="$ROOT/scripts/schedule-card.sh"
if [ "$rc" = "0" ] && [ ! -f "$home/Library/LaunchAgents/com.ohmyboring.weekly-card.plist" ]; then
    pass "mutation: registering the weekly under the card's label makes case (w3) miss its plist — the label check is not vacuous"
else
    fail "mutation: the weekly plist still appeared with the card's label — the label check proves nothing"
fi

if [ "$fails" -eq 0 ]; then
    echo "card/weekly scheduler guardrails: all passed, 0 failed."
    exit 0
fi
echo "card/weekly scheduler guardrails: $fails failed."
exit 1
