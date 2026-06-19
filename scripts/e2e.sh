#!/bin/sh
# e2e.sh — wiki-mode (DRUDGE_VECTOR=off) end-to-end against a RUNNING stack.
#   make e2e   or   ./scripts/e2e.sh
#
# Exercises the live MCP surface (drudge /mcp, JSON-RPC 2.0) the way an agent does:
#   1. remember  — write a throwaway, clearly-namespaced note via the `remember` tool
#   2. recall    — read it back via the `recall` tool; assert the body round-trips
#   3. neighbors — assert a vector-only tool errors with code -32603 when vector is off
#
# This needs a live stack on :7700 (make up) running in wiki mode. It does NOT start,
# stop, or build anything. If the stack is down it SKIPS (exit 0) like smoke.sh, never
# failing CI on a missing engine. If the stack is in VECTOR mode it SKIPS too: the
# vector-off rejection (step 3) is the contract under test and only holds with vector off.
#
# Request shapes are grounded in drudge/src/serve.rs + scripts/smoke.sh — see the
# `# grounded:` comment on each call. /bin/sh (dash) has no pipefail → each step's
# result is checked explicitly.
set -eu
URL="${DRUDGE_URL:-http://localhost:7700}"

fail() { echo "FAIL: $1"; exit 1; }
skip() { echo "SKIP: $1"; exit 0; }

command -v curl >/dev/null 2>&1 || fail "curl not found"
command -v jq   >/dev/null 2>&1 || skip "jq not found — install: brew install jq / apt-get install jq"

# --- stack-up check (friendly skip when down, like smoke.sh step 2) -----------------
# grounded: serve.rs:1004  .route("/health", get(health))  → 200 when the engine is up.
echo "0) stack up? (drudge /health)…"
if [ "$(curl -s -o /dev/null -w '%{http_code}' -m5 "$URL/health" 2>/dev/null)" != "200" ]; then
  skip "drudge not up at $URL (run: make up) — e2e needs a live stack"
fi

# --- mode check: this e2e asserts the wiki-mode (DRUDGE_VECTOR=off) contract ---------
# Resolve mode the same way smoke.sh does: honor an explicit DRUDGE_VECTOR, else ask the
# live engine — /audit returns total_chunks only on the vector backend (HTTP 500 in wiki
# mode, which curl -f turns into a non-zero exit).
# grounded: serve.rs:1009 .route("/audit", get(handle_audit)); smoke.sh:14-27 (same probe).
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
[ "$VEC" = 0 ] || skip "stack is in VECTOR mode — this e2e asserts the DRUDGE_VECTOR=off contract"
echo "   wiki mode (DRUDGE_VECTOR=off) confirmed"

MCP="$URL/mcp"
# A unique, single-line, lowercase nonce. recall returns a ~200-char snippet with newlines
# collapsed to spaces and the body lowercased (wiki_recall.rs:91-104 snippet_around +
# score_doc lowercases), so the assertion uses one lowercased token that survives
# truncation and case-folding. The "e2e" namespace marks the note as a throwaway.
NONCE="e2e$(date +%s)$$"
TITLE="e2e throwaway $NONCE"
BODY="e2e end to end probe note $NONCE for a wiki mode round trip do not keep"

# --- 1) remember --------------------------------------------------------------------
# grounded: serve.rs:277 "tools/call" => mcp_call; serve.rs:464-474 routes name "remember"
#           → mcp_remember; serve.rs:466-470 reads params.name + params.arguments;
#           serve.rs:730-740 remember requires arguments.title + arguments.body.
#           Same JSON-RPC shape as Makefile `remember` (line 55-58) + smoke.sh-style curl.
echo "1) remember (MCP tools/call remember)…"
req=$(jq -nc --arg t "$TITLE" --arg b "$BODY" \
  '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:"remember",arguments:{title:$t,body:$b,origin:"personal"}}}')
resp=$(curl -sf -m120 "$MCP" -H 'content-type: application/json' -d "$req") \
  || fail "remember call failed (curl)"
# grounded: serve.rs:283 errors → {error:{code,message}}; serve.rs:281 ok → {result:...}.
err=$(printf '%s' "$resp" | jq -r '.error.message // empty')
[ -z "$err" ] || fail "remember returned JSON-RPC error: $err"
# grounded: ToolOut::Text → result.content[0].text (serve.rs:447-449); mcp_remember success
#           text contains "remembered" (serve.rs:680/693).
ack=$(printf '%s' "$resp" | jq -r '.result.content[0].text // empty')
printf '%s' "$ack" | grep -q "remembered" || fail "remember ack missing 'remembered': $ack"
echo "   → $ack"

# --- 2) recall (round-trip) ---------------------------------------------------------
# grounded: serve.rs:473 routes name "recall" → mcp_recall; serve.rs:499-505 reads
#           arguments.query (required). Wiki mode → wiki_recall::recall (serve.rs:530-538);
#           result text is "- [src] body" lines (serve.rs:543-550), ToolOut::Text.
echo "2) recall (MCP tools/call recall — assert round-trip)…"
req=$(jq -nc --arg q "$NONCE" \
  '{jsonrpc:"2.0",id:2,method:"tools/call",params:{name:"recall",arguments:{query:$q}}}')
resp=$(curl -sf -m120 "$MCP" -H 'content-type: application/json' -d "$req") \
  || fail "recall call failed (curl)"
err=$(printf '%s' "$resp" | jq -r '.error.message // empty')
[ -z "$err" ] || fail "recall returned JSON-RPC error: $err"
text=$(printf '%s' "$resp" | jq -r '.result.content[0].text // empty')
# The nonce is lowercase already; recall lowercases the body, so a plain substring match holds.
printf '%s' "$text" | grep -q "$NONCE" \
  || fail "round-trip failed — nonce '$NONCE' not found in recall result: $text"
echo "   → round-trip OK (nonce recalled)"

# --- 3) neighbors rejected when vector is off ---------------------------------------
# grounded: serve.rs:480 routes name "neighbors" → mcp_neighbors; serve.rs:564
#           s.store.ok_or_else(vec_off_rpc) when vector is off; vec_off_rpc returns
#           (-32603, …) (serve.rs:188-190); handle_mcp wraps it as
#           {error:{code:-32603,message:…}} (serve.rs:283).
echo "3) neighbors (MCP tools/call neighbors — assert vector-off error -32603)…"
req=$(jq -nc --arg q "$NONCE" \
  '{jsonrpc:"2.0",id:3,method:"tools/call",params:{name:"neighbors",arguments:{query:$q}}}')
resp=$(curl -sf -m30 "$MCP" -H 'content-type: application/json' -d "$req") \
  || fail "neighbors call failed (curl)"
code=$(printf '%s' "$resp" | jq -r '.error.code // empty')
[ "$code" = "-32603" ] \
  || fail "expected JSON-RPC error code -32603 for neighbors in wiki mode, got: $resp"
echo "   → neighbors correctly rejected with -32603"

echo "OK: e2e passed — wiki-mode remember→recall round-trips, vector-only neighbors rejected."
echo "NOTE: a throwaway note (title '$TITLE') was written to the live vault/wiki — namespaced 'e2e' for easy cleanup."
