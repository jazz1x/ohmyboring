#!/bin/sh
# Backup the oh-my-boring pgvector database to data/backups/ (custom format).
# Keeps the latest N backups (default 7); older ones are deleted automatically.
#   make backup-db
#   OMB_BACKUP_DIR=/path OMB_BACKUP_KEEP=10 make backup-db
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BACKUP_DIR="${OMB_BACKUP_DIR:-$ROOT/data/backups}"
KEEP="${OMB_BACKUP_KEEP:-7}"

if docker compose version 2>&1 | grep -q "Docker Compose"; then
  COMPOSE="docker compose"
else
  COMPOSE="docker-compose"
fi

mkdir -p "$BACKUP_DIR"
# The dump is the full corpus + raw query_log — owner-only. Restrict the dir, and write the dump
# under a tight umask so it lands 0600 even if OMB_BACKUP_DIR points outside the 700 data/ tree.
chmod 700 "$BACKUP_DIR" 2>/dev/null || true
umask 077

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
FILE="$BACKUP_DIR/ohmyboring_$TIMESTAMP.dump"
# Stage to a .partial name first: the ohmyboring_*.dump glob (used by restore + retention)
# never sees it, so a failed/empty dump can never become the "newest backup".
TMP="$FILE.partial"

echo "[backup] dumping boring database ..."
if ! $COMPOSE --profile vector exec -T boring-postgres pg_dump -U boring -d boring -Fc > "$TMP"; then
  rm -f "$TMP"
  echo "[backup] ABORT: pg_dump failed; no backup written." >&2
  exit 1
fi
if [ ! -s "$TMP" ]; then
  rm -f "$TMP"
  echo "[backup] ABORT: pg_dump produced an empty file; no backup written." >&2
  exit 1
fi
# Validate it is a readable custom-format archive before publishing it as a restore point.
if ! $COMPOSE --profile vector exec -T boring-postgres pg_restore -l >/dev/null 2>&1 < "$TMP"; then
  rm -f "$TMP"
  echo "[backup] ABORT: dump is not a valid pg_restore archive; no backup written." >&2
  exit 1
fi
mv "$TMP" "$FILE"
chmod 600 "$FILE" 2>/dev/null || true

# retention: keep only the latest $KEEP dumps
ls -t "$BACKUP_DIR"/ohmyboring_*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | while IFS= read -r old; do
  echo "[backup] pruning old backup: $old"
  rm -f "$old"
done

SIZE="$(du -h "$FILE" | cut -f1)"
echo "[backup] done — $FILE ($SIZE)"
