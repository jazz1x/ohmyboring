#!/bin/sh
# Export the Surreal side of the ohmyboring store to data/backups/ (SurrealQL text).
# The pg dump holds the other half (sync_state, event_log, ...): a Surreal dump alone is not a restore point.
# Same folder, keep-count and timestamp scheme as backup-db.sh, so ohmyboring_<TS>.dump and ohmyboring_<TS>.surql pair by prefix.
#   make backup-surreal                         # new timestamp (no pg pair; the first line says so)
#   make backup-surreal PAIR=data/backups/ohmyboring_20261116_120000.dump   # reuse that dump's timestamp
#   BORING_BACKUP_DIR=/path BORING_BACKUP_KEEP=10 SURREAL_CONTAINER=name make backup-surreal
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BACKUP_DIR="${BORING_BACKUP_DIR:-$ROOT/data/backups}"
KEEP="${BORING_BACKUP_KEEP:-7}"
CONTAINER="${SURREAL_CONTAINER:-boring-surreal}"
SURREAL_PASSWORD="${BORING_SURREAL_PASSWORD:-boring}"

# Namespace/database come from boring.json vector.surreal (engine default `boring`, drudge/src/config.rs:330,352-353).
# An unreadable config loads as {} and would silently name the default, so refuse instead.
NSDB="$(uv run --locked python -c '
from ohmyboring import config
cfg = config.load()
if not cfg:
    raise SystemExit(1)
s = (cfg.get("vector") or {}).get("surreal") or {}
print(s.get("namespace", "boring"), s.get("database", "boring"))
')" || { echo "[backup-surreal] ABORT: boring.json unreadable — namespace/database unknown; no backup written." >&2; exit 1; }
NS="${NSDB% *}"
DB="${NSDB#* }"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true
umask 077

if [ -n "${PAIR:-}" ]; then
  PAIR_NAME="$(basename "$PAIR" .dump)"
  [ -f "$BACKUP_DIR/$PAIR_NAME.dump" ] || { echo "[backup-surreal] ABORT: PAIR dump not found: $BACKUP_DIR/$PAIR_NAME.dump" >&2; exit 1; }
  PREFIX="$PAIR_NAME"
else
  PREFIX="ohmyboring_$(date +%Y%m%d_%H%M%S)"
fi
FILE="$BACKUP_DIR/$PREFIX.surql"
TMP="$FILE.partial"

if [ -f "$BACKUP_DIR/$PREFIX.dump" ]; then
  echo "[backup-surreal] Surreal 만 — 짝 pg 덤프 $PREFIX.dump 필요 (있음: $BACKUP_DIR/$PREFIX.dump)"
else
  echo "[backup-surreal] Surreal 만 — 짝 pg 덤프 $PREFIX.dump 필요 (없음: 같은 접두사의 pg 덤프가 없다)"
fi

echo "[backup-surreal] exporting $NS/$DB from $CONTAINER ..."
# `surreal export [OPTIONS] [FILE]`: FILE `-` writes to stdout; flags per `surreal export --help` of the pinned image.
# `--log warn`: at the default level the CLI's INFO lines land on stdout, i.e. INSIDE the dump (measured E2), ahead of `OPTION IMPORT;`.
if ! docker exec "$CONTAINER" /surreal export --log warn --endpoint http://127.0.0.1:8000 --username root \
  --password "$SURREAL_PASSWORD" --namespace "$NS" --database "$DB" - > "$TMP"; then
  rm -f "$TMP"
  echo "[backup-surreal] ABORT: surreal export failed; no backup written." >&2
  exit 1
fi
if [ ! -s "$TMP" ]; then
  rm -f "$TMP"
  echo "[backup-surreal] ABORT: surreal export produced an empty file; no backup written." >&2
  exit 1
fi
mv "$TMP" "$FILE"
chmod 600 "$FILE" 2>/dev/null || true

# retention: keep the newest $KEEP prefixes; a pruned prefix loses its pg dump and its Surreal dump together
ls "$BACKUP_DIR"/ohmyboring_*.dump "$BACKUP_DIR"/ohmyboring_*.surql 2>/dev/null \
  | sed 's/\.[a-z]*$//' | sort -ur | tail -n +"$((KEEP + 1))" | while IFS= read -r old; do
  echo "[backup-surreal] pruning old backup pair: $old.{dump,surql}"
  rm -f "$old.dump" "$old.surql"
done

SIZE="$(du -h "$FILE" | cut -f1)"
echo "[backup-surreal] done — $FILE ($SIZE)"
