#!/bin/sh
# Smoke test — adversarially verify the engine runs end-to-end (don't assume the happy path).
#   make smoke   or   ./scripts/smoke.sh
# Mode-aware: wiki-first is the default; vector/graph checks run only on a vector-mode stack.
# Mode is detected from the RUNNING engine (not a possibly-unsourced env): /audit is
# pgvector-only — it returns total_chunks on a vector backend and HTTP 500 in wiki mode.
# An explicit DRUDGE_VECTOR (if exported) still forces the mode.
# Note: /bin/sh (dash) has no pipefail → check each step's result explicitly.
set -eu
URL="${DRUDGE_URL:-http://localhost:7700}"
fail() { echo "FAIL: $1"; exit 1; }

# Resolve mode: honor an explicit DRUDGE_VECTOR, else ask the live engine.
case "$(printf '%s' "${DRUDGE_VECTOR:-}" | tr '[:upper:]' '[:lower:]')" in
  on | 1 | true | yes) VEC=1 ;;
  off | 0 | false | no) VEC=0 ;;
  *)
    # Unset/unknown → detect from reality: /audit returns total_chunks only on the
    # vector backend (HTTP 500 in wiki mode). curl -f makes the 500 a non-zero exit.
    if probe=$(curl -sf -m5 "$URL/audit" 2>/dev/null) \
       && [ "$(printf '%s' "$probe" | jq -r 'has("total_chunks")')" = "true" ]; then
      VEC=1
    else
      VEC=0
    fi
    ;;
esac

echo "1) containers…"
if docker compose version 2>&1 | grep -q "Docker Compose"; then
  COMPOSE="docker compose"
else
  COMPOSE="docker-compose"
fi
ps=$($COMPOSE ps --format '{{.Name}} {{.Status}}' 2>/dev/null) || fail "compose ps failed"
printf '%s\n' "$ps" | grep -qE 'ohmyboring.*Up' || fail "ohmyboring not running"
if [ "$VEC" = 1 ]; then
  printf '%s\n' "$ps" | grep -qE 'postgres.*(healthy|Up)' || fail "postgres not running (vector mode)"
fi
printf '%s\n' "$ps" | grep -qi 'restarting' && fail "crash-looping container: $(printf '%s' "$ps" | grep -i restarting)"
printf '%s\n' "$ps" | grep -E 'postgres|ohmyboring|agent' || true

echo "2) engine /health…"
[ "$(curl -s -o /dev/null -w '%{http_code}' -m5 "$URL/health")" = "200" ] || fail "/health != 200"

echo "3) /ask (real answer expected; fallback/error fails)…"
ans=$(curl -sf -m120 "$URL/ask" -H 'content-type: application/json' \
  -d '{"question":"what is ohmyboring?"}' | jq -r '.answer') || fail "/ask call failed"
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
    -d '{"query":"ohmyboring"}' | jq -r '.graph_neighbors | length') || fail "/graph call failed"
  [ "${n:-0}" -gt 0 ] || fail "0 graph neighbors (graph recall not working)"
  echo "   graph_neighbors=$n"
else
  echo "4) wiki mode detected (/audit has no vector corpus) — vector/graph checks skipped"
fi

echo "OK: smoke passed — engine works end-to-end (adversarially verified)."
