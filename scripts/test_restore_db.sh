#!/bin/sh
# Guardrail tests for restore-db.sh — the destructive DB restore path.
# restore-db.sh drops + recreates the live database, so a bad/empty/missing backup must NEVER reach
# the drop step (a wiped DB with nothing to restore = data loss). These tests run offline with a stub
# `docker` on PATH that records every invocation; we assert the command ORDER, not a real Postgres.
#
# Owned guardrails:
#   1. Empty (0-byte) backup → ABORT before any dropdb.
#   2. Unreadable archive (pg_restore -l fails) → ABORT before any dropdb.
#   3. Missing backup → exit 1, no dropdb.
#   4. User declines the prompt → no dropdb.
#   5. Valid backup + confirm → validation (pg_restore -l) runs BEFORE dropdb.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/scripts/restore-db.sh"
PASS=0
FAIL=0

check() { # desc, condition-result(0/1)
  if [ "$2" = 0 ]; then echo "ok - $1"; PASS=$((PASS + 1)); else echo "FAIL - $1"; FAIL=$((FAIL + 1)); fi
}

setup() { # creates a tmp workspace with a stub `docker`; sets STUB_LOG / STUB_BIN
  WORK="$(mktemp -d)"
  STUB_LOG="$WORK/calls.log"
  STUB_BIN="$WORK/bin"
  mkdir -p "$STUB_BIN"
  : > "$STUB_LOG"
  cat > "$STUB_BIN/docker" <<STUB
#!/bin/sh
echo "\$*" >> "$STUB_LOG"
case "\$*" in
  "compose version") echo "Docker Compose version v2.29.0"; exit 0 ;;
  *"pg_restore -l"*) exit \${STUB_PGRESTORE_L_RC:-0} ;;
  *) exit 0 ;;
esac
STUB
  chmod +x "$STUB_BIN/docker"
}

teardown() { rm -rf "$WORK"; }

run() { # stdin_answer ; runs restore with stub docker on PATH; sets RC
  printf '%s\n' "$1" | PATH="$STUB_BIN:$PATH" STUB_PGRESTORE_L_RC="${STUB_PGRESTORE_L_RC:-0}" \
    FILE="${FILE:-}" BORING_BACKUP_DIR="$WORK/backups" sh "$SCRIPT" >"$WORK/out" 2>&1 || RC=$?
  RC="${RC:-0}"
}

dropdb_called() { grep -q "dropdb" "$STUB_LOG"; }

# --- 1. empty backup → abort before dropdb -----------------------------------
setup
: > "$WORK/empty.dump"  # 0 bytes
RC=0; FILE="$WORK/empty.dump" STUB_PGRESTORE_L_RC=0; run "y"
{ [ "$RC" = 1 ] && ! dropdb_called; }; check "empty backup aborts before dropdb" $?
teardown

# --- 2. unreadable archive (pg_restore -l fails) → abort before dropdb --------
setup
printf 'garbage' > "$WORK/bad.dump"  # non-empty but not a valid archive
RC=0; FILE="$WORK/bad.dump" STUB_PGRESTORE_L_RC=1; run "y"
{ [ "$RC" = 1 ] && ! dropdb_called; }; check "unreadable archive aborts before dropdb" $?
teardown

# --- 3. missing backup → exit 1, no dropdb -----------------------------------
setup
mkdir -p "$WORK/backups"  # empty dir, no FILE
RC=0; FILE="" STUB_PGRESTORE_L_RC=0; run "y"
{ [ "$RC" = 1 ] && ! dropdb_called; }; check "missing backup exits 1, no dropdb" $?
teardown

# --- 4. user declines prompt → no dropdb -------------------------------------
setup
printf 'realish' > "$WORK/ok.dump"
RC=0; FILE="$WORK/ok.dump" STUB_PGRESTORE_L_RC=0; run "n"
{ [ "$RC" = 0 ] && ! dropdb_called; }; check "declined prompt does not drop" $?
teardown

# --- 5. valid backup + confirm → validate BEFORE dropdb ----------------------
setup
printf 'realish' > "$WORK/ok.dump"
RC=0; FILE="$WORK/ok.dump" STUB_PGRESTORE_L_RC=0; run "y"
val_line=$(grep -n "pg_restore -l" "$STUB_LOG" | head -1 | cut -d: -f1)
drop_line=$(grep -n "dropdb" "$STUB_LOG" | head -1 | cut -d: -f1)
{ [ "$RC" = 0 ] && [ -n "$val_line" ] && [ -n "$drop_line" ] && [ "$val_line" -lt "$drop_line" ]; }
check "validation runs before dropdb on confirm" $?
teardown

echo
echo "restore-db guardrails: $PASS passed, $FAIL failed."
[ "$FAIL" = 0 ]
