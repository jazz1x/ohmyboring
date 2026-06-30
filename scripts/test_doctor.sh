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
    if [ "${2:-}" = --record ]; then
        if [ -n "${DOCTOR_EVENT_CALLS:-}" ]; then
            printf '%s %s %s\n' "${3:-}" "${4:-}" "${5:-}" >>"$DOCTOR_EVENT_CALLS"
        fi
        exit 0
    fi
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

    mkdir -p "$home/.claude" "$home/.cache/boring-distill" "$boring/vault/wiki" "$boring/agents/codex" "$boring/agents/shared" "$boring/scripts"
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
    cat >"$boring/scripts/verify-llm.sh" <<'SH'
#!/bin/sh
if [ "${DOCTOR_VERIFY_LLM_FAIL:-0}" = 1 ]; then
    echo "verify-llm failed by test"
    exit 1
fi
echo "verify-llm ok"
SH
    chmod +x "$boring/scripts/verify-llm.sh"
}

run_strict() {
    case_dir="$1"
    out="$2"
    HOME="$case_dir/home" \
    BORING_HOME="$case_dir/boring" \
    BORING_URL="http://127.0.0.1:7700" \
    BORING_READINESS_NOTE_MAX_HOURS="${BORING_READINESS_NOTE_MAX_HOURS:-48}" \
    DOCTOR_EVENT_CALLS="$case_dir/events.calls" \
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
case "$(cat "$TMP/pass/events.calls")" in
  *"doctor readiness ok"*) ;;
  *)
    cat "$TMP/pass/events.calls"
    echo "FAIL: strict doctor pass event was not recorded" >&2
    exit 1
    ;;
esac

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
case "$(cat "$TMP/fail/events.calls")" in
  *"doctor readiness failed"*) ;;
  *)
    cat "$TMP/fail/events.calls"
    echo "FAIL: strict doctor failure event was not recorded" >&2
    exit 1
    ;;
esac

make_case "$TMP/provider-fail" yes
if DOCTOR_VERIFY_LLM_FAIL=1 run_strict "$TMP/provider-fail" "$TMP/provider-fail.out"; then
    cat "$TMP/provider-fail.out"
    echo "FAIL: strict doctor should fail when verify-llm fails" >&2
    exit 1
fi
case "$(cat "$TMP/provider-fail.out")" in
  *"LLM provider/model/embed contract failed"*) ;;
  *)
    cat "$TMP/provider-fail.out"
    echo "FAIL: strict doctor did not surface verify-llm failure" >&2
    exit 1
    ;;
esac

make_case "$TMP/stale-note" yes
old_note="$TMP/stale-note/boring/vault/wiki/wiki-0001.md"
old_epoch=$(( $(date +%s) - 7200 ))
python3 -c 'import os, sys; os.utime(sys.argv[1], (int(sys.argv[2]), int(sys.argv[2])))' "$old_note" "$old_epoch"
if BORING_READINESS_NOTE_MAX_HOURS=1 run_strict "$TMP/stale-note" "$TMP/stale-note.out"; then
    cat "$TMP/stale-note.out"
    echo "FAIL: strict doctor should fail when newest note is stale" >&2
    exit 1
fi
case "$(cat "$TMP/stale-note.out")" in
  *"note_freshness age_s="*"newest note is stale"*) ;;
  *)
    cat "$TMP/stale-note.out"
    echo "FAIL: strict doctor did not report note freshness failure" >&2
    exit 1
    ;;
esac

make_case "$TMP/stale-marker" yes
touch "$TMP/stale-marker/home/.cache/boring-distill/stale.pending"
old_marker_epoch=$(( $(date +%s) - 7200 ))
python3 -c 'import os, sys; os.utime(sys.argv[1], (int(sys.argv[2]), int(sys.argv[2])))' "$TMP/stale-marker/home/.cache/boring-distill/stale.pending" "$old_marker_epoch"
if BORING_READINESS_PENDING_TTL=60 run_strict "$TMP/stale-marker" "$TMP/stale-marker.out"; then
    cat "$TMP/stale-marker.out"
    echo "FAIL: strict doctor should fail when pending marker is stale" >&2
    exit 1
fi
case "$(cat "$TMP/stale-marker.out")" in
  *"marker_health writable=1 stale_pending=1"*) ;;
  *)
    cat "$TMP/stale-marker.out"
    echo "FAIL: strict doctor did not report stale marker failure" >&2
    exit 1
    ;;
esac

make_case "$TMP/invalid-ttl" yes
if BORING_READINESS_PENDING_TTL=abc run_strict "$TMP/invalid-ttl" "$TMP/invalid-ttl.out"; then
    cat "$TMP/invalid-ttl.out"
    echo "FAIL: strict doctor should fail on invalid marker TTL" >&2
    exit 1
fi
case "$(cat "$TMP/invalid-ttl.out")" in
  *"invalid pending marker TTL 'abc'"*) ;;
  *)
    cat "$TMP/invalid-ttl.out"
    echo "FAIL: strict doctor did not report invalid marker TTL" >&2
    exit 1
    ;;
esac

echo "doctor strict gate tests passed"
