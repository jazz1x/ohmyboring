#!/bin/sh
# Guardrail tests for verify-llm.sh config-boundary handling.
# `api_key_env` is data: the name of an environment variable. It must never be
# evaluated as shell code while checking whether the key is available.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/scripts/verify-llm.sh"
PASS=0
FAIL=0

check() {
  if [ "$2" = 0 ]; then
    echo "ok - $1"
    PASS=$((PASS + 1))
  else
    echo "FAIL - $1"
    FAIL=$((FAIL + 1))
  fi
}

setup() {
  WORK="$(mktemp -d)"
  STUB_BIN="$WORK/bin"
  mkdir -p "$STUB_BIN"
  cat > "$STUB_BIN/curl" <<'STUB'
#!/bin/sh
case " $* " in
  *" -w "*) printf '401'; exit 0 ;;
  *) exit 1 ;;
esac
STUB
  chmod +x "$STUB_BIN/curl"
}

teardown() {
  rm -rf "$WORK"
}

write_config() {
  jq -n --arg api_key_env "$1" '{
    llm: {
      provider: "openai-compatible",
      base_url: "http://127.0.0.1:9/v1",
      model: "chat-model",
      embed_model: "bge-m3",
      embed_dim: 1024,
      api_key_env: $api_key_env
    }
  }' > "$WORK/boring.json"
}

run_verify() {
  RC=0
  PATH="$STUB_BIN:$PATH" BORING_HOME="$ROOT" BORING_CONFIG="$WORK/boring.json" "$SCRIPT" > "$WORK/out" 2>&1 || RC=$?
}

# --- 1. malicious env-name payload is rejected, not executed ------------------
setup
SENTINEL="$WORK/eval-ran"
write_config "(touch $SENTINEL)"
run_verify
{ [ "$RC" = 1 ] && [ ! -e "$SENTINEL" ]; }
check "api_key_env payload is not evaluated" $?
teardown

# --- 2. valid env var name + non-empty value passes auth check ----------------
setup
write_config "OMB_VERIFY_LLM_KEY"
OMB_VERIFY_LLM_KEY="secret" run_verify
{ [ "$RC" = 0 ]; }
check "valid api_key_env with value passes" $?
teardown

# --- 3. valid env var name + missing value fails ------------------------------
setup
write_config "OMB_VERIFY_LLM_MISSING_KEY"
run_verify
{ [ "$RC" = 1 ]; }
check "missing configured api key env fails" $?
teardown

echo
echo "verify-llm guardrails: $PASS passed, $FAIL failed."
[ "$FAIL" = 0 ]
