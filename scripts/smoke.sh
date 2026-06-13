#!/bin/sh
# Smoke test — adversarially verify the engine runs end-to-end (don't assume the happy path).
#   make smoke   or   ./scripts/smoke.sh
# Mode-aware: wiki-first is the default; vector/graph checks run only when DRUDGE_VECTOR=on.
# Note: /bin/sh (dash) has no pipefail → check each step's result explicitly.
set -eu
URL="${DRUDGE_URL:-http://localhost:7700}"
fail() { echo "FAIL: $1"; exit 1; }

case "$(printf '%s' "${DRUDGE_VECTOR:-off}" | tr '[:upper:]' '[:lower:]')" in
  on | 1 | true | yes) VEC=1 ;;
  *) VEC=0 ;;
esac

echo "1) containers…"
ps=$(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null) || fail "compose ps failed"
printf '%s\n' "$ps" | grep -qE 'drudge.*Up' || fail "drudge not running"
if [ "$VEC" = 1 ]; then
  printf '%s\n' "$ps" | grep -qE 'postgres.*(healthy|Up)' || fail "postgres not running (vector mode)"
fi
printf '%s\n' "$ps" | grep -qi 'restarting' && fail "crash-looping container: $(printf '%s' "$ps" | grep -i restarting)"
printf '%s\n' "$ps" | grep -E 'postgres|drudge|agent' || true

echo "2) drudge /health…"
[ "$(curl -s -o /dev/null -w '%{http_code}' -m5 "$URL/health")" = "200" ] || fail "/health != 200"

echo "3) /ask (real answer expected; fallback/error fails)…"
ans=$(curl -sf -m120 "$URL/ask" -H 'content-type: application/json' \
  -d '{"question":"what is oh-my-boring?"}' | jq -r '.answer') || fail "/ask call failed"
[ -n "$ans" ] && [ "$ans" != "null" ] || fail "empty ask response"
echo "   → $(printf '%s' "$ans" | head -c 90)…"

if [ "$VEC" = 1 ]; then
  echo "4) /audit (vector: corpus + graph measured)…"
  audit=$(curl -sf -m5 "$URL/audit") || fail "/audit failed"
  chunks=$(printf '%s' "$audit" | jq -r '.total_chunks // 0')
  edges=$(printf '%s' "$audit" | jq -r '.graph_edges // 0')
  [ "${chunks:-0}" -gt 0 ] || fail "empty corpus (chunks=0)"
  [ "${edges:-0}" -gt 0 ] || fail "no graph edges (structural graph not built)"
  echo "   chunks=$chunks edges=$edges"

  echo "5) /graph (CTE neighbors > 0)…"
  n=$(curl -sf -m90 "$URL/graph" -H 'content-type: application/json' \
    -d '{"query":"oh-my-boring"}' | jq -r '.graph_neighbors | length') || fail "/graph call failed"
  [ "${n:-0}" -gt 0 ] || fail "0 graph neighbors (graph recall not working)"
  echo "   graph_neighbors=$n"
else
  echo "4) wiki mode — vector/graph checks skipped (set DRUDGE_VECTOR=on to test them)"
fi

echo "OK: smoke passed — engine works end-to-end (adversarially verified)."
