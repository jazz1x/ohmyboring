#!/bin/sh
# Restore the oh-my-boring pgvector database from a backup dump.
# Interactive by default. Stops drudge, drops/recreates the DB, restores, then starts drudge.
#   make restore-db                    # use latest backup
#   make restore-db FILE=path/to.dump  # use specific backup
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BACKUP_DIR="${OMB_BACKUP_DIR:-$ROOT/data/backups}"

if docker compose version 2>&1 | grep -q "Docker Compose"; then
  COMPOSE="docker compose"
else
  COMPOSE="docker-compose"
fi

if [ -n "${FILE:-}" ]; then
  BACKUP="$FILE"
else
  BACKUP="$(ls -t "$BACKUP_DIR"/ohmyboring_*.dump 2>/dev/null | head -1)"
fi

if [ -z "$BACKUP" ] || [ ! -f "$BACKUP" ]; then
  echo "[restore] no backup found in $BACKUP_DIR"
  exit 1
fi

echo "[restore] target backup: $BACKUP"

# Validate the archive BEFORE touching the live DB. A 0-byte/partial/wrong-format
# dump (e.g. a failed pg_dump) must never reach the drop step, or a bad backup
# wipes the database with nothing to restore. `pg_restore -l` reads the archive
# TOC without restoring and fails on any non-custom-format / corrupt input.
if [ ! -s "$BACKUP" ]; then
  echo "[restore] ABORT: backup is empty (0 bytes): $BACKUP — live DB left untouched."
  exit 1
fi
echo "[restore] validating backup archive ..."
if ! $COMPOSE --profile vector exec -T boring-postgres pg_restore -l >/dev/null 2>&1 < "$BACKUP"; then
  echo "[restore] ABORT: '$BACKUP' is not a readable pg_restore custom-format archive — live DB left untouched."
  exit 1
fi

printf '⚠️  This will DESTROY the current oh-my-boring database and replace it with the backup. Continue? [y/N] '
read ans
[ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "aborted."; exit 0; }

echo "[restore] stopping boring-drudge ..."
$COMPOSE --profile vector stop boring-drudge || true

echo "[restore] recreating database ..."
$COMPOSE --profile vector exec -T boring-postgres dropdb -U boring --if-exists boring
$COMPOSE --profile vector exec -T boring-postgres createdb -U boring boring

echo "[restore] restoring from $BACKUP ..."
$COMPOSE --profile vector exec -T boring-postgres pg_restore -U boring -d boring -Fc < "$BACKUP"

echo "[restore] starting boring-drudge ..."
$COMPOSE --profile vector start boring-drudge

echo "[restore] done — the engine will run a startup sync shortly."
