#!/bin/sh
# Guardrails for doctor's morning-card line (a2e): the launchd log is the only record of a run,
# and finished lands only after card.py's CARD_WAIT_HOURS button wait — a posted run proves
# itself with `[card] posted ts=`, not with finished. Both directions are pinned per case, and
# two mutants prove the cases are not vacuous.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STUB="$ROOT/scripts/lib/health_stub.py"
tmp="$(mktemp -d)"
fails=0

boring="$tmp/boring"
mkdir -p "$boring/agents/codex" "$boring/agents/shared" "$boring/scripts" \
    "$tmp/home/.cache/boring-distill" "$tmp/home/.cache/oh-my-boring" "$tmp/logs"
: > "$boring/agents/codex/collect-sessions.py"
: > "$boring/agents/shared/event_log.py"
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

pass() { echo "ok - $1"; }
fail() { echo "FAIL: $1"; fails=$((fails + 1)); }

# Only the card line is asserted: doctor has no single-block mode, so the suite runs all of it
# against a stub engine and a fixture home, then greps the morning-card verdict.
run_doctor() {
    card_log="$1"; now="$2"; script="${3:-$ROOT/scripts/doctor.sh}"
    env HOME="$tmp/home" \
        BORING_HOME="$boring" \
        BORING_URL="$stub_url" \
        BORING_EVENT_LOG="$tmp/home/.cache/oh-my-boring/events.ndjson" \
        BORING_SKIP_LEDGER_PROBE=1 \
        BORING_SKIP_CONFIG_PROBE=1 \
        BORING_CARD_LOG="$card_log" \
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
    card_log="$1"; now="$2"
    env HOME="$tmp/home" \
        BORING_HOME="$boring" \
        BORING_URL="$stub_url" \
        DOOR_URL="http://127.0.0.1:1" \
        PATH="$tmp/fakebin:$PATH" \
        DOCKER_BIN="$tmp/fakebin/docker" \
        BORING_EVENT_LOG="$tmp/home/.cache/oh-my-boring/events.ndjson" \
        BORING_SKIP_LEDGER_PROBE=1 \
        BORING_SKIP_CONFIG_PROBE=1 \
        BORING_CARD_LOG="$card_log" \
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

NOW="2026-09-26T09:00:00+0900"
START="2026-09-26T08:00:03+0900"

# (a) a finished run with exit 1 is a failure, named with its started stamp.
printf '=== morning card started at %s ===\n=== morning card finished at 2026-09-26T08:00:41+0900 (exit 1) ===\n' "$START" > "$tmp/logs/a.log"
out="$(run_doctor "$tmp/logs/a.log" "$NOW")"
expect "a failed run (exit 1) is reported as FAILED" 1 "✗ morning card FAILED at $START (exit 1)" "$out"

# (b) started + posted line, no finished — the normal state of a good run during the button wait.
printf '=== morning card started at %s ===\n[card] posted ts=1.2\n' "$START" > "$tmp/logs/b.log"
out="$(run_doctor "$tmp/logs/b.log" "$NOW")"
expect "a posted run without finished reads as posted" 1 "✓ morning card: posted at $START" "$out"

# (c) finished exit 0 is the completed shape of the same verdict.
printf '=== morning card started at %s ===\n[card] posted ts=1.2\n=== morning card finished at 2026-09-26T08:05:17+0900 (exit 0) ===\n' "$START" > "$tmp/logs/c.log"
out="$(run_doctor "$tmp/logs/c.log" "$NOW")"
expect "a finished run (exit 0) is reported as posted" 1 "✓ morning card: posted at $START" "$out"

# (d) the last start predates the most recent passed 08:05 — the run never happened.
printf '=== morning card started at 2026-09-25T08:00:03+0900 ===\n' > "$tmp/logs/d.log"
out="$(run_doctor "$tmp/logs/d.log" "$NOW")"
expect "a stale last start is reported as not having run" 1 "✗ morning card did not run since 2026-09-26T08:05:00+09:00" "$out"

# (e) no log at all: 모름, and no ✓/✗ card verdict either.
out="$(run_doctor "$tmp/logs/e.log" "$NOW")"
bad=""
[ "$(printf '%s\n' "$out" | grep -cF -- '! morning card: 모름' || true)" = 1 ] || bad="모름 warn missing"
[ "$(printf '%s\n' "$out" | grep -c '✓ morning card' || true)" = 0 ] || bad="${bad:+$bad, }unexpected ✓ card line"
[ "$(printf '%s\n' "$out" | grep -c '✗ morning card' || true)" = 0 ] || bad="${bad:+$bad, }unexpected ✗ card line"
if [ -z "$bad" ]; then pass "a missing log says 모름, not ✓ and not ✗"; else fail "a missing log — $bad"; fi

# (f) started today with nothing after it, before the 08:05 threshold has passed: not posted yet.
printf '=== morning card started at %s ===\n' "$START" > "$tmp/logs/f.log"
out="$(run_doctor "$tmp/logs/f.log" "2026-09-26T08:03:00+0900")"
expect "a started-but-silent run is reported as not posted yet" 1 "✗ morning card started at $START but has not posted yet" "$out"

# (g) yesterday's posted run is not today's: staleness binds before the posted verdict —
# the 09-24~26 shape (run_card exits before writing `started`, log keeps yesterday's post).
printf '=== morning card started at 2026-09-25T08:00:03+0900 ===\n[card] posted ts=1.2\n' > "$tmp/logs/g.log"
out="$(run_doctor "$tmp/logs/g.log" "$NOW")"
expect "yesterday's posted run is reported as not having run today" 1 "✗ morning card did not run since 2026-09-26T08:05:00+09:00" "$out"

# (h) --strict exit code: the card line alone flips readiness. A FAILED card fails strict; a
# posted card exits the same as no card log at all. Kills a footer that drops failed_card
# (string and zero literal) and a FAILED branch that never sets the flag.
no_log_rc=0
run_doctor_strict "$tmp/logs/e.log" "$NOW" || no_log_rc=$?
posted_rc=0
run_doctor_strict "$tmp/logs/b.log" "$NOW" || posted_rc=$?
failed_rc=0
run_doctor_strict "$tmp/logs/a.log" "$NOW" || failed_rc=$?
if [ "$no_log_rc" -eq 0 ] && [ "$posted_rc" -eq 0 ]; then
    pass "strict passes with no card log and with a posted card"
else
    fail "strict base must pass (no log rc=$no_log_rc, posted rc=$posted_rc) — case (h) is unprovable"
fi
if [ "$failed_rc" -ne 0 ]; then
    pass "a FAILED card makes --strict exit non-zero"
else
    fail "a FAILED card left --strict at exit 0 — failed_card does not reach the footer"
fi

# Non-vacuous proofs on scratch copies: a mutant that cannot turn case (a) or case (d) red
# proves nothing. The copies share the real scripts/lib via one symlink so sourcing still works.
mkdir -p "$tmp/mut"
ln -s "$ROOT/scripts/lib" "$tmp/mut/lib"

# Mutant 1: the exit≠0 branch prints ✓ — case (a) must lose its ✗ line.
sed 's/bad "morning card FAILED/ok "morning card FAILED/' "$ROOT/scripts/doctor.sh" > "$tmp/mut/doctor.sh"
out="$(run_doctor "$tmp/logs/a.log" "$NOW" "$tmp/mut/doctor.sh")"
if printf '%s\n' "$out" | grep -qF "✗ morning card FAILED"; then
    fail "mutation 1: exit≠0 printing ✓ still lets case (a) see ✗ — the mutant survived"
else
    pass "mutation 1: exit≠0 branch printing ✓ makes case (a) fail"
fi

# Mutant 2: the staleness decision removed — case (d) must lose its did-not-run line.
sed 's/int(started.date() < threshold.date())/int(False)/' "$ROOT/scripts/doctor.sh" > "$tmp/mut/doctor.sh"
out="$(run_doctor "$tmp/logs/d.log" "$NOW" "$tmp/mut/doctor.sh")"
if printf '%s\n' "$out" | grep -qF "✗ morning card did not run since"; then
    fail "mutation 2: without the staleness branch case (d) still sees did-not-run — the mutant survived"
else
    pass "mutation 2: removing the staleness branch makes case (d) fail"
fi

if [ "$fails" -eq 0 ]; then
    echo "morning-card doctor guardrails: 11 passed, 0 failed."
    exit 0
fi
echo "morning-card doctor guardrails: $fails failed."
exit 1
