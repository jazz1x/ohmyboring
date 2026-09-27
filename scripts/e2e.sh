#!/bin/sh
# e2e.sh — black-box service-contract test against a RUNNING stack.
#
#   make e2e   or   ./scripts/e2e.sh
#
# This does NOT test drudge internals (those stay in Rust #[cfg(test)]).
# It exercises the live HTTP/MCP surface the way an agent does:
#   1. remember  — write a throwaway note via the `remember` MCP tool
#   2. search    — vector mode: the note is the top hit for its nonce
#   3. recall    — read it back via `recall`; assert body round-trips
#   3b. query-log — vector mode: the recall call left an mcp.recall row
#   4. neighbors — wiki mode: rejected with -32603; vector mode: returns results
#   6. forget    — refused: closed during the migration
#   7. recall    — the note is still there
#
# This needs a live stack on :7700 (make up). It does NOT start, stop, or build
# anything. If the stack is down it SKIPS (exit 0), never failing CI on a missing
# engine.
set -eu
URL="${BORING_URL:-http://localhost:7700}"
# shellcheck source=lib/drudge_health_readiness.sh
. "$(cd "$(dirname "$0")/lib" && pwd)/drudge_health_readiness.sh"

fail() { echo "FAIL: $1"; exit 1; }
skip() { echo "SKIP: $1"; exit 0; }

command -v curl >/dev/null 2>&1 || fail "curl not found"
command -v jq   >/dev/null 2>&1 || skip "jq not found — install: brew install jq / apt-get install jq"

# --- stack-up check (friendly skip when down, like smoke.sh) ------------------
echo "0) stack up? (engine /health)…"
if [ "$(curl -s -o /dev/null -w '%{http_code}' -m5 "$URL/health" 2>/dev/null)" != "200" ]; then
  skip "engine not up at $URL (run: make up) — e2e needs a live stack"
fi
# A reachable engine whose DB is down would fail every write below with a confusing error.
check_drudge_db_healthy "$URL" || skip "engine at $URL reports db_healthy=false — postgres is degraded"

# --- mode check ----------------------------------------------------------------
# Resolve mode: honor explicit BORING_VECTOR, else ask /audit
# (vector backend returns total_chunks; wiki mode returns 500).
case "$(printf '%s' "${BORING_VECTOR:-}" | tr '[:upper:]' '[:lower:]')" in
  on | 1 | true | yes) VEC=1 ;;
  off | 0 | false | no) VEC=0 ;;
  *)
    if probe=$(curl -sf -m5 "$URL/audit" 2>/dev/null) \
       && [ "$(printf '%s' "$probe" | jq -r 'has("total_chunks")')" = "true" ]; then
      VEC=1
    else
      VEC=0
    fi
    ;;
esac
if [ "$VEC" = 1 ]; then
  echo "   vector mode (BORING_VECTOR=on) confirmed"
else
  echo "   wiki mode (BORING_VECTOR=off) confirmed"
fi

MCP="$URL/mcp"
# A unique, single-line, lowercase nonce. recall lowercases the body, so the
# assertion uses one lowercased token that survives case-folding.
NONCE="e2e$(date +%s)$$"
TITLE="e2e throwaway $NONCE"
BODY="e2e end to end probe note $NONCE for a service contract round trip do not keep"
CLAIMS='[{"subject":"e2e-throwaway","predicate":"follow-up","value":"verify next_actions endpoint","kind":"next","confidence":"certain"}]'

# --- 1) remember ----------------------------------------------------------------
echo "1) remember (MCP tools/call remember)…"
req=$(jq -nc --arg t "$TITLE" --arg b "$BODY" --argjson claims "$CLAIMS" \
  '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:"remember",arguments:{title:$t,body:$b,origin:"personal",repo:"e2e-throwaway",claims:$claims}}}')
resp=$(curl -sf -m120 "$MCP" -H 'content-type: application/json' -d "$req") \
  || fail "remember call failed (curl)"
err=$(printf '%s' "$resp" | jq -r '.error.message // empty')
[ -z "$err" ] || fail "remember returned JSON-RPC error: $err"
ack=$(printf '%s' "$resp" | jq -r '.result.content[0].text // empty')
printf '%s' "$ack" | grep -q "remembered" || fail "remember ack missing 'remembered': $ack"
# remember ack text contains "wiki/wiki-NNNN.md"; derive the source_path used by /search.
note_path=$(printf '%s' "$ack" | grep -oE 'wiki/wiki-[0-9]+\.md' | head -n1)
[ -n "$note_path" ] || fail "could not derive note path from remember ack: $ack"
note_path="/vault/$note_path"
echo "   → $ack (path: $note_path)"

# --- 2) search (vector mode only) -----------------------------------------------
if [ "$VEC" = 1 ]; then
  echo "2) search (HTTP /search — assert the note is the top hit)…"
  req=$(jq -nc --arg q "$NONCE" '{query:$q,max_results:3,max_tokens:2000}')
  resp=$(curl -sf -m30 "$URL/search" -H 'content-type: application/json' -d "$req") \
    || fail "search call failed (curl)"
  top=$(printf '%s' "$resp" | jq -r '.hits[0].source_path // empty')
  [ "$top" = "$note_path" ] \
    || fail "search top hit does not match throwaway note; expected $note_path, got: $top"
  echo "   → search top hit matches throwaway note"
else
  echo "2) search (HTTP /search — skipped in wiki mode)"
fi

# --- 3) recall (round-trip) -----------------------------------------------------
echo "3) recall (MCP tools/call recall — assert round-trip)…"
req=$(jq -nc --arg q "$NONCE" \
  '{jsonrpc:"2.0",id:3,method:"tools/call",params:{name:"recall",arguments:{query:$q}}}')
resp=$(curl -sf -m120 "$MCP" -H 'content-type: application/json' -d "$req") \
  || fail "recall call failed (curl)"
err=$(printf '%s' "$resp" | jq -r '.error.message // empty')
[ -z "$err" ] || fail "recall returned JSON-RPC error: $err"
text=$(printf '%s' "$resp" | jq -r '.result.content[0].text // empty')
printf '%s' "$text" | grep -q "$NONCE" \
  || fail "round-trip failed — nonce '$NONCE' not found in recall result: $text"
echo "   → round-trip OK (nonce recalled)"

# --- 3b) query_log records the MCP call (vector mode only) ------------------------
if [ "$VEC" = 1 ]; then
  echo "3b) query-log (GET /query-log — assert the recall call left an mcp.recall row)…"
  # The log write is a detached task, so poll briefly instead of asserting immediately.
  # Wiki mode records nothing here because `store` is None — pre-existing behaviour,
  # not a new gap, so this step runs in the vector branch only.
  row=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    row=$(curl -sf -m10 "$URL/query-log?limit=50" | jq -r --arg n "$NONCE" \
      '[.entries[]? | select(.endpoint == "mcp.recall") | select(.query | contains($n))][0].endpoint // empty' \
      2>/dev/null) || true
    [ -n "$row" ] && break
    sleep 1
  done
  [ -n "$row" ] \
    || fail "no query_log row with endpoint=mcp.recall containing nonce '$NONCE' after 10s"
  echo "   → query_log has an mcp.recall row for the nonce"
fi

# --- 4) neighbors (mode-specific contract) --------------------------------------
if [ "$VEC" = 0 ]; then
  echo "4) neighbors (MCP tools/call neighbors — assert vector-off error -32603)…"
  req=$(jq -nc --arg q "$NONCE" \
    '{jsonrpc:"2.0",id:4,method:"tools/call",params:{name:"neighbors",arguments:{query:$q}}}')
  resp=$(curl -sf -m30 "$MCP" -H 'content-type: application/json' -d "$req") \
    || fail "neighbors call failed (curl)"
  code=$(printf '%s' "$resp" | jq -r '.error.code // empty')
  [ "$code" = "-32603" ] \
    || fail "expected JSON-RPC error code -32603 for neighbors in wiki mode, got: $resp"
  echo "   → neighbors correctly rejected with -32603"
else
  echo "4) neighbors (MCP tools/call neighbors — assert vector mode succeeds)…"
  req=$(jq -nc --arg q "$NONCE" \
    '{jsonrpc:"2.0",id:4,method:"tools/call",params:{name:"neighbors",arguments:{query:$q}}}')
  resp=$(curl -sf -m30 "$MCP" -H 'content-type: application/json' -d "$req") \
    || fail "neighbors call failed (curl)"
  err=$(printf '%s' "$resp" | jq -r '.error.message // empty')
  [ -z "$err" ] || fail "neighbors returned JSON-RPC error in vector mode: $err"
  echo "   → neighbors returned results in vector mode"

  # 4b) tags must actually VARY by tool. The nonce reached the engine through two
  # different tools, so it must come back under two different endpoint tags. Asserting
  # only `mcp.recall` (step 3b) cannot see a wiring bug that hardcodes the tag: a mutant
  # passing a literal tool name to `mcp_endpoint_tag` destroys per-tool attribution —
  # the whole point of this table — while every unit test, clippy and rg gate still pass.
  # That mutant was run and survived everything before this step existed.
  echo "4b) query-log (assert the tag varies by tool — mcp.neighbors, not just mcp.recall)…"
  tags=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    tags=$(curl -sf -m10 "$URL/query-log?limit=50" | jq -r --arg n "$NONCE" \
      '[.entries[]? | select(.query | contains($n)) | .endpoint] | unique | join(",")' \
      2>/dev/null) || true
    case "$tags" in *mcp.neighbors*) break ;; esac
    sleep 1
  done
  case "$tags" in
    *mcp.neighbors*) ;;
    *) fail "no mcp.neighbors row for nonce '$NONCE' — tags seen: [$tags]. The endpoint tag is not derived from the tool name." ;;
  esac
  case "$tags" in
    *mcp.recall*) ;;
    *) fail "mcp.recall row vanished for nonce '$NONCE' — tags seen: [$tags]" ;;
  esac
  echo "   → nonce appears under two distinct tags: $tags"
fi

# --- 5) next_actions (mode-specific contract) -----------------------------------
if [ "$VEC" = 0 ]; then
  echo "5) next_actions (HTTP /next_actions — assert vector-off error)…"
  resp=$(curl -sf -m30 "$URL/next_actions" -H 'content-type: application/json' -d '{"project":"e2e-throwaway"}') \
    || fail "next_actions call failed (curl)"
  code=$(printf '%s' "$resp" | jq -r '.error.code // empty')
  [ "$code" = "-32603" ] \
    || fail "expected JSON-RPC-style error -32603 for next_actions in wiki mode, got: $resp"
  echo "   → next_actions correctly rejected with -32603"
else
  echo "5) next_actions (HTTP /next_actions + MCP — assert claim round-trip)…"
  resp=$(curl -sf -m30 "$URL/next_actions" -H 'content-type: application/json' -d '{"project":"e2e-throwaway"}') \
    || fail "next_actions HTTP call failed (curl)"
  ans=$(printf '%s' "$resp" | jq -r '.answer // empty')
  printf '%s' "$ans" | grep -q "verify next_actions endpoint" \
    || fail "next_actions HTTP answer missing the planted claim: $ans"
  echo "   → HTTP /next_actions returned the planted next-action claim"

  req=$(jq -nc \
    '{jsonrpc:"2.0",id:5,method:"tools/call",params:{name:"next_actions",arguments:{project:"e2e-throwaway"}}}')
  resp=$(curl -sf -m30 "$MCP" -H 'content-type: application/json' -d "$req") \
    || fail "next_actions MCP call failed (curl)"
  err=$(printf '%s' "$resp" | jq -r '.error.message // empty')
  [ -z "$err" ] || fail "next_actions returned JSON-RPC error in vector mode: $err"
  ans=$(printf '%s' "$resp" | jq -r '.result.structuredContent.answer // empty')
  printf '%s' "$ans" | grep -q "verify next_actions endpoint" \
    || fail "next_actions MCP structuredContent missing the planted claim: $ans"
  echo "   → MCP next_actions returned the planted next-action claim"
fi

# --- 6) forget ------------------------------------------------------------------
echo "6) forget (MCP tools/call forget — closed during the migration)…"
req=$(jq -nc --arg t "$TITLE" \
  '{jsonrpc:"2.0",id:5,method:"tools/call",params:{name:"forget",arguments:{title:$t}}}')
resp=$(curl -sf -m30 "$MCP" -H 'content-type: application/json' -d "$req") \
  || fail "forget call failed (curl)"
err=$(printf '%s' "$resp" | jq -r '.error.message // empty')
printf '%s' "$err" | grep -q "closed during the migration" \
  || fail "forget was not refused as closed during the migration: $resp"
echo "   → refused: $err"

# --- 7) recall after forget -----------------------------------------------------
echo "7) recall after forget (assert the throwaway note is still there)…"
req=$(jq -nc --arg q "$NONCE" \
  '{jsonrpc:"2.0",id:7,method:"tools/call",params:{name:"recall",arguments:{query:$q}}}')
resp=$(curl -sf -m120 "$MCP" -H 'content-type: application/json' -d "$req") \
  || fail "recall call failed (curl)"
err=$(printf '%s' "$resp" | jq -r '.error.message // empty')
[ -z "$err" ] || fail "recall returned JSON-RPC error: $err"
text=$(printf '%s' "$resp" | jq -r '.result.content[0].text // empty')
printf '%s' "$text" | grep -q "$NONCE" \
  || fail "note with nonce '$NONCE' gone after a refused forget: $text"
echo "   → note still recalled"

if [ "$VEC" = 0 ]; then
  echo "OK: e2e passed — wiki-mode remember→recall round-trips, forget refused, vector-only neighbors/next_actions rejected."
else
  echo "OK: e2e passed — vector-mode remember→search→recall→neighbors→next_actions round-trips, forget refused."
fi

echo "NOTE: the throwaway note (title '$TITLE' / $note_path) stays in the vault — forget is closed during the migration."
