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
#   4. neighbors — wiki mode: rejected with -32603; vector mode: returns results
#   5. forget    — delete the throwaway note
#   6. recall    — assert deletion
#
# This needs a live stack on :7700 (make up). It does NOT start, stop, or build
# anything. If the stack is down it SKIPS (exit 0), never failing CI on a missing
# engine.
set -eu
URL="${DRUDGE_URL:-http://localhost:7700}"

fail() { echo "FAIL: $1"; exit 1; }
skip() { echo "SKIP: $1"; exit 0; }

command -v curl >/dev/null 2>&1 || fail "curl not found"
command -v jq   >/dev/null 2>&1 || skip "jq not found — install: brew install jq / apt-get install jq"

# --- stack-up check (friendly skip when down, like smoke.sh) ------------------
echo "0) stack up? (engine /health)…"
if [ "$(curl -s -o /dev/null -w '%{http_code}' -m5 "$URL/health" 2>/dev/null)" != "200" ]; then
  skip "engine not up at $URL (run: make up) — e2e needs a live stack"
fi

# --- mode check ----------------------------------------------------------------
# Resolve mode: honor explicit DRUDGE_VECTOR, else ask /audit (vector backend
# returns total_chunks; wiki mode returns 500).
case "$(printf '%s' "${DRUDGE_VECTOR:-}" | tr '[:upper:]' '[:lower:]')" in
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
  echo "   vector mode (DRUDGE_VECTOR=on) confirmed"
else
  echo "   wiki mode (DRUDGE_VECTOR=off) confirmed"
fi

MCP="$URL/mcp"
# A unique, single-line, lowercase nonce. recall lowercases the body, so the
# assertion uses one lowercased token that survives case-folding.
NONCE="e2e$(date +%s)$$"
TITLE="e2e throwaway $NONCE"
BODY="e2e end to end probe note $NONCE for a service contract round trip do not keep"

# --- 1) remember ----------------------------------------------------------------
echo "1) remember (MCP tools/call remember)…"
req=$(jq -nc --arg t "$TITLE" --arg b "$BODY" \
  '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:"remember",arguments:{title:$t,body:$b,origin:"personal",repo:"e2e-throwaway"}}}')
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
fi

# --- 5) forget ------------------------------------------------------------------
echo "5) forget (MCP tools/call forget — clean up)…"
req=$(jq -nc --arg t "$TITLE" \
  '{jsonrpc:"2.0",id:5,method:"tools/call",params:{name:"forget",arguments:{title:$t}}}')
resp=$(curl -sf -m30 "$MCP" -H 'content-type: application/json' -d "$req") \
  || fail "forget call failed (curl)"
err=$(printf '%s' "$resp" | jq -r '.error.message // empty')
[ -z "$err" ] || fail "forget returned JSON-RPC error: $err"
ack=$(printf '%s' "$resp" | jq -r '.result.content[0].text // empty')
printf '%s' "$ack" | grep -q "forgot" || fail "forget ack missing 'forgot': $ack"
echo "   → $ack"

# --- 6) recall after forget -----------------------------------------------------
echo "6) recall after forget (assert the throwaway note is gone)…"
req=$(jq -nc --arg q "$NONCE" \
  '{jsonrpc:"2.0",id:6,method:"tools/call",params:{name:"recall",arguments:{query:$q}}}')
resp=$(curl -sf -m120 "$MCP" -H 'content-type: application/json' -d "$req") \
  || fail "recall call failed (curl)"
err=$(printf '%s' "$resp" | jq -r '.error.message // empty')
[ -z "$err" ] || fail "recall returned JSON-RPC error: $err"
text=$(printf '%s' "$resp" | jq -r '.result.content[0].text // empty')
printf '%s' "$text" | grep -q "$NONCE" \
  && fail "forget failed — nonce '$NONCE' still found after recall: $text" \
  || echo "   → note no longer recalled"

# --- 7) assert the throwaway file was actually deleted from disk -----------------
echo "7) assert throwaway file removed from vault/wiki …"
if [ -f "$note_path" ]; then
  fail "forget did not remove the file: $note_path"
fi
echo "   → file removed"

if [ "$VEC" = 0 ]; then
  echo "OK: e2e passed — wiki-mode remember→recall→forget round-trips, vector-only neighbors rejected."
else
  echo "OK: e2e passed — vector-mode remember→search→recall→neighbors→forget round-trips."
fi

echo "NOTE: the throwaway note (title '$TITLE' / $note_path) was removed by the forget step."
