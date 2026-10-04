#!/bin/sh
# The nightly maintenance run closes with a read-only doctor pass (scripts/doctor.sh --strict,
# never --fix): a failing doctor sends exactly one DM line — the ✗ count and the first ✗ line —
# while housekeeping still finishes, and a green doctor stays silent. doctor, the housekeeping
# steps, drudge and curl are stubs, so the suite never touches the real stack, launchd, or Slack.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fails=0

home="$tmp/home"
mkdir -p "$home/scripts" "$home/drudge/target/release" "$tmp/bin"

# The fixture home's .env — run_maintenance sources exactly this file for the Slack token.
cat > "$home/.env" <<EOF
SLACK_BOT_TOKEN=test-token-not-real
SLACK_CARD_CHANNEL=C0123456789
EOF

# Stub doctor: records its own argv (the suite pins the --strict, no --fix call), prints
# DOCTOR_STUB_X_LINES lines beginning with "✗ ", then exits DOCTOR_STUB_EXIT.
cat > "$home/scripts/doctor.sh" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$DOCTOR_STUB_ARGV_LOG"
i=0
while [ "$i" -lt "${DOCTOR_STUB_X_LINES:-0}" ]; do
    echo "✗ stub doctor failure line $((i + 1))"
    i=$((i + 1))
done
echo "✓ stub doctor green line"
exit "${DOCTOR_STUB_EXIT:-0}"
EOF

# The housekeeping steps before doctor: no-op Python stubs and a drudge stub, all green.
for step in data-steward retention label-recall; do
    printf 'print("%s-ran")\n' "$step" > "$home/scripts/$step.py"
done
cat > "$home/drudge/target/release/drudge" <<'EOF'
#!/bin/sh
echo "drudge-stub-ran $*"
EOF
chmod +x "$home/drudge/target/release/drudge"

# Stub curl: records every argv line, then answers ok:true like a real Slack post.
cat > "$tmp/bin/curl" <<'EOF'
#!/bin/sh
printf '%s\n' "$@" >> "$STUB_CURL_LOG"
printf '%s' '{"ok":true,"channel":"C0123456789","ts":"1.0"}'
EOF
chmod +x "$tmp/bin/curl"

pass() { echo "ok - $1"; }
fail() { echo "FAIL: $1"; fails=$((fails + 1)); }

SCRIPT_UNDER_TEST="$ROOT/scripts/schedule-maintenance.sh"
rc=0
out=""

run_case() {
    rc=0
    out=$(env "$@" BORING_HOME="$home" PATH="$tmp/bin:$PATH" \
        STUB_CURL_LOG="$tmp/curl.log" DOCTOR_STUB_ARGV_LOG="$tmp/doctor-argv.log" \
        sh "$SCRIPT_UNDER_TEST" run 2>&1) || rc=$?
}

# (a) a doctor that fails with two ✗ lines: exactly one DM naming the count, the first line,
# and the rest-count; the run itself still finishes — doctor failure must not fail housekeeping.
: > "$tmp/curl.log"
: > "$tmp/doctor-argv.log"
run_case DOCTOR_STUB_EXIT=1 DOCTOR_STUB_X_LINES=2
n_post=$(grep -c 'chat\.postMessage' "$tmp/curl.log" || true)
bad=""
if [ "$rc" != "0" ]; then bad="rc $rc (want 0)"; fi
if [ "$n_post" != "1" ]; then bad="${bad:+$bad, }expected exactly 1 chat.postMessage, saw $n_post"; fi
if ! grep -qF -- '새벽 점검에서 ✗ 2개 — stub doctor failure line 1(…외 1개)' "$tmp/curl.log"; then
    bad="${bad:+$bad, }DM text missing: 새벽 점검에서 ✗ 2개 — stub doctor failure line 1(…외 1개)"
fi
if ! printf '%s' "$out" | grep -qF '=== maintenance finished'; then
    bad="${bad:+$bad, }the run did not finish"
fi
if ! printf '%s' "$out" | grep -qF 'stub doctor failure line 2'; then
    bad="${bad:+$bad, }doctor output never reached the log"
fi
if ! grep -qxF -- '--strict' "$tmp/doctor-argv.log"; then
    bad="${bad:+$bad, }doctor was not called with exactly --strict"
fi
if grep -qF -- '--fix' "$tmp/doctor-argv.log"; then
    bad="${bad:+$bad, }doctor was called with --fix"
fi
if [ -z "$bad" ]; then
    pass "a doctor with two ✗ lines sends one DM (count + first line + rest) and the run still finishes"
else
    fail "a doctor with two ✗ lines — $bad"
fi

# (b) a lone ✗ line: one DM, no (…외) suffix — a single failure is not "and N more".
: > "$tmp/curl.log"
run_case DOCTOR_STUB_EXIT=1 DOCTOR_STUB_X_LINES=1
n_post=$(grep -c 'chat\.postMessage' "$tmp/curl.log" || true)
bad=""
if [ "$rc" != "0" ]; then bad="rc $rc (want 0)"; fi
if [ "$n_post" != "1" ]; then bad="${bad:+$bad, }expected exactly 1 chat.postMessage, saw $n_post"; fi
if ! grep -qF -- '새벽 점검에서 ✗ 1개 — stub doctor failure line 1' "$tmp/curl.log"; then
    bad="${bad:+$bad, }DM text missing: 새벽 점검에서 ✗ 1개 — stub doctor failure line 1"
fi
if grep -qF '…외' "$tmp/curl.log"; then
    bad="${bad:+$bad, }a lone ✗ grew an (…외) suffix"
fi
if [ -z "$bad" ]; then
    pass "a doctor with one ✗ line sends one DM without an (…외) suffix"
else
    fail "a doctor with one ✗ line — $bad"
fi

# (c) control: a green doctor sends nothing and the run exits 0.
: > "$tmp/curl.log"
run_case
bad=""
if [ "$rc" != "0" ]; then bad="rc $rc (want 0)"; fi
if grep -q 'chat\.postMessage' "$tmp/curl.log"; then
    bad="${bad:+$bad, }a failure DM was sent"
fi
if [ -z "$bad" ]; then
    pass "a green doctor sends no DM"
else
    fail "a green doctor — $bad"
fi

# (d) no .env (launchd without a token): the notice cannot send, says so on stderr, and the
# run still finishes — a notice must never fail housekeeping.
mv "$home/.env" "$home/.env.off"
: > "$tmp/curl.log"
run_case SLACK_BOT_TOKEN= SLACK_CARD_CHANNEL= DOCTOR_STUB_EXIT=1 DOCTOR_STUB_X_LINES=1
mv "$home/.env.off" "$home/.env"
bad=""
if [ "$rc" != "0" ]; then bad="rc $rc (want 0)"; fi
if grep -q 'chat\.postMessage' "$tmp/curl.log"; then
    bad="${bad:+$bad, }a DM was sent without a token"
fi
if ! printf '%s' "$out" | grep -qF '✗ 실패 알림도 못 보냈다'; then
    bad="${bad:+$bad, }stderr is missing the 알림 실패 marker"
fi
if ! printf '%s' "$out" | grep -qF '=== maintenance finished'; then
    bad="${bad:+$bad, }the run did not finish"
fi
if [ -z "$bad" ]; then
    pass "a missing token means one stderr line and the run still finishes"
else
    fail "a missing token — $bad"
fi

# Scratch mutants run from a directory with the real scripts/lib beside them: the script
# resolves lib relative to its own path, and a symlink keeps the mutant on the shared files.
mutant_dir="$tmp/mutant"
mkdir -p "$mutant_dir"
ln -s "$ROOT/scripts/lib" "$mutant_dir/lib"

# Non-vacuous proof for the silence: a mutant that sends the DM on a green doctor must make
# case (c) fail. The guard line is the only place doctor_rc gates the notice.
sed 's/if \[ "$doctor_rc" -ne 0 \]; then/if true; then/' \
    "$ROOT/scripts/schedule-maintenance.sh" > "$mutant_dir/schedule-maintenance.sh"
grep -qF 'if true; then' "$mutant_dir/schedule-maintenance.sh" || fail "mutation setup: the sed did not apply"
SCRIPT_UNDER_TEST="$mutant_dir/schedule-maintenance.sh"
: > "$tmp/curl.log"
run_case
SCRIPT_UNDER_TEST="$ROOT/scripts/schedule-maintenance.sh"
if [ "$rc" = "0" ] && grep -q 'chat\.postMessage' "$tmp/curl.log"; then
    pass "mutation: sending a DM on a green doctor makes case (c) fail — the silence check is not vacuous"
else
    fail "mutation: a green doctor stayed silent even with the guard removed — case (c) proves nothing"
fi

# Non-vacuous proof for the finish guarantee: a mutant that aborts the run on doctor failure
# must make case (a)'s rc/finished-marker assertions fail.
sed 's/if \[ "$doctor_rc" -ne 0 \]; then/if [ "$doctor_rc" -ne 0 ]; then exit 9;/' \
    "$ROOT/scripts/schedule-maintenance.sh" > "$mutant_dir/schedule-maintenance.sh"
grep -qF 'exit 9;' "$mutant_dir/schedule-maintenance.sh" || fail "mutation setup: the sed did not apply"
SCRIPT_UNDER_TEST="$mutant_dir/schedule-maintenance.sh"
: > "$tmp/curl.log"
run_case DOCTOR_STUB_EXIT=1 DOCTOR_STUB_X_LINES=2
SCRIPT_UNDER_TEST="$ROOT/scripts/schedule-maintenance.sh"
if [ "$rc" != "0" ] || ! printf '%s' "$out" | grep -qF '=== maintenance finished'; then
    pass "mutation: aborting maintenance on doctor failure makes case (a) fail — the finish guarantee is not vacuous"
else
    fail "mutation: the run still finished with the abort in place — case (a) proves nothing"
fi

# (install) the plist carries a PATH that starts at the installer's python3 — launchd's own
# PATH resolves python3 to Xcode's 3.9, and every nested python3 in run/doctor follows PATH.
mkdir -p "$tmp/py/bin"
printf '#!/bin/sh\n' > "$tmp/py/bin/python3"
chmod +x "$tmp/py/bin/python3"
printf '#!/bin/sh\n[ "${1:-}" = print ] && echo "program = $BORING_HOME"\nexit 0\n' > "$tmp/bin/launchctl"
printf '#!/bin/sh\necho Darwin\n' > "$tmp/bin/uname"
chmod +x "$tmp/bin/launchctl" "$tmp/bin/uname"
rc=0
out=$(env BORING_HOME="$home" HOME="$home" PATH="$tmp/bin:$tmp/py/bin:/usr/bin:/bin" \
    sh "$SCRIPT_UNDER_TEST" install 2>&1) || rc=$?
plist="$home/Library/LaunchAgents/com.ohmyboring.maintenance.plist"
if [ "$rc" = "0" ] && grep -A1 '<key>PATH</key>' "$plist" | grep -qF "<string>$tmp/py/bin:"; then
    pass "install: the plist's PATH starts at the installer's python3 directory"
else
    fail "install: no PATH starting at $tmp/py/bin in the plist (rc=$rc): $out"
fi

if [ "$fails" -eq 0 ]; then
    echo "nightly-maintenance scheduler guardrails: all passed, 0 failed."
    exit 0
fi
echo "nightly-maintenance scheduler guardrails: $fails failed."
exit 1
