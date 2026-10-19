#!/bin/sh
# doctor's door-image freshness line (d5d): the running boring-door container must be newer
# than the last commit touching a path agents/door/Dockerfile COPYs. Real git history with
# pinned commit dates; docker is a PATH stub that answers `inspect` from env.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fails=0
passes=0
pass() { echo "ok - $1"; passes=$((passes + 1)); }
fail() { echo "FAIL: $1"; fails=$((fails + 1)); }

boring="$tmp/boring"
mkdir -p "$boring/agents/door" "$boring/src" "$boring/docs" "$tmp/home" "$tmp/fakebin" "$tmp/mut"
ln -s "$ROOT/scripts/lib" "$tmp/mut/lib"
cat > "$boring/agents/door/Dockerfile" <<'EOF'
FROM python:3.12-slim
COPY requirements.txt requirements.txt
COPY src/ src/
COPY agents/door/ agents/door/
EOF
echo req > "$boring/requirements.txt"
echo a > "$boring/src/a.py"
echo d > "$boring/docs/d.md"

commit_at() { # <iso-utc> <message> — commits everything staged-or-not at a pinned time
    git -C "$boring" add -A
    GIT_AUTHOR_DATE="$1" GIT_COMMITTER_DATE="$1" git -C "$boring" \
        -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -q -m "$2"
}
git -C "$boring" init -q
commit_at 2026-10-19T01:00:00Z "base"
echo b >> "$boring/src/a.py"
commit_at 2026-10-19T03:04:51Z "touch COPY path"
echo e >> "$boring/docs/d.md"
commit_at 2026-10-19T05:00:00Z "touch non-COPY path"

cat > "$tmp/fakebin/docker" <<'SH'
#!/bin/sh
case "${1:-} ${2:-}" in
  "compose version") echo "Docker Compose version v2.27.0"; exit 0 ;;
  "compose ps") echo "boring-drudge Up"; exit 0 ;;
  "compose exec") exit 0 ;;
esac
if [ "${1:-}" = inspect ]; then
    [ -n "${FAKE_INSPECT_FAIL:-}" ] && { echo "Cannot connect to the Docker daemon" >&2; exit 1; }
    echo "$FAKE_CREATED"
    exit 0
fi
exit 0
SH
chmod +x "$tmp/fakebin/docker"

run_doctor() { # <created> <fail-flag> [doctor.sh]
    script="${3:-$ROOT/scripts/doctor.sh}"
    env HOME="$tmp/home" BORING_HOME="$boring" BORING_URL="http://127.0.0.1:1" \
        DOOR_URL="http://127.0.0.1:1" PATH="$tmp/fakebin:$PATH" DOCKER_BIN="$tmp/fakebin/docker" \
        BORING_SKIP_LEDGER_PROBE=1 BORING_SKIP_CONFIG_PROBE=1 \
        FAKE_CREATED="$1" FAKE_INSPECT_FAIL="$2" \
        sh "$script" 2>&1 || true
}
door_line() { printf '%s\n' "$1" | rg 'door image|cannot measure door' || true; }

# 1. violation: a COPY-path commit (03:04:51Z) is newer than the container (03:00:00Z).
out="$(run_doctor 2026-10-19T03:00:00.123456789Z "")"
line="$(door_line "$out")"
case "$line" in
    "✗ DEPLOY DRIFT — door image 2026-10-19T03:00:00"*"last COPY-path commit 2026-10-19T03:04:51"*) pass "1 stale door image is red, with both times" ;;
    *) fail "1 stale door image — got: ${line:-<no door line>}" ;;
esac

# 2. control: only a non-COPY-path commit (05:00) is newer than the container (04:00).
out="$(run_doctor 2026-10-19T04:00:00.5Z "")"
line="$(door_line "$out")"
case "$line" in
    "✓ door image 2026-10-19T04:00:00"*"last COPY-path commit 2026-10-19T03:04:51"*) pass "2 container newer than last COPY-path commit passes" ;;
    *) fail "2 control — got: ${line:-<no door line>}" ;;
esac

# 3. docker inspect fails: cannot measure is red, never a pass.
out="$(run_doctor "" 1)"
line="$(door_line "$out")"
case "$line" in
    "✗ cannot measure door image"*"inspect"*) pass "3 inspect failure is red with the reason" ;;
    *) fail "3 inspect failure — got: ${line:-<no door line>}" ;;
esac

# Mutants run on copies of doctor.sh; both must flip a verdict above.
# 4. door drift + --fix: the operator gets the command, and no make target runs.
cat > "$tmp/fakebin/make" <<'SH'
#!/bin/sh
echo "make $*" >> "$MAKE_LOG"
exit 0
SH
chmod +x "$tmp/fakebin/make"
make_log="$tmp/make.log"
: > "$make_log"
# A healthy engine, so no other failure triggers make: whatever make logs is the door's doing.
python3 "$ROOT/scripts/lib/health_stub.py" '{"status":"ok","vector":true,"sync":"idle","corpus_count":5,"db_healthy":true}' 7866 &
stub_pid=$!
trap 'kill "$stub_pid" 2>/dev/null; rm -rf "$tmp"' EXIT
for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -sf -m1 "http://127.0.0.1:7866/health" >/dev/null 2>&1 && break
    sleep 0.3
done
run_fix() { # <doctor.sh> — stale door, healthy engine, --fix
    : > "$make_log"
    env HOME="$tmp/home" BORING_HOME="$boring" BORING_URL="http://127.0.0.1:7866" \
        DOOR_URL="http://127.0.0.1:1" PATH="$tmp/fakebin:$PATH" DOCKER_BIN="$tmp/fakebin/docker" \
        BORING_SKIP_LEDGER_PROBE=1 BORING_SKIP_CONFIG_PROBE=1 MAKE_LOG="$make_log" \
        FAKE_CREATED=2026-10-19T03:00:00Z FAKE_INSPECT_FAIL="" \
        sh "$1" --fix 2>&1 || true
}
out="$(run_fix "$ROOT/scripts/doctor.sh")"
if [ ! -s "$make_log" ]; then
    pass "4 --fix with a stale door runs no make target"
else
    fail "4 --fix invoked make: $(cat "$make_log")"
fi
hint="$(printf '%s\n' "$out" | rg -c "run 'make door-build && make door-up' yourself" || true)"
if [ "$hint" -ge 1 ]; then
    pass "4 --fix prints the door-build hint"
else
    fail "4 --fix hint missing"
fi
# The hint's command must be the one the delivering skill states, one wording.
if rg -qF 'make door-build && make door-up' "$ROOT/.claude/skills/delivering/SKILL.md"; then
    pass "4 hint command matches delivering/SKILL.md"
else
    fail "4 delivering/SKILL.md no longer states 'make door-build && make door-up'"
fi

# Mutant C: door drift routed through the generic container fix -> case 4 must see make run.
cp "$ROOT/scripts/doctor.sh" "$tmp/mut/doctor.sh"
sd -F 'failed_door_image=1' 'failed_containers=1' "$tmp/mut/doctor.sh"
run_fix "$tmp/mut/doctor.sh" > /dev/null
if [ -s "$make_log" ]; then
    pass "mutation C: door drift via failed_containers makes --fix run make"
else
    fail "mutation C: door drift via failed_containers still ran no make — case 4 proves nothing"
fi

# Mutant A: cannot-measure rendered as ok -> case 3 must stop being red.
cp "$ROOT/scripts/doctor.sh" "$tmp/mut/doctor.sh"
sd 'bad "cannot measure door image' 'ok "cannot measure door image' "$tmp/mut/doctor.sh"
out="$(run_doctor "" 1 "$tmp/mut/doctor.sh")"
case "$(door_line "$out")" in
    "✓ cannot measure door image"*) pass "mutation A: cannot-measure as ok turns case 3 non-red" ;;
    *) fail "mutation A: cannot-measure as ok stayed red — mutant survived: $(door_line "$out")" ;;
esac

# Mutant B: last commit of ANY path instead of COPY paths -> case 2 must turn red.
cp "$ROOT/scripts/doctor.sh" "$tmp/mut/doctor.sh"
sd -F "'%cI %h' -- \$door_paths" "'%cI %h'" "$tmp/mut/doctor.sh" \
    || echo "mutation B: sd failed (rc=$?)"
cmp -s "$ROOT/scripts/doctor.sh" "$tmp/mut/doctor.sh" && echo "mutation B: not applied"
out="$(run_doctor 2026-10-19T04:00:00.5Z "" "$tmp/mut/doctor.sh")"
case "$(door_line "$out")" in
    "✗ "*) pass "mutation B: any-path commit time turns case 2 red" ;;
    *) fail "mutation B: any-path commit time left case 2 passing — mutant survived: $(printf '%s\n' "$out" | head -5)" ;;
esac

if [ "$fails" -eq 0 ]; then
    echo "door-image doctor guardrails: $passes passed, 0 failed."
    exit 0
fi
echo "door-image doctor guardrails: $fails failed."
exit 1
