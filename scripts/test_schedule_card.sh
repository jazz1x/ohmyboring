#!/bin/sh
# A failed morning card sends one line to the DM; a healthy run stays silent. curl and card.py
# are stubs, so the suite never reaches the real Slack API.
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
# Stub for the morning card: prints CARD_STUB_STDERR to stderr, exits CARD_STUB_EXIT.
[ -n "${CARD_STUB_STDERR:-}" ] && printf '%s\n' "$CARD_STUB_STDERR" >&2
exit "${CARD_STUB_EXIT:-0}"
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

# (c) control: a healthy run sends nothing and exits 0.
: > "$tmp/curl.log"
run_case
bad=""
if [ "$rc" != "0" ]; then bad="rc $rc (want 0)"; fi
if grep -q 'chat\.postMessage' "$tmp/curl.log"; then
    bad="${bad:+$bad, }a failure notice was sent"
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

if [ "$fails" -eq 0 ]; then
    echo "morning-card failure notice guardrails: 5 passed, 0 failed."
    exit 0
fi
echo "morning-card failure notice guardrails: $fails failed."
exit 1
