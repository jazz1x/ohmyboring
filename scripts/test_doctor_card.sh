#!/bin/sh
# doctor's morning-card line (a2e), against a hermes fixture home and a fixed clock.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STUB="$ROOT/scripts/lib/health_stub.py"
tmp="$(mktemp -d)"
fails=0

boring="$tmp/boring"
mkdir -p "$boring/agents/codex" "$boring/agents/shared" "$boring/src/ohmyboring/adapters" "$boring/scripts" \
    "$tmp/home/.cache/boring-distill" "$tmp/home/.cache/oh-my-boring" "$tmp/hermes"
: > "$boring/agents/codex/collect-sessions.py"
: > "$boring/src/ohmyboring/adapters/events.py"
: > "$boring/agents/shared/uptake_core.py"
: > "$boring/agents/shared/agent_wiring.py"
printf '#!/bin/sh\necho "verify-llm ok"\n' > "$boring/scripts/verify-llm.sh"
chmod +x "$boring/scripts/verify-llm.sh"

stub_url="http://127.0.0.1:7865"
python3 "$STUB" '{"status":"ok","vector":true,"sync":"idle","corpus_count":5,"db_healthy":true}' 7865 &
srv=$!
trap 'kill "$srv" 2>/dev/null; rm -rf "$tmp"' EXIT
for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -sf -m1 "$stub_url/health" >/dev/null 2>&1 && break
    sleep 0.3
done

passes=0
pass() { echo "ok - $1"; passes=$((passes + 1)); }
fail() { echo "FAIL: $1"; fails=$((fails + 1)); }

# Only the card line is asserted: doctor has no single-block mode, so the suite runs all of it
# against a stub engine and a fixture home, then greps the morning-card verdict.
run_doctor() {
    hermes="$1"; now="$2"; script="${3:-$ROOT/scripts/doctor.sh}"
    env HOME="$tmp/home" \
        BORING_HOME="$boring" \
        BORING_URL="$stub_url" \
        BORING_EVENT_LOG="$tmp/home/.cache/oh-my-boring/events.ndjson" \
        BORING_SKIP_LEDGER_PROBE=1 \
        BORING_SKIP_CONFIG_PROBE=1 \
        BORING_HERMES_HOME="$hermes" \
        BORING_NOW="$now" \
        sh "$script" 2>&1 || true
}

# Case (h) asserts doctor's own exit code, so the rest of the fixture has to pass --strict too:
# wired hooks (with a deliverable SessionEnd command), a fresh note and hook marker, a healthy
# compose stack, and no door. The fake docker answers only the calls doctor makes.
mkdir -p "$tmp/home/.claude" "$tmp/fakebin" "$boring/vault/wiki"
touch "$boring/vault/wiki/wiki-0001.md"
touch "$tmp/home/.cache/boring-distill/session.ts"
cat > "$tmp/home/.claude/settings.json" <<'JSON'
{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"f=$(mktemp); cat > \"$f\"; nohup sh -c 'python3 ~/oh-my-boring/hooks/distill-session.py < \"$0\"; rm -f \"$0\"' \"$f\" >/dev/null 2>&1 &"}]}],"UserPromptSubmit":[{"hooks":[{"type":"command","command":"python3 ~/oh-my-boring/hooks/recall.py"}]}]}}
JSON
cat > "$tmp/fakebin/docker" <<'SH'
#!/bin/sh
case "${1:-} ${2:-}" in
  "compose version") echo "Docker Compose version v2.27.0"; exit 0 ;;
  "compose ps") echo "boring-drudge Up"; exit 0 ;;
  "compose exec") exit 0 ;;
esac
[ "${1:-}" = logs ] && { echo "INFO ready"; exit 0; }
exit 0
SH
chmod +x "$tmp/fakebin/docker"

run_doctor_strict() {
    hermes="$1"; now="$2"
    env HOME="$tmp/home" \
        BORING_HOME="$boring" \
        BORING_URL="$stub_url" \
        DOOR_URL="http://127.0.0.1:1" \
        PATH="$tmp/fakebin:$PATH" \
        DOCKER_BIN="$tmp/fakebin/docker" \
        BORING_EVENT_LOG="$tmp/home/.cache/oh-my-boring/events.ndjson" \
        BORING_SKIP_LEDGER_PROBE=1 \
        BORING_SKIP_CONFIG_PROBE=1 \
        BORING_HERMES_HOME="$hermes" \
        BORING_NOW="$now" \
        sh "$ROOT/scripts/doctor.sh" --strict >/dev/null 2>&1
}

expect() { # <label> <want-count> <pattern> <output>
    label="$1"; want="$2"; pat="$3"; out="$4"
    got=$(printf '%s\n' "$out" | grep -cF -- "$pat" || true)
    if [ "$got" = "$want" ]; then
        pass "$label"
    else
        fail "$label — expected '$pat' x$want, saw x$got"
    fi
}

expect_card_silence() { # <label> <output> — 모름 and neither ✓ nor ✗ on the card line
    label="$1"; out="$2"
    bad=""
    [ "$(printf '%s\n' "$out" | grep -cF -- '! morning card: 모름' || true)" = 1 ] || bad="모름 warn missing"
    [ "$(printf '%s\n' "$out" | grep -c '✓ morning card' || true)" = 0 ] || bad="${bad:+$bad, }unexpected ✓ card line"
    [ "$(printf '%s\n' "$out" | grep -c '✗ morning card' || true)" = 0 ] || bad="${bad:+$bad, }unexpected ✗ card line"
    if [ -z "$bad" ]; then pass "$label"; else fail "$label — $bad"; fi
}

# Hermes-shaped fixtures, same record the real cron writes: jobs.json names the job (id looked
# up by name, never hardcoded in doctor), output files are <YYYY-MM-DD_HH-MM-SS>.md in local
# time and the dir rotates, so newest-by-name is the whole record.
JOB_ID=f1f703aac9b2ec3f

hermes_with_job() { # <dir>
    mkdir -p "$1/cron/output/$JOB_ID"
    printf '{"jobs":[{"id":"%s","name":"morning-card","last_status":"ok","last_run_at":"2026-09-26T08:00:03+09:00"}]}\n' "$JOB_ID" > "$1/cron/jobs.json"
}

hermes_other_job() { # <dir> — jobs.json exists, the morning-card job does not
    mkdir -p "$1/cron"
    printf '{"jobs":[{"id":"cc33a556631a","name":"memory-ingest-worker"}]}\n' > "$1/cron/jobs.json"
}

ok_run() { # <path> <Run Time> [exit]
    printf '# Cron Job: morning-card\n\n**Job ID:** %s\n**Run Time:** %s\n**Mode:** no_agent (script)\n\n---\n\n[run-morning-card] ok exit=%s posted_ts=1790550084.910269\n' \
        "$JOB_ID" "$2" "${3:-0}" > "$1"
}

failed_run() { # <path> <Run Time> <exit> <tail>
    printf '# Cron Job: morning-card\n\n**Job ID:** %s\n**Run Time:** %s\n**Mode:** no_agent (script)\n\n---\n\n[run-morning-card] FAILED: exit=%s tail=%s\n' \
        "$JOB_ID" "$2" "$3" "$4" > "$1"
}

NOW="2026-09-26T09:00:00+0900"

# (a) a FAILED run is a failure, named with its run stamp.
fx="$tmp/hermes/a"; hermes_with_job "$fx"
failed_run "$fx/cron/output/$JOB_ID/2026-09-26_08-00-03.md" "2026-09-26 08:00:03" 1 "door down"
out="$(run_doctor "$fx" "$NOW")"
expect "a failed run is reported as FAILED" 1 "✗ morning card FAILED at 2026-09-26T08:00:03" "$out"
expect "a failed run names its exit code" 1 "(exit 1)" "$out"

# (b) an ok run with posted_ts reads as posted; the stamp is the output file's name.
fx="$tmp/hermes/b"; hermes_with_job "$fx"
ok_run "$fx/cron/output/$JOB_ID/2026-09-26_08-00-03.md" "2026-09-26 08:00:03"
out="$(run_doctor "$fx" "$NOW")"
expect "a posted run reads as posted" 1 "✓ morning card: posted at 2026-09-26T08:00:03" "$out"

# (c) the "already posted today" path prints the same ok line, with today's card ts — same verdict.
fx="$tmp/hermes/c"; hermes_with_job "$fx"
ok_run "$fx/cron/output/$JOB_ID/2026-09-26_08-00-03.md" "2026-09-26 08:00:03"
out="$(run_doctor "$fx" "$NOW")"
expect "the already-posted-today line also reads as posted" 1 "✓ morning card: posted at 2026-09-26T08:00:03" "$out"

# (d) the newest run predates the most recent passed 08:05 — the run never happened.
fx="$tmp/hermes/d"; hermes_with_job "$fx"
ok_run "$fx/cron/output/$JOB_ID/2026-09-25_08-00-03.md" "2026-09-25 08:00:03"
out="$(run_doctor "$fx" "$NOW")"
expect "a stale newest run is reported as not having run" 1 "✗ morning card did not run since 2026-09-26T08:05:00+09:00" "$out"

# (e) no jobs.json at all: 모름, and no ✓/✗ card verdict either.
fx="$tmp/hermes/e"; mkdir -p "$fx/cron"
out="$(run_doctor "$fx" "$NOW")"
expect_card_silence "a missing jobs.json says 모름, not ✓ and not ✗" "$out"

# (e2) jobs.json without a morning-card job: same silence — 0 is not data.
fx="$tmp/hermes/e2"; hermes_other_job "$fx"
out="$(run_doctor "$fx" "$NOW")"
expect "a missing job says 모름" 1 "! morning card: 모름 — hermes에 morning-card 작업이 없음" "$out"
expect_card_silence "a missing job gives no ✓/✗ card verdict" "$out"

# (e3) the job exists but its output dir does not: 모름 again.
fx="$tmp/hermes/e3"
mkdir -p "$fx/cron"
printf '{"jobs":[{"id":"%s","name":"morning-card"}]}\n' "$JOB_ID" > "$fx/cron/jobs.json"
out="$(run_doctor "$fx" "$NOW")"
expect "a missing output dir says 모름" 1 "! morning card: 모름 — 출력 디렉터리가 없음" "$out"
expect_card_silence "a missing output dir gives no ✓/✗ card verdict" "$out"

# (f) a fresh run with no result line yet, before the 08:05 threshold has passed: not posted.
fx="$tmp/hermes/f"; hermes_with_job "$fx"
printf '# Cron Job: morning-card\n\n**Job ID:** %s\n**Run Time:** 2026-09-26 08:00:03\n**Mode:** no_agent (script)\n' "$JOB_ID" \
    > "$fx/cron/output/$JOB_ID/2026-09-26_08-00-03.md"
out="$(run_doctor "$fx" "2026-09-26T08:03:00+0900")"
expect "a fresh run with no result line has not posted yet" 1 "✗ morning card ran at 2026-09-26T08:00:03" "$out"
expect "a fresh run with no result line says has-not-posted" 1 "but has not posted yet" "$out"

# (n) rotation: the newest file by name wins over an older FAILED one — never a count of files.
fx="$tmp/hermes/n"; hermes_with_job "$fx"
failed_run "$fx/cron/output/$JOB_ID/2026-09-25_08-00-03.md" "2026-09-25 08:00:03" 1 "door down"
ok_run "$fx/cron/output/$JOB_ID/2026-09-26_08-00-03.md" "2026-09-26 08:00:03"
out="$(run_doctor "$fx" "$NOW")"
expect "the newest file wins over an older failure" 1 "✓ morning card: posted at 2026-09-26T08:00:03" "$out"

# (w) posted, then a nonzero exit: delivered, with a warning.
fx="$tmp/hermes/w"; hermes_with_job "$fx"
ok_run "$fx/cron/output/$JOB_ID/2026-09-26_08-00-03.md" "2026-09-26 08:00:03" 1
out="$(run_doctor "$fx" "$NOW")"
expect "a posted run with nonzero exit is posted" 1 "✓ morning card: posted at 2026-09-26T08:00:03" "$out"
expect "a posted run with nonzero exit warns" 1 "! morning card ended with exit 1 after posting" "$out"

# (j) an unreadable clock is 모름, not a pass.
out="$(run_doctor "$tmp/hermes/b" "not-a-date")"
expect "an unreadable clock says 모름" 1 "! morning card: 모름 — could not compare dates" "$out"

# (h) --strict exit code: the card line alone flips readiness. A FAILED card fails strict; a
# posted card exits the same as a hermes home with no record at all. Kills a footer that drops
# failed_card (string and zero literal) and a FAILED branch that never sets the flag.
no_record_rc=0
run_doctor_strict "$tmp/hermes/e" "$NOW" || no_record_rc=$?
posted_rc=0
run_doctor_strict "$tmp/hermes/b" "$NOW" || posted_rc=$?
failed_rc=0
run_doctor_strict "$tmp/hermes/a" "$NOW" || failed_rc=$?
if [ "$no_record_rc" -eq 0 ] && [ "$posted_rc" -eq 0 ]; then
    pass "strict passes with no hermes record and with a posted card"
else
    fail "strict base must pass (no record rc=$no_record_rc, posted rc=$posted_rc) — case (h) is unprovable"
fi
if [ "$failed_rc" -ne 0 ]; then
    pass "a FAILED card makes --strict exit non-zero"
else
    fail "a FAILED card left --strict at exit 0 — failed_card does not reach the footer"
fi

# Non-vacuous proofs on scratch copies: a mutant that cannot turn case (n) or case (e2) red
# proves nothing. The copies share the real scripts/lib via one symlink so sourcing still works.
mkdir -p "$tmp/mut"
ln -s "$ROOT/scripts/lib" "$tmp/mut/lib"

# Mutant 1: the newest-by-name selection turned into oldest-by-name — case (n) must lose its ✓.
sed 's/| sort | tail -n 1/| sort | head -n 1/' "$ROOT/scripts/doctor.sh" > "$tmp/mut/doctor.sh"
out="$(run_doctor "$tmp/hermes/n" "$NOW" "$tmp/mut/doctor.sh")"
if printf '%s\n' "$out" | grep -qF "✓ morning card: posted at 2026-09-26T08:00:03"; then
    fail "mutation 1: reading the oldest file still lets case (n) see ✓ — the mutant survived"
else
    pass "mutation 1: reading the oldest file makes case (n) fail"
fi

# Mutant 2: a missing job read as ✓ — case (e2) must light a ✓ card line where silence is wanted.
sed 's/warn "morning card: 모름 — hermes에 morning-card 작업이 없음/ok "morning card: 모름 — hermes에 morning-card 작업이 없음/' \
    "$ROOT/scripts/doctor.sh" > "$tmp/mut/doctor.sh"
out="$(run_doctor "$tmp/hermes/e2" "$NOW" "$tmp/mut/doctor.sh")"
if printf '%s\n' "$out" | grep -qF "✓ morning card"; then
    pass "mutation 2: treating a missing job as ✓ makes case (e2) fail"
else
    fail "mutation 2: treating a missing job as ✓ stayed silent — the mutant survived"
fi

if [ "$fails" -eq 0 ]; then
    echo "morning-card doctor guardrails: $passes passed, 0 failed."
    exit 0
fi
echo "morning-card doctor guardrails: $fails failed."
exit 1
