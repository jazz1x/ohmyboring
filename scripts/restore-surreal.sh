#!/bin/sh
# Import a Surreal dump into an EMPTY Surreal container, then make the next relates projection run Full.
# No default target: restore-db.sh's "latest dump into the live DB" is deliberately not copied here.
#   make restore-surreal FILE=data/backups/ohmyboring_<TS>.surql TARGET=<surreal container> \
#        PG_CONTAINER=<postgres container> PG_DSN=<dsn as that container sees it>
# Refuses (exit 1, nothing written) when an argument is missing, the dump has no pg pair of the same prefix,
# boring.json is unreadable, or the target namespace/database is not empty.
set -eu

FILE="${FILE:-}"
TARGET="${TARGET:-}"
PG_CONTAINER="${PG_CONTAINER:-}"
PG_DSN="${PG_DSN:-}"
SURREAL_PASSWORD="${BORING_SURREAL_PASSWORD:-boring}"

refuse() { echo "[restore-surreal] REFUSED: $1 — nothing written." >&2; exit 1; }

[ -n "$FILE" ] || refuse "FILE (the .surql dump) is required"
[ -n "$TARGET" ] || refuse "TARGET (surreal container to import into) is required; there is no default"
[ -n "$PG_CONTAINER" ] || refuse "PG_CONTAINER (postgres container holding sync_state) is required; there is no default"
[ -n "$PG_DSN" ] || refuse "PG_DSN (database to invalidate the projection marker in) is required; there is no default"
[ -s "$FILE" ] || refuse "dump missing or empty: $FILE"
case "$FILE" in *.surql) ;; *) refuse "FILE must be a .surql dump: $FILE" ;; esac

PAIR="${FILE%.surql}.dump"
[ -s "$PAIR" ] || refuse "no pg pair for this dump (expected $PAIR) — a Surreal dump alone is not a restore point"

NSDB="$(uv run --locked python -c '
from ohmyboring import config
cfg = config.load()
if not cfg:
    raise SystemExit(1)
s = (cfg.get("vector") or {}).get("surreal") or {}
print(s.get("namespace", "boring"), s.get("database", "boring"))
')" || refuse "boring.json unreadable — namespace/database unknown"
NS="${NSDB% *}"
DB="${NSDB#* }"

SURREAL="docker exec -i $TARGET /surreal"
CONN="--endpoint http://127.0.0.1:8000 --username root --password $SURREAL_PASSWORD --namespace $NS --database $DB"

# A failing statement does not stop a Surreal import, so importing over rows would blend old and new: empty only.
# INFO FOR DB lists every table; a fresh namespace/database has none. A query that cannot be answered is "cannot measure" = refuse.
INFO="$(echo 'INFO FOR DB;' | $SURREAL sql $CONN --json --hide-welcome)" || refuse "cannot read $NS/$DB on $TARGET"
EMPTY=0
echo "$INFO" | python3 -c '
import json, sys
def tables(node):
    if isinstance(node, dict):
        if "tables" in node:
            return node["tables"]
        for v in node.values():
            t = tables(v)
            if t is not None:
                return t
    if isinstance(node, list):
        for v in node:
            t = tables(v)
            if t is not None:
                return t
    return None
t = tables(json.load(sys.stdin))
if t is None:
    sys.exit(2)
sys.exit(1 if t else 0)
' || EMPTY=$?
case "$EMPTY" in
  0) ;;
  1) refuse "$NS/$DB on $TARGET already has tables — import only into an empty fresh namespace/database" ;;
  *) refuse "could not read the table list of $NS/$DB on $TARGET" ;;
esac

echo "[restore-surreal] importing $FILE into $TARGET ($NS/$DB), pg pair $PAIR ..."
docker cp "$FILE" "$TARGET:/tmp/restore.surql"
$SURREAL import $CONN /tmp/restore.surql || { echo "[restore-surreal] FAILED: surreal import returned an error; $TARGET may be half-filled — discard it." >&2; exit 1; }

# Next relates projection must run Full: same DELETE as drudge/src/store/relates_projection.rs:503-507,
# key = SURREAL_FULL_KEY (relates_projection.rs:25).
docker exec -i "$PG_CONTAINER" psql "$PG_DSN" -v ON_ERROR_STOP=1 \
  -c "DELETE FROM sync_state WHERE key = 'relates_projection_full_surreal';" \
  || { echo "[restore-surreal] FAILED: imported, but the projection marker was NOT cleared — run the DELETE by hand before the next projection." >&2; exit 1; }

# E2 (11-16, v3.3.5): export carries the chunk/claim HNSW and BM25 index definitions; INFO FOR TABLE on the
# restored db listed them. If a future version drops them, catch_up (vector_surreal.rs:2265) redefines them.
echo "[restore-surreal] done — projection marker cleared."
