#!/bin/sh
# doctor's vector index parity line (d5e): on backend=surreal the host CLI's `vector-check` compares
# pg and Surreal row counts and id-set hashes; different or unmeasurable is red, pgvector is an
# information line. The host CLI is a stub keyed on FAKE_VC; the real binary against real
# containers is run by hand (see the commit body).
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fails=0
passes=0
pass() { echo "ok - $1"; passes=$((passes + 1)); }
fail() { echo "FAIL: $1"; fails=$((fails + 1)); }

boring="$tmp/boring"
mkdir -p "$boring/drudge/target/release" "$tmp/home" "$tmp/fakebin" "$tmp/mut"
ln -s "$ROOT/scripts/lib" "$tmp/mut/lib"

cat > "$boring/drudge/target/release/drudge" <<'SH'
#!/bin/sh
[ "${1:-}" = vector-check ] || exit 0
case "$FAKE_VC" in
  pg) echo "backend=pgvector — 해당 없음 (대조는 backend=surreal 일 때만)" ;;
  same)
    echo "chunk: pg rows=3 hash=aa | surreal rows=3 hash=aa | same"
    echo "claim(open): pg rows=2 hash=bb | surreal rows=2 hash=bb | same" ;;
  diff)
    echo "chunk: pg rows=3 hash=aa | surreal rows=2 hash=cc | DIFFERENT"
    echo "claim(open): pg rows=2 hash=bb | surreal rows=2 hash=bb | same"
    echo "Error: vector-check: pg 와 surreal 의 행 수 또는 id 집합 해시가 다름" >&2
    exit 1 ;;
  fail)
    echo "Error: BORING_VECTOR is off" >&2
    exit 1 ;;
esac
SH
chmod +x "$boring/drudge/target/release/drudge"
printf '#!/bin/sh\nexit 0\n' > "$tmp/fakebin/docker"
chmod +x "$tmp/fakebin/docker"

run_doctor() { # <FAKE_VC> [doctor.sh]
    script="${2:-$ROOT/scripts/doctor.sh}"
    env HOME="$tmp/home" BORING_HOME="$boring" BORING_URL="http://127.0.0.1:1" \
        DOOR_URL="http://127.0.0.1:1" PATH="$tmp/fakebin:$PATH" DOCKER_BIN="$tmp/fakebin/docker" \
        BORING_SKIP_LEDGER_PROBE=1 BORING_SKIP_CONFIG_PROBE=1 FAKE_VC="$1" \
        sh "$script" 2>&1 || true
}
vc_line() { printf '%s\n' "$1" | grep -E 'vector index parity' || true; }
# POSIX in-place substitute for the mutants (CI has no sd): subst <file> <sed-script>
subst() { sed "$2" "$1" > "$1.new" && mv "$1.new" "$1"; }

# 1. control: pgvector -> an information line, not red
line="$(vc_line "$(run_doctor pg)")"
case "$line" in
    "✓ vector index parity: backend=pgvector — 해당 없음"*) pass "1 pgvector is an information line" ;;
    *) fail "1 pgvector — got: ${line:-<no line>}" ;;
esac

# 2. control: surreal, both tables the same -> green with the counts
line="$(vc_line "$(run_doctor same)")"
case "$line" in
    "✓ vector index parity: chunk: pg rows=3"*"| same claim(open): pg rows=2"*"| same"*) pass "2 surreal in step is green, with both tables" ;;
    *) fail "2 surreal in step — got: ${line:-<no line>}" ;;
esac

# 3. violation: surreal behind pg -> red, naming the table that differs
line="$(vc_line "$(run_doctor diff)")"
case "$line" in
    "✗ vector index parity — chunk: pg rows=3"*"DIFFERENT"*) pass "3 surreal behind pg is red" ;;
    *) fail "3 surreal behind pg — got: ${line:-<no line>}" ;;
esac

# 4. cannot measure (the command itself fails): red with the reason
line="$(vc_line "$(run_doctor fail)")"
case "$line" in
    "✗ vector index parity — Error: BORING_VECTOR is off"*) pass "4 a failing check is red with its reason" ;;
    *) fail "4 failing check — got: ${line:-<no line>}" ;;
esac

# 5. cannot measure (no binary) while boring.json asks for surreal: red
mv "$boring/drudge/target/release/drudge" "$tmp/drudge.off"
echo '{"schema_version": 2, "vector": {"backend": "surreal"}}' > "$boring/boring.json"
line="$(vc_line "$(run_doctor same)")"
case "$line" in
    "✗ cannot measure vector index parity"*"host CLI"*) pass "5 no binary + surreal configured is red" ;;
    *) fail "5 no binary + surreal — got: ${line:-<no line>}" ;;
esac

# 6. control: no binary, boring.json without surreal -> not applicable
echo '{"schema_version": 2, "vector": {"backend": "pgvector"}}' > "$boring/boring.json"
line="$(vc_line "$(run_doctor same)")"
case "$line" in
    "✓ vector index parity: backend=pgvector — 해당 없음"*) pass "6 no binary + pgvector is not applicable" ;;
    *) fail "6 no binary + pgvector — got: ${line:-<no line>}" ;;
esac
mv "$tmp/drudge.off" "$boring/drudge/target/release/drudge"
rm "$boring/boring.json"

# Mutants run on copies of doctor.sh; each must flip a verdict above.
# A: a difference rendered as ok -> case 3 must stop being red.
cp "$ROOT/scripts/doctor.sh" "$tmp/mut/doctor.sh"
subst "$tmp/mut/doctor.sh" 's/bad "vector index parity/ok "vector index parity/'
cmp -s "$ROOT/scripts/doctor.sh" "$tmp/mut/doctor.sh" && echo "mutation A: not applied"
line="$(vc_line "$(run_doctor diff "$tmp/mut/doctor.sh")")"
case "$line" in
    "✓ "*) pass "mutation A: difference as ok turns case 3 non-red" ;;
    *) fail "mutation A: survived — $line" ;;
esac

# B: exit status of the check ignored -> cases 3 and 4 must stop being red.
cp "$ROOT/scripts/doctor.sh" "$tmp/mut/doctor.sh"
subst "$tmp/mut/doctor.sh" 's/if vector_out="$("$vector_cli" vector-check 2>&1)"; then/vector_out="$("$vector_cli" vector-check 2>\&1)" || true; if true; then/'
cmp -s "$ROOT/scripts/doctor.sh" "$tmp/mut/doctor.sh" && echo "mutation B: not applied"
line="$(vc_line "$(run_doctor fail "$tmp/mut/doctor.sh")")"
case "$line" in
    "✓ "*) pass "mutation B: ignoring the exit status turns case 4 non-red" ;;
    *) fail "mutation B: survived — $line" ;;
esac

# C: cannot-measure (no binary) rendered as ok -> case 5 must stop being red.
mv "$boring/drudge/target/release/drudge" "$tmp/drudge.off"
echo '{"schema_version": 2, "vector": {"backend": "surreal"}}' > "$boring/boring.json"
cp "$ROOT/scripts/doctor.sh" "$tmp/mut/doctor.sh"
subst "$tmp/mut/doctor.sh" 's/bad "cannot measure vector index parity/ok "cannot measure vector index parity/'
cmp -s "$ROOT/scripts/doctor.sh" "$tmp/mut/doctor.sh" && echo "mutation C: not applied"
line="$(vc_line "$(run_doctor same "$tmp/mut/doctor.sh")")"
case "$line" in
    "✓ cannot measure"*) pass "mutation C: cannot-measure as ok turns case 5 non-red" ;;
    *) fail "mutation C: survived — $line" ;;
esac

if [ "$fails" -eq 0 ]; then
    echo "vector-parity doctor guardrails: $passes passed, 0 failed."
    exit 0
fi
echo "vector-parity doctor guardrails: $fails failed."
exit 1
