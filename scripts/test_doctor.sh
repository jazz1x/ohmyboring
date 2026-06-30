#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

make_fake_path() {
    fakebin="$1"
    mkdir -p "$fakebin"

    cat >"$fakebin/curl" <<'SH'
#!/bin/sh
case " $* " in
  *" -w %{http_code} "*) printf 200 ;;
esac
exit 0
SH

    cat >"$fakebin/docker" <<'SH'
#!/bin/sh
if [ "${1:-}" = compose ] && [ "${2:-}" = version ]; then
    echo "Docker Compose version v2.27.0"
    exit 0
fi
if [ "${1:-}" = compose ] && [ "${2:-}" = ps ]; then
    echo "boring-drudge Up"
    exit 0
fi
exit 1
SH

    cat >"$fakebin/jq" <<'SH'
#!/bin/sh
case "${2:-}" in
  '.llm.provider // "ollama"') echo ollama ;;
  '.llm.base_url // "http://host.docker.internal:11434/v1"') echo "http://localhost:11434/v1" ;;
  *) exit 1 ;;
esac
SH

    cat >"$fakebin/python3" <<'SH'
#!/bin/sh
case "${1:-}" in
  */event_log.py)
    echo "resolution_quality recent_failures=0 log=/tmp/events.ndjson"
    exit 0
    ;;
esac
if [ "${2:-}" = --status ]; then
    echo "[codex-status] host_worker found=true loaded=true kind=launchd path=/tmp/fake.plist"
    exit 0
fi
exit 1
SH

    chmod +x "$fakebin/curl" "$fakebin/docker" "$fakebin/jq" "$fakebin/python3"
}

make_case() {
    case_dir="$1"
    with_note="$2"
    home="$case_dir/home"
    boring="$case_dir/boring"

    mkdir -p "$home/.claude" "$home/.cache/boring-distill" "$boring/vault/wiki" "$boring/agents/codex" "$boring/agents/shared"
    touch "$boring/agents/codex/collect-sessions.py"
    touch "$boring/agents/shared/event_log.py"
    touch "$home/.cache/boring-distill/session.ts"
    [ "$with_note" = yes ] && touch "$boring/vault/wiki/wiki-0001.md"
    printf 'DRUDGE_TOKEN=local\n' >"$boring/.env"
    chmod 600 "$boring/.env"
    cat >"$boring/boring.json" <<'JSON'
{"llm":{"provider":"ollama","base_url":"http://localhost:11434/v1"}}
JSON
    cat >"$home/.claude/settings.json" <<JSON
{"hooks":["$boring/hooks/distill-session.py","$boring/hooks/recall.py"]}
JSON
}

run_strict() {
    case_dir="$1"
    out="$2"
    HOME="$case_dir/home" \
    BORING_HOME="$case_dir/boring" \
    BORING_URL="http://127.0.0.1:7700" \
    PATH="$TMP/fakebin:$PATH" \
    sh "$ROOT/scripts/doctor.sh" --strict >"$out" 2>&1
}

make_fake_path "$TMP/fakebin"

make_case "$TMP/pass" yes
if ! run_strict "$TMP/pass" "$TMP/pass.out"; then
    cat "$TMP/pass.out"
    echo "FAIL: strict doctor should pass when every readiness proof exists" >&2
    exit 1
fi

make_case "$TMP/fail" no
if run_strict "$TMP/fail" "$TMP/fail.out"; then
    cat "$TMP/fail.out"
    echo "FAIL: strict doctor should fail without a distilled note" >&2
    exit 1
fi
case "$(cat "$TMP/fail.out")" in
  *"readiness: one or more doctor checks failed"*) ;;
  *)
    cat "$TMP/fail.out"
    echo "FAIL: strict doctor failure message missing" >&2
    exit 1
    ;;
esac

echo "doctor strict gate tests passed"
