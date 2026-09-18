#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM
REAL_PY="$(command -v python3)"
export ROOT REAL_PY

make_fake_path() {
    fakebin="$1"
    mkdir -p "$fakebin"

    cat >"$fakebin/curl" <<'SH'
#!/bin/sh
# Two shapes of /health call in doctor.sh: the status probe (-w %{http_code}) and the body read
# that carries build_sha. Answering only the first is what left the drift check untestable.
case " $* " in
  *" -w %{http_code} "*) printf 200; exit 0 ;;
esac
case " $* " in
  *"/health"*)
    if [ -n "${DOCTOR_BUILD_SHA:-}" ]; then
        printf '{"status":"ok","build_sha":"%s"}' "$DOCTOR_BUILD_SHA"
    else
        printf '{"status":"ok"}'
    fi
    exit 0
    ;;
esac
# Anything this fake does not recognise is an unmodelled call, and answering it with a
# cheerful exit 0 is how a fixture stops being able to see a check at all — #217 removed
# exactly this catch-all from the fake python3, and it was re-introduced here one binary
# over. A new curl-based doctor check must fail loudly here until this fake models it.
echo "fake curl: unmodelled call: $*" >&2
exit 7
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
# `compose exec … drudge audit` is doctor's config probe: it asks the RUNNING binary to parse the
# config on disk, because a config written for a newer binary is invisible until a restart turns
# it into a crash loop.
if [ "${1:-}" = compose ] && [ "${2:-}" = exec ]; then
    if [ "${DOCTOR_CONFIG_UNREADABLE:-0}" = 1 ]; then
        echo "Error: deserialize boring.json" >&2
        echo "    unknown variant \`cobol\`, expected one of \`rust\`, \`python\`, \`shell\`" >&2
        exit 1
    fi
    exit 0
fi
echo "fake docker: unmodelled call: $*" >&2
exit 7
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
  */uptake_core.py)
    # doctor asks the ledger whether one prompt was recorded twice — a hook that fires twice
    # leaves the uptake rate unchanged and doubles a pre-registered sample floor.
    if [ "${2:-}" = --duplicate-injections ]; then
        if [ "${DOCTOR_LEDGER_DUPES:-0}" = 1 ]; then
            echo "injection_ledger duplicate_rows=42 total_rows=100 sessions=3"
            exit 1
        fi
        echo "injection_ledger duplicate_rows=0 total_rows=100 sessions=0"
        exit 0
    fi
    # doctor asks whether the uptake detector can still see a use handed to it verbatim. A zero
    # rate from a blind scorer looks exactly like a zero rate from an unused channel, and
    # docs/PRD.md §2 will not read "not working" off one.
    if [ "${2:-}" = --sensitivity-probe ]; then
        if [ "${DOCTOR_UPTAKE_BLIND:-0}" = 1 ]; then
            echo "uptake_sensitivity=blind rows=12 reason=a phrase was handed back and not counted"
            exit 1
        fi
        echo "uptake_sensitivity=ok rows=12 reason=phrase from wiki-0001.md was detected"
        exit 0
    fi
    if [ "${2:-}" = --pipeline-probe ]; then
        if [ "${DOCTOR_UPTAKE_PIPELINE_BLIND:-0}" = 1 ]; then
            echo "uptake_pipeline_probe=blind reason=a.jsonl read back with no turn markers at all"
            exit 1
        fi
        if [ "${DOCTOR_UPTAKE_PIPELINE_UNKNOWN:-0}" = 1 ]; then
            echo "uptake_pipeline_probe=unknown reason=no_ledger_session_has_a_transcript"
            exit 0
        fi
        echo "uptake_pipeline_probe=ok reason=phrase from wiki-0001.md survived a.jsonl → extract → scorer"
        exit 0
    fi
    # The other half of §2's instrument check: each transcript scored against a session's hits it
    # never received. A rate that reaches the treatment rate means the scorer is reading topic
    # overlap, so doctor has to be able to fail on it — and to tell "could not run" apart from
    # "ran clean", which is why the unknown case is modelled too.
    if [ "${2:-}" = --self-check ]; then
        if [ "${DOCTOR_UPTAKE_CONTAMINATED:-0}" = 1 ]; then
            echo "uptake_self_check=contaminated cross=40/100 rate=0.4000 treatment=45/100"
            exit 1
        fi
        if [ "${DOCTOR_UPTAKE_SELF_UNKNOWN:-0}" = 1 ]; then
            echo "uptake_self_check=unknown cross=0/0 reason=too_few_sessions_or_transcripts"
            exit 0
        fi
        echo "uptake_self_check=ok cross=0/2145 rate=0.0000 treatment=180/2145"
        exit 0
    fi
    echo "fake python3: unmodelled uptake_core call: $*" >&2
    exit 7
    ;;
  */agent_wiring.py)
    # doctor asks the installer which files it writes, rather than keeping its own list.
    # Invocation is: agent_wiring.py --list-hermes-scripts --boring-home <BORING_HOME>
    if [ "${2:-}" = --list-hermes-scripts ]; then
        for n in briefing.py slack_briefing.py; do
            echo "${4:-}/agents/hermes/$n"
        done
        exit 0
    fi
    # doctor asks the installer whether any hook of ours is registered more than once — read from
    # the agent configs, not from ledger damage, so an adapter that is wired twice but never runs
    # is still caught.
    if [ "${2:-}" = --check-registrations ]; then
        if [ "${DOCTOR_DUPLICATE_HOOKS:-0}" = 1 ]; then
            echo "hook_registered_twice agent=kimi count=2 script=/x/agents/kimi/recall.py" >&2
            echo "hook_registrations checked_configs=2 duplicates=1"
            exit 1
        fi
        echo "hook_registrations checked_configs=2 duplicates=0"
        exit 0
    fi
    echo "fake python3: unmodelled agent_wiring call: $*" >&2
    exit 7
    ;;
  */event_log.py)
    if [ "${2:-}" = --record ]; then
        if [ -n "${DOCTOR_EVENT_CALLS:-}" ]; then
            printf '%s %s %s\n' "${3:-}" "${4:-}" "${5:-}" >>"$DOCTOR_EVENT_CALLS"
        fi
        exit 0
    fi
    if [ "${2:-}" = --stale-gates ]; then
        if [ "${DOCTOR_STALE_GATES_FAIL:-0}" = 1 ]; then
            echo "stale gate: eval_graphrag_gate last_seen_days=35"
            exit 1
        fi
        exit 0
    fi
    if [ "${2:-}" = --sink-mode ]; then
        echo "${DOCTOR_EVENT_SINK_MODE:-db}"
        exit 0
    fi
    if [ "${2:-}" = --verdict-spool-loss ]; then
        exec "${REAL_PY:-/usr/bin/python3}" "${ROOT:?}/agents/shared/event_log.py" --verdict-spool-loss
    fi
    echo "resolution_quality recent_failures=0 log=/tmp/events.ndjson"
    exit 0
    ;;
esac
if [ "${2:-}" = --status ]; then
    echo "[codex-status] host_worker found=true loaded=true kind=launchd path=/tmp/fake.plist"
    exit 0
fi
# (a1b) the probe needs the real interpreter — extraction parses the fixture JSON, and the
# sentinel must record what the replayed command delivered. Models above take precedence.
if [ "${1:-}" = "-" ] || { [ -n "${1:-}" ] && [ -f "${1:-}" ]; }; then
    exec /usr/bin/python3 "$@"
fi
exit 1
SH

    chmod +x "$fakebin/curl" "$fakebin/docker" "$fakebin/jq" "$fakebin/python3"
}

window_ts() {
    python3 - "$ROOT" "$1" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/agents/shared")
from datetime import datetime, timedelta, timezone
import verdict_core
start = datetime.strptime(verdict_core.WINDOW_SINCE, "%Y-%m-%d").replace(tzinfo=verdict_core.WINDOW_TZ)
print((start + timedelta(hours=float(sys.argv[2]))).astimezone(timezone.utc).isoformat())
PY
}

window_ts_local() {
    python3 - "$ROOT" "$1" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/agents/shared")
from datetime import datetime, timedelta
import verdict_core
start = datetime.strptime(verdict_core.WINDOW_SINCE, "%Y-%m-%d").replace(tzinfo=verdict_core.WINDOW_TZ)
print((start + timedelta(hours=float(sys.argv[2]))).isoformat())
PY
}

make_case() {
    case_dir="$1"
    with_note="$2"
    home="$case_dir/home"
    boring="$case_dir/boring"

    mkdir -p "$home/.claude" "$home/.cache/boring-distill" "$boring/vault/wiki" "$boring/agents/codex" "$boring/agents/shared" "$boring/scripts"
    touch "$boring/agents/codex/collect-sessions.py"
    touch "$boring/agents/shared/event_log.py"
    touch "$boring/agents/shared/agent_wiring.py"
    # Without this file doctor skips the ledger probe entirely, and a check no fixture can reach
    # is a check that can be deleted without a single test going red.
    touch "$boring/agents/shared/uptake_core.py"

    # The scripts hermes runs are a separate artifact from the checkout: merging a briefing
    # change does not copy it to ~/.hermes/scripts. Default the fixture to the deployed state
    # so the healthy path is exercised by every existing case.
    mkdir -p "$boring/agents/hermes" "$home/.hermes/scripts"
    for n in briefing.py slack_briefing.py; do
        printf 'current %s\n' "$n" >"$boring/agents/hermes/$n"
        case "${DOCTOR_HERMES_STATE:-match}" in
            drift) printf 'shipped twelve days ago %s\n' "$n" >"$home/.hermes/scripts/$n" ;;
            missing) rm -f "$home/.hermes/scripts/$n" ;;
            *) printf 'current %s\n' "$n" >"$home/.hermes/scripts/$n" ;;
        esac
    done
    [ "${DOCTOR_HERMES_STATE:-match}" = none ] && rm -rf "$home/.hermes"
    touch "$home/.cache/boring-distill/session.ts"
    mkdir -p "$home/.cache/oh-my-boring"
    if [ "${DOCTOR_SPOOL_ROWS:-0}" -gt 0 ] 2>/dev/null; then
        spool_ts="$(window_ts 9)"
        n=0
        while [ "$n" -lt "$DOCTOR_SPOOL_ROWS" ]; do
            printf '%s\n' "{\"event\":\"injection_uptake\",\"session_id\":\"spooled-session\",\"ts\":\"$spool_ts\"}" >>"$home/.cache/oh-my-boring/events.ndjson"
            n=$((n + 1))
        done
    fi
    [ "$with_note" = yes ] && touch "$boring/vault/wiki/wiki-0001.md"
    printf 'DRUDGE_TOKEN=local\n' >"$boring/.env"
    chmod 600 "$boring/.env"
    cat >"$boring/boring.json" <<'JSON'
{"llm":{"provider":"ollama","base_url":"http://localhost:11434/v1"}}
JSON
    # Real shape, not the flat list (a1) greps: (a1b) replays the SessionEnd command, so the
    # fixture must carry one in the repaired read-stdin-into-a-file-first form.
    cat >"$home/.claude/settings.json" <<'JSON'
{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"f=$(mktemp); cat > \"$f\"; nohup sh -c 'python3 ~/oh-my-boring/hooks/distill-session.py < \"$0\"; rm -f \"$0\"' \"$f\" >/dev/null 2>&1 &"}]}],"UserPromptSubmit":[{"hooks":[{"type":"command","command":"python3 ~/oh-my-boring/hooks/recall.py"}]}]}}
JSON
    if [ -n "${DOCTOR_HOST_CLI_SHA:-}" ]; then
        mkdir -p "$boring/drudge/target/release"
        cat >"$boring/drudge/target/release/drudge" <<SH
#!/bin/sh
[ "\${1:-}" = version ] && { echo "$DOCTOR_HOST_CLI_SHA"; exit 0; }
exit 0
SH
        chmod +x "$boring/drudge/target/release/drudge"
    fi
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

# doctor compares the engine's build_sha against `git -C "$BORING_HOME" rev-parse HEAD`, so a
# fixture that is not a repo can only ever reach the "cannot read HEAD" branch. This makes the
# case a one-commit repo and prints the sha the assertions compare against.
make_case_repo() {
    case_dir="$1"
    make_case "$case_dir" yes
    git init -q "$case_dir/boring"
    git -C "$case_dir/boring" \
        -c user.email=fixture@example.invalid -c user.name=fixture -c commit.gpgsign=false \
        commit -q --allow-empty -m "fixture head"
    git -C "$case_dir/boring" rev-parse HEAD
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
# (a1b) control: the strict pass above only proves the probe did not fail readiness; assert
# the ok line itself, or a check that never prints cannot be told from one that passes.
grep -q "✓ SessionEnd hook delivers its stdin to distill-session.py" "$TMP/pass.out" || {
    cat "$TMP/pass.out"
    echo "FAIL: the healthy case must say the SessionEnd payload is delivered" >&2
    exit 1
}

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
if ( DOCTOR_VERIFY_LLM_FAIL=1 run_strict "$TMP/provider-fail" "$TMP/provider-fail.out" ); then
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
if ( BORING_READINESS_NOTE_MAX_HOURS=1 run_strict "$TMP/stale-note" "$TMP/stale-note.out" ); then
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
if ( BORING_READINESS_PENDING_TTL=60 run_strict "$TMP/stale-marker" "$TMP/stale-marker.out" ); then
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
if ( BORING_READINESS_PENDING_TTL=abc run_strict "$TMP/invalid-ttl" "$TMP/invalid-ttl.out" ); then
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

# (a2b) deploy drift. Merging is not deploying — ten merged PRs ran nowhere for two days once.
# The check only warns, so nothing but these assertions can tell it from a deleted block.
make_case "$TMP/build-sha-absent" yes
if ! run_strict "$TMP/build-sha-absent" "$TMP/build-sha-absent.out"; then
    cat "$TMP/build-sha-absent.out"
    echo "FAIL: a missing build_sha is a warning, not a strict failure" >&2
    exit 1
fi
case "$(cat "$TMP/build-sha-absent.out")" in
  *"engine reports no build_sha"*) ;;
  *)
    cat "$TMP/build-sha-absent.out"
    echo "FAIL: strict doctor did not report the missing build_sha" >&2
    exit 1
    ;;
esac

head_sha="$(make_case_repo "$TMP/build-sha-drift")"
# Drift fails readiness as of 2026-08-25. It was a warning, and the warning did not work:
# #221 sat merged-but-not-deployed and collected zero labels while readiness stayed green.
if ( DOCTOR_BUILD_SHA=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef \
     run_strict "$TMP/build-sha-drift" "$TMP/build-sha-drift.out" ); then
    cat "$TMP/build-sha-drift.out"
    echo "FAIL: deploy drift must fail strict readiness, not merely warn" >&2
    exit 1
fi
case "$(cat "$TMP/build-sha-drift.out")" in
  *"DEPLOY DRIFT — engine runs deadbeef, checkout is at "*) ;;
  *)
    cat "$TMP/build-sha-drift.out"
    echo "FAIL: strict doctor did not report deploy drift" >&2
    exit 1
    ;;
esac
case "$(cat "$TMP/build-sha-drift.out")" in
  *"runs the checked-out commit"*)
    cat "$TMP/build-sha-drift.out"
    echo "FAIL: drifted engine was reported as running the checkout" >&2
    exit 1
    ;;
esac

matched_sha="$(make_case_repo "$TMP/build-sha-match")"
if ! ( DOCTOR_BUILD_SHA="$matched_sha" \
       run_strict "$TMP/build-sha-match" "$TMP/build-sha-match.out" ); then
    cat "$TMP/build-sha-match.out"
    echo "FAIL: strict doctor should pass when the engine runs the checkout" >&2
    exit 1
fi
case "$(cat "$TMP/build-sha-match.out")" in
  *"engine runs the checked-out commit ($(printf '%.8s' "$matched_sha"))"*) ;;
  *)
    cat "$TMP/build-sha-match.out"
    echo "FAIL: strict doctor did not confirm the engine runs the checkout" >&2
    exit 1
    ;;
esac
case "$(cat "$TMP/build-sha-match.out")" in
  *"DEPLOY DRIFT"*)
    cat "$TMP/build-sha-match.out"
    echo "FAIL: matching shas were reported as drift" >&2
    exit 1
    ;;
esac

# The fixture's own fakes are checked here, because a fake that answers an unmodelled call with
# exit 0 makes every future check built on it vacuous — that is #217's defect, and it was living
# in the fake curl until 2026-08-25. Nothing else in this file exercises an unmodelled call, so
# without this assertion the guard would be unprovable.
if "$TMP/fakebin/curl" -sf http://127.0.0.1:7700/some-endpoint-the-fake-does-not-model >/dev/null 2>&1; then
    echo "FAIL: fake curl answers unmodelled calls successfully — checks built on it are vacuous" >&2
    exit 1
fi

if "$TMP/fakebin/docker" compose logs boring-drudge >/dev/null 2>&1; then
    echo "FAIL: fake docker answers unmodelled calls successfully — checks built on it are vacuous" >&2
    exit 1
fi

# (a1) hook wiring. install.sh may register the tilde form, which the shell expands at run time
# but which a grep for the expanded path never matches — doctor called working hooks missing.
# Both cases carry a real-shape SessionEnd command so (a1b)'s probe has something to replay.
make_case "$TMP/hooks-tilde" yes
cat >"$TMP/hooks-tilde/home/.claude/settings.json" <<'JSON'
{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"f=$(mktemp); cat > \"$f\"; nohup sh -c 'python3 ~/oh-my-boring/hooks/distill-session.py < \"$0\"; rm -f \"$0\"' \"$f\" >/dev/null 2>&1 &"}]}],"UserPromptSubmit":[{"hooks":[{"type":"command","command":"python3 ~/oh-my-boring/hooks/recall.py"}]}]}}
JSON
if ! run_strict "$TMP/hooks-tilde" "$TMP/hooks-tilde.out"; then
    cat "$TMP/hooks-tilde.out"
    echo "FAIL: tilde-form hook paths are wired and must not be called missing" >&2
    exit 1
fi
case "$(cat "$TMP/hooks-tilde.out")" in
  *"Claude Code hooks wired in"*) ;;
  *)
    cat "$TMP/hooks-tilde.out"
    echo "FAIL: strict doctor did not recognise tilde-form hook wiring" >&2
    exit 1
    ;;
esac

make_case "$TMP/hooks-partial" yes
cat >"$TMP/hooks-partial/home/.claude/settings.json" <<'JSON'
{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"f=$(mktemp); cat > \"$f\"; nohup sh -c 'python3 ~/oh-my-boring/hooks/distill-session.py < \"$0\"; rm -f \"$0\"' \"$f\" >/dev/null 2>&1 &"}]}]}}
JSON
if run_strict "$TMP/hooks-partial" "$TMP/hooks-partial.out"; then
    cat "$TMP/hooks-partial.out"
    echo "FAIL: strict doctor should fail when only one hook is wired" >&2
    exit 1
fi
case "$(cat "$TMP/hooks-partial.out")" in
  *"Claude Code hooks missing in"*) ;;
  *)
    cat "$TMP/hooks-partial.out"
    echo "FAIL: strict doctor did not report the half-wired hooks" >&2
    exit 1
    ;;
esac

# (a1b) The registered command can be present and still dead: a command backgrounded with `&`
# gets /dev/null as stdin, which is how the real hook ran for months — registered, firing,
# receiving nothing, with >/dev/null swallowing the error. The check must go red on that exact
# old form, stay green on the repaired form (every case above exercises it), and treat a
# missing SessionEnd entry as a failure rather than a silent pass.
( make_case "$TMP/sessionend-drops-stdin" yes
  cat >"$TMP/sessionend-drops-stdin/home/.claude/settings.json" <<'JSON'
{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"DISTILL_COMPANY_CWD=marketboro nohup python3 ~/oh-my-boring/hooks/distill-session.py >/dev/null 2>&1 &"}]}],"UserPromptSubmit":[{"hooks":[{"type":"command","command":"python3 ~/oh-my-boring/hooks/recall.py"}]}]}}
JSON
  if run_strict "$TMP/sessionend-drops-stdin" "$TMP/sessionend-drops-stdin.out"; then
      cat "$TMP/sessionend-drops-stdin.out"
      echo "FAIL: the backgrounded-stdin mutant must fail strict doctor" >&2
      exit 1
  fi
  grep -q "✗ SessionEnd hook drops its stdin" "$TMP/sessionend-drops-stdin.out" || {
      cat "$TMP/sessionend-drops-stdin.out"
      echo "FAIL: the dropped-stdin mutant must be named, not merely counted" >&2
      exit 1
  }
  echo "ok - a SessionEnd command that backgrounds stdin away fails readiness" )

( make_case "$TMP/sessionend-absent" yes
  cat >"$TMP/sessionend-absent/home/.claude/settings.json" <<'JSON'
{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"python3 ~/oh-my-boring/hooks/recall.py"}]}]}}
JSON
  if run_strict "$TMP/sessionend-absent" "$TMP/sessionend-absent.out"; then
      cat "$TMP/sessionend-absent.out"
      echo "FAIL: a settings file with no SessionEnd entry must not pass strict doctor" >&2
      exit 1
  fi
  grep -q "✗ no SessionEnd command running distill-session.py" "$TMP/sessionend-absent.out" || {
      cat "$TMP/sessionend-absent.out"
      echo "FAIL: the absent SessionEnd entry must be named, not merely counted" >&2
      exit 1
  }
  echo "ok - a settings file with no SessionEnd entry fails readiness" )

( make_case "$TMP/sessionend-unreadable" yes
  printf '{"hooks": {"SessionEnd": [unterminated\n' >"$TMP/sessionend-unreadable/home/.claude/settings.json"
  if run_strict "$TMP/sessionend-unreadable" "$TMP/sessionend-unreadable.out"; then
      cat "$TMP/sessionend-unreadable.out"
      echo "FAIL: unreadable settings JSON must not pass strict doctor" >&2
      exit 1
  fi
  grep -q "✗ Claude Code settings unreadable" "$TMP/sessionend-unreadable.out" || {
      cat "$TMP/sessionend-unreadable.out"
      echo "FAIL: unreadable settings must be named, not merely counted" >&2
      exit 1
  }
  echo "ok - unreadable settings JSON fails readiness" )

# The probe swaps the script path for a sentinel so the real distiller never runs during a doctor
# run. A command that names the script twice — a retry, a fallback — must have both swapped: one
# missed occurrence distills into the vault every time the doctor runs.
( make_case "$TMP/sessionend-named-twice" yes
  twice_boring="$TMP/sessionend-named-twice/boring"
  probe_marker="$TMP/sessionend-named-twice/doctor-probe-reached-the-real-script"
  mkdir -p "$twice_boring/hooks"
  cat >"$twice_boring/hooks/distill-session.py" <<PY
open("$probe_marker", "w").write("the doctor probe ran the real distiller")
PY
  cat >"$TMP/sessionend-named-twice/home/.claude/settings.json" <<JSON
{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"f=\$(mktemp); cat > \"\$f\"; python3 $twice_boring/hooks/distill-session.py < \"\$f\"; python3 $twice_boring/hooks/distill-session.py < \"\$f\"; rm -f \"\$f\""}]}],"UserPromptSubmit":[{"hooks":[{"type":"command","command":"python3 $twice_boring/hooks/recall.py"}]}]}}
JSON
  if ! run_strict "$TMP/sessionend-named-twice" "$TMP/sessionend-named-twice.out"; then
      cat "$TMP/sessionend-named-twice.out"
      echo "FAIL: a command naming the script twice still delivers its stdin and must pass" >&2
      exit 1
  fi
  if [ -e "$probe_marker" ]; then
      echo "FAIL: the doctor probe executed the real distill script — only the first occurrence was swapped" >&2
      exit 1
  fi
  echo "ok - a SessionEnd command naming the script twice is fully sandboxed" )

# (a2b2) The host CLI is a separate artifact from the image: `make build` does not refresh it, and
# the daily code-sync runs it. A stale one ran old logic against live data with every gate green.
matched_host="$(make_case_repo "$TMP/host-cli-ok")"
DOCTOR_HOST_CLI_SHA="$matched_host" make_case "$TMP/host-cli-ok" yes
if ! ( DOCTOR_BUILD_SHA="$matched_host" DOCTOR_HOST_CLI_SHA="$matched_host" \
       run_strict "$TMP/host-cli-ok" "$TMP/host-cli-ok.out" ); then
    cat "$TMP/host-cli-ok.out"
    echo "FAIL: a host CLI built from the checkout must pass" >&2
    exit 1
fi

drift_host="$(make_case_repo "$TMP/host-cli-drift")"
DOCTOR_HOST_CLI_SHA=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef make_case "$TMP/host-cli-drift" yes
if ( DOCTOR_BUILD_SHA="$drift_host" DOCTOR_HOST_CLI_SHA=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef \
     run_strict "$TMP/host-cli-drift" "$TMP/host-cli-drift.out" ); then
    cat "$TMP/host-cli-drift.out"
    echo "FAIL: a host CLI older than the checkout must fail readiness" >&2
    exit 1
fi
case "$(cat "$TMP/host-cli-drift.out")" in
  *"HOST CLI DRIFT"*) ;;
  *)
    cat "$TMP/host-cli-drift.out"
    echo "FAIL: strict doctor did not name the stale host CLI" >&2
    exit 1
    ;;
esac

# An unstamped host CLI (built without git, or an old binary from before the stamp existed) is a
# different statement from a stale one: there is nothing to compare, so it warns and readiness
# still passes. Treating "unknown" as "wrong" would block every wiki-first install.
unstamped_host="$(make_case_repo "$TMP/host-cli-unstamped")"
DOCTOR_HOST_CLI_SHA=unstamped make_case "$TMP/host-cli-unstamped" yes
if ! ( DOCTOR_BUILD_SHA="$unstamped_host" DOCTOR_HOST_CLI_SHA=unstamped \
       run_strict "$TMP/host-cli-unstamped" "$TMP/host-cli-unstamped.out" ); then
    cat "$TMP/host-cli-unstamped.out"
    echo "FAIL: an unstamped host CLI must warn, not fail readiness" >&2
    exit 1
fi
case "$(cat "$TMP/host-cli-unstamped.out")" in
  *"host CLI reports no build stamp"*) ;;
  *)
    cat "$TMP/host-cli-unstamped.out"
    echo "FAIL: strict doctor did not report the unstamped host CLI" >&2
    exit 1
    ;;
esac

# (a2c) A config the running binary cannot parse is invisible until something restarts the engine,
# and then it is a crash loop rather than a start. On 2026-08-25 that took the live engine down
# days after the edit that caused it. The engine is healthy in this case — that is the point.
make_case "$TMP/config-unreadable" yes
if ( DOCTOR_CONFIG_UNREADABLE=1 run_strict "$TMP/config-unreadable" "$TMP/config-unreadable.out" ); then
    cat "$TMP/config-unreadable.out"
    echo "FAIL: a config the running engine cannot parse must fail readiness before a restart" >&2
    exit 1
fi
case "$(cat "$TMP/config-unreadable.out")" in
  *"the next restart will crash-loop, not start"*) ;;
  *)
    cat "$TMP/config-unreadable.out"
    echo "FAIL: strict doctor did not name the unreadable config" >&2
    exit 1
    ;;
esac

# A gate that stopped running is the failure doctor.sh (d4b) names — silence reads as green.
make_case "$TMP/stale-gates" yes
if ( DOCTOR_STALE_GATES_FAIL=1 run_strict "$TMP/stale-gates" "$TMP/stale-gates.out" ); then
    cat "$TMP/stale-gates.out"
    echo "FAIL: strict doctor should fail when a watched gate has gone stale" >&2
    exit 1
fi
case "$(cat "$TMP/stale-gates.out")" in
  *"a watched gate has gone stale — it is not failing, it stopped running"*) ;;
  *)
    cat "$TMP/stale-gates.out"
    echo "FAIL: strict doctor did not report the stale gate" >&2
    exit 1
    ;;
esac

# Assertions below grep the severity marker (✓ / ✗), not just the wording. `bad` prints ✗ and
# `warn` prints ⚠ with the same text, so a check quietly downgraded to a warning keeps every
# text-only assertion green — a mutant that survived here four times before this was written.
# (d5b) The scripts hermes runs are a separate artifact from the checkout. On 2026-08-26 the
# installed copies were twelve days behind main, five merged PRs had never reached the 08:01
# cron, and every other doctor check was green — so "merged" has to stop reading as "delivered".
( DOCTOR_HERMES_STATE=match make_case "$TMP/hermes-match" yes
  if ! run_strict "$TMP/hermes-match" "$TMP/hermes-match.out"; then
      cat "$TMP/hermes-match.out"
      echo "FAIL: installed scripts identical to the checkout must pass" >&2
      exit 1
  fi
  grep -q "✓ hermes briefing scripts match the checkout" "$TMP/hermes-match.out" || {
      cat "$TMP/hermes-match.out"
      echo "FAIL: the healthy case must say the scripts match" >&2
      exit 1
  } ) || exit 1

( DOCTOR_HERMES_STATE=drift make_case "$TMP/hermes-drift" yes
  if run_strict "$TMP/hermes-drift" "$TMP/hermes-drift.out"; then
      cat "$TMP/hermes-drift.out"
      echo "FAIL: an installed script older than the checkout must fail strict" >&2
      exit 1
  fi
  grep -q "✗ DEPLOY DRIFT" "$TMP/hermes-drift.out" || {
      cat "$TMP/hermes-drift.out"
      echo "FAIL: drift must be named, not just counted" >&2
      exit 1
  } ) || exit 1

( DOCTOR_HERMES_STATE=missing make_case "$TMP/hermes-missing" yes
  if run_strict "$TMP/hermes-missing" "$TMP/hermes-missing.out"; then
      cat "$TMP/hermes-missing.out"
      echo "FAIL: a script hermes imports but never received must fail strict" >&2
      exit 1
  fi
  grep -q "✗ hermes never received" "$TMP/hermes-missing.out" || {
      cat "$TMP/hermes-missing.out"
      echo "FAIL: a missing script must be reported as missing" >&2
      exit 1
  } ) || exit 1

# A machine with no hermes-agent has nothing to drift from; the check must stay quiet rather
# than invent a failure for an integration the user never enabled.
( DOCTOR_HERMES_STATE=none make_case "$TMP/hermes-none" yes
  if ! run_strict "$TMP/hermes-none" "$TMP/hermes-none.out"; then
      cat "$TMP/hermes-none.out"
      echo "FAIL: an install without hermes must not fail on hermes script drift" >&2
      exit 1
  fi
  if grep -q "hermes briefing scripts" "$TMP/hermes-none.out"; then
      cat "$TMP/hermes-none.out"
      echo "FAIL: no hermes install means no hermes verdict" >&2
      exit 1
  fi ) || exit 1

# (d5c) One prompt recorded twice means the recall hook fired twice. The uptake rate cannot see
# it — both halves of the fraction double — so the ledger is the only place it shows, and it
# quietly halves the evidence behind a pre-registered sample floor (docs/PRD.md §2).
# A hook registered twice fires twice, which halves the evidence behind a pre-registered floor
# while leaving every rate looking normal — so it fails readiness rather than warning.
( make_case "$TMP/dup-hooks" yes
  if DOCTOR_DUPLICATE_HOOKS=1 run_strict "$TMP/dup-hooks" "$TMP/dup-hooks.out"; then
      cat "$TMP/dup-hooks.out"
      echo "FAIL: a hook registered twice must fail strict doctor" >&2
      exit 1
  fi
  grep -q "HOOK REGISTERED TWICE" "$TMP/dup-hooks.out" || {
      cat "$TMP/dup-hooks.out"
      echo "FAIL: the duplicate registration must be named" >&2
      exit 1
  }
  echo "ok - a hook registered twice fails readiness"
)

# A blind uptake detector has to fail readiness, not warn: the window closes on a verdict, and a
# verdict read off a scorer that sees nothing is worse than no verdict at all.
( make_case "$TMP/uptake-blind" yes
  if DOCTOR_UPTAKE_BLIND=1 run_strict "$TMP/uptake-blind" "$TMP/uptake-blind.out"; then
      cat "$TMP/uptake-blind.out"
      echo "FAIL: a blind uptake detector must fail strict doctor" >&2
      exit 1
  fi
  grep -q "UPTAKE DETECTOR IS BLIND" "$TMP/uptake-blind.out" || {
      cat "$TMP/uptake-blind.out"
      echo "FAIL: the blind detector must be named, not merely counted" >&2
      exit 1
  }
  echo "ok - a blind uptake detector fails readiness"
)

( make_case "$TMP/uptake-pipeline" yes
  if DOCTOR_UPTAKE_PIPELINE_BLIND=1 run_strict "$TMP/uptake-pipeline" "$TMP/uptake-pipeline.out"; then
      cat "$TMP/uptake-pipeline.out"
      echo "FAIL: a blind uptake pipeline must fail strict doctor" >&2
      exit 1
  fi
  grep -q "UPTAKE PIPELINE BLIND" "$TMP/uptake-pipeline.out" || {
      cat "$TMP/uptake-pipeline.out"
      echo "FAIL: the blind pipeline must be named, not merely counted" >&2
      exit 1
  }
  echo "ok - a blind uptake pipeline fails readiness"
)

( make_case "$TMP/uptake-pipeline-unknown" yes
  if DOCTOR_UPTAKE_PIPELINE_UNKNOWN=1 run_strict "$TMP/uptake-pipeline-unknown" "$TMP/uptake-pipeline-unknown.out"; then
      echo "ok - nothing to probe with is not a blind pipeline"
  else
      cat "$TMP/uptake-pipeline-unknown.out"
      echo "FAIL: an unprobeable pipeline must warn, not fail readiness" >&2
      exit 1
  fi
)

# A contaminated self-check has to fail readiness for the same reason a blind one does, from the
# other side: hits a session never received being counted as used means the treatment rate is
# measuring topic overlap, and a verdict read off that is a verdict about the corpus's vocabulary.
( make_case "$TMP/uptake-cross" yes
  if DOCTOR_UPTAKE_CONTAMINATED=1 run_strict "$TMP/uptake-cross" "$TMP/uptake-cross.out"; then
      cat "$TMP/uptake-cross.out"
      echo "FAIL: a contaminated uptake self-check must fail strict doctor" >&2
      exit 1
  fi
  grep -q "UPTAKE SELF-CHECK FAILED" "$TMP/uptake-cross.out" || {
      cat "$TMP/uptake-cross.out"
      echo "FAIL: the contaminated self-check must be named, not merely counted" >&2
      exit 1
  }
  echo "ok - a contaminated uptake self-check fails readiness"
)

# "Could not run" is not "ran clean". Too few sessions to pair is a warning, because failing on it
# would make an empty ledger indistinguishable from a broken scorer — the confusion this whole
# check exists to prevent.
( make_case "$TMP/uptake-cross-unknown" yes
  if ! DOCTOR_UPTAKE_SELF_UNKNOWN=1 run_strict "$TMP/uptake-cross-unknown" "$TMP/uptake-cross-unknown.out"; then
      cat "$TMP/uptake-cross-unknown.out"
      echo "FAIL: an unrunnable self-check must warn, not fail" >&2
      exit 1
  fi
  echo "ok - an unrunnable uptake self-check warns instead of failing"
)

( make_case "$TMP/ledger-clean" yes
  if ! run_strict "$TMP/ledger-clean" "$TMP/ledger-clean.out"; then
      cat "$TMP/ledger-clean.out"
      echo "FAIL: a ledger with no double-recorded prompts must pass" >&2
      exit 1
  fi
  grep -q "✓ injection ledger has no double-recorded prompts" "$TMP/ledger-clean.out" || {
      cat "$TMP/ledger-clean.out"
      echo "FAIL: the healthy case must say the ledger is clean" >&2
      exit 1
  } ) || exit 1

( make_case "$TMP/ledger-dupes" yes
  if DOCTOR_LEDGER_DUPES=1 run_strict "$TMP/ledger-dupes" "$TMP/ledger-dupes.out"; then
      cat "$TMP/ledger-dupes.out"
      echo "FAIL: double-recorded injections must fail strict" >&2
      exit 1
  fi
  # Grep the failure marker, not just the words. `bad` prints ✗ and `warn` prints ⚠, and a
  # downgrade to warn while the failure flag stays set reads as green in non-strict mode while
  # every strict assertion still passes — the mutant that survives when only the text is checked.
  grep -q "✗ DOUBLE-RECORDED INJECTIONS" "$TMP/ledger-dupes.out" || {
      cat "$TMP/ledger-dupes.out"
      echo "FAIL: the duplicate must be reported as a failure, not a warning" >&2
      exit 1
  } ) || exit 1

( make_case "$TMP/spool-clean" yes
  if ! run_strict "$TMP/spool-clean" "$TMP/spool-clean.out"; then
      cat "$TMP/spool-clean.out"
      echo "FAIL: an empty spool must pass strict doctor" >&2
      exit 1
  fi
  grep -q "✓ event spool is empty" "$TMP/spool-clean.out" || {
      cat "$TMP/spool-clean.out"
      echo "FAIL: the healthy case must say the spool is empty" >&2
      exit 1
  }
  grep -q "✓ event spool dir is writable" "$TMP/spool-clean.out" || {
      cat "$TMP/spool-clean.out"
      echo "FAIL: the healthy case must say the spool dir is writable" >&2
      exit 1
  } ) || exit 1

( DOCTOR_SPOOL_ROWS=3 make_case "$TMP/spool-trapped" yes
  if run_strict "$TMP/spool-trapped" "$TMP/spool-trapped.out"; then
      cat "$TMP/spool-trapped.out"
      echo "FAIL: rows trapped in the spool must fail strict doctor" >&2
      exit 1
  fi
  grep -q "✗ EVENTS TRAPPED IN THE SPOOL" "$TMP/spool-trapped.out" || {
      cat "$TMP/spool-trapped.out"
      echo "FAIL: the trapped rows must be named, not merely counted" >&2
      exit 1
  }
  echo "ok - events trapped in the spool fail readiness" )

( make_case "$TMP/in_window_spool_rows_fail_readiness" yes
  printf '{"event":"injection_uptake","session_id":"spooled-in-window","ts":"%s"}\n' "$(window_ts 9)" \
      >>"$TMP/in_window_spool_rows_fail_readiness/home/.cache/oh-my-boring/events.ndjson"
  if run_strict "$TMP/in_window_spool_rows_fail_readiness" "$TMP/in_window_spool_rows_fail_readiness.out"; then
      cat "$TMP/in_window_spool_rows_fail_readiness.out"
      echo "FAIL: a verdict-kind row dated inside the window must fail strict doctor" >&2
      exit 1
  fi
  grep -q "✗ EVENTS TRAPPED IN THE SPOOL" "$TMP/in_window_spool_rows_fail_readiness.out" || {
      cat "$TMP/in_window_spool_rows_fail_readiness.out"
      echo "FAIL: the in-window loss must be named as a failure, not a warning" >&2
      exit 1
  }
  grep -q "1 verdict-kind row(s) from 1 session(s)" "$TMP/in_window_spool_rows_fail_readiness.out" || {
      cat "$TMP/in_window_spool_rows_fail_readiness.out"
      echo "FAIL: the failure must carry the in-window row and session counts" >&2
      exit 1
  }
  echo "ok - in_window_spool_rows_fail_readiness" ) || exit 1

( make_case "$TMP/out_of_window_spool_rows_warn_instead_of_failing" yes
  printf '{"event":"session_end","session_id":"spooled-historical","ts":"%s"}\n' "$(window_ts -12)" \
      >>"$TMP/out_of_window_spool_rows_warn_instead_of_failing/home/.cache/oh-my-boring/events.ndjson"
  if ! run_strict "$TMP/out_of_window_spool_rows_warn_instead_of_failing" "$TMP/out_of_window_spool_rows_warn_instead_of_failing.out"; then
      cat "$TMP/out_of_window_spool_rows_warn_instead_of_failing.out"
      echo "FAIL: history the open verdict cannot read must not fail strict readiness" >&2
      exit 1
  fi
  grep -q "! verdict-kind rows trapped in the spool before the window" "$TMP/out_of_window_spool_rows_warn_instead_of_failing.out" || {
      cat "$TMP/out_of_window_spool_rows_warn_instead_of_failing.out"
      echo "FAIL: the historical rows must still be said out loud, as a warning" >&2
      exit 1
  }
  grep -q "1 row(s) older than" "$TMP/out_of_window_spool_rows_warn_instead_of_failing.out" || {
      cat "$TMP/out_of_window_spool_rows_warn_instead_of_failing.out"
      echo "FAIL: the warning must carry the out-of-window count" >&2
      exit 1
  }
  grep -q "oldest $(window_ts -12 | cut -c1-10)" "$TMP/out_of_window_spool_rows_warn_instead_of_failing.out" || {
      cat "$TMP/out_of_window_spool_rows_warn_instead_of_failing.out"
      echo "FAIL: the warning must carry the oldest date, not merely the count" >&2
      exit 1
  }
  echo "ok - out_of_window_spool_rows_warn_instead_of_failing" ) || exit 1

( make_case "$TMP/non_verdict_spool_rows_do_not_raise_a_verdict_alarm" yes
  printf '{"event":"doctor","status":"ok","ts":"%s"}\n' "$(window_ts 9)" \
      >>"$TMP/non_verdict_spool_rows_do_not_raise_a_verdict_alarm/home/.cache/oh-my-boring/events.ndjson"
  if ! run_strict "$TMP/non_verdict_spool_rows_do_not_raise_a_verdict_alarm" "$TMP/non_verdict_spool_rows_do_not_raise_a_verdict_alarm.out"; then
      cat "$TMP/non_verdict_spool_rows_do_not_raise_a_verdict_alarm.out"
      echo "FAIL: spool volume of kinds the verdict never reads must not fail strict readiness" >&2
      exit 1
  fi
  grep -q "✓ event spool holds 1 row(s), none of the kinds" "$TMP/non_verdict_spool_rows_do_not_raise_a_verdict_alarm.out" || {
      cat "$TMP/non_verdict_spool_rows_do_not_raise_a_verdict_alarm.out"
      echo "FAIL: non-verdict rows must be reported as volume the verdict cannot miss" >&2
      exit 1
  }
  if grep -q "verdict-kind rows trapped" "$TMP/non_verdict_spool_rows_do_not_raise_a_verdict_alarm.out"; then
      cat "$TMP/non_verdict_spool_rows_do_not_raise_a_verdict_alarm.out"
      echo "FAIL: a doctor row must not raise a verdict-shaped alarm" >&2
      exit 1
  fi
  echo "ok - non_verdict_spool_rows_do_not_raise_a_verdict_alarm" ) || exit 1

( make_case "$TMP/a_window_boundary_row_is_placed_by_instant_not_by_string" yes
  printf '{"event":"session_end","session_id":"spooled-boundary","ts":"%s"}\n' "$(window_ts_local -1)" \
      >>"$TMP/a_window_boundary_row_is_placed_by_instant_not_by_string/home/.cache/oh-my-boring/events.ndjson"
  if ! run_strict "$TMP/a_window_boundary_row_is_placed_by_instant_not_by_string" "$TMP/a_window_boundary_row_is_placed_by_instant_not_by_string.out"; then
      cat "$TMP/a_window_boundary_row_is_placed_by_instant_not_by_string.out"
      echo "FAIL: a +09:00 row one hour before the window opens on the owner's calendar is outside it; only a string compare files it inside" >&2
      exit 1
  fi
  grep -q "! verdict-kind rows trapped in the spool before the window" "$TMP/a_window_boundary_row_is_placed_by_instant_not_by_string.out" || {
      cat "$TMP/a_window_boundary_row_is_placed_by_instant_not_by_string.out"
      echo "FAIL: the boundary row must warn as history the open verdict cannot read" >&2
      exit 1
  }
  if grep -q "✗ EVENTS TRAPPED IN THE SPOOL" "$TMP/a_window_boundary_row_is_placed_by_instant_not_by_string.out"; then
      cat "$TMP/a_window_boundary_row_is_placed_by_instant_not_by_string.out"
      echo "FAIL: the boundary row must not be counted as in-window loss" >&2
      exit 1
  fi
  echo "ok - a_window_boundary_row_is_placed_by_instant_not_by_string" ) || exit 1

( make_case "$TMP/spool-unwritable" yes
  rm -rf "$TMP/spool-unwritable/home/.cache/oh-my-boring"
  touch "$TMP/spool-unwritable/home/.cache/oh-my-boring"
  if run_strict "$TMP/spool-unwritable" "$TMP/spool-unwritable.out"; then
      cat "$TMP/spool-unwritable.out"
      echo "FAIL: an unusable spool dir must fail strict doctor" >&2
      exit 1
  fi
  grep -q "✗ event spool dir cannot be created" "$TMP/spool-unwritable.out" || {
      cat "$TMP/spool-unwritable.out"
      echo "FAIL: the unusable spool dir must be named, not merely counted" >&2
      exit 1
  }
  echo "ok - an unusable spool dir fails readiness" )

( make_case "$TMP/spool-mode-override" yes
  if ! DOCTOR_EVENT_SINK_MODE=spool run_strict "$TMP/spool-mode-override" "$TMP/spool-mode-override.out"; then
      cat "$TMP/spool-mode-override.out"
      echo "FAIL: a deliberate spool-mode override must warn, not fail readiness" >&2
      exit 1
  fi
  grep -q "! event sink is 'spool'" "$TMP/spool-mode-override.out" || {
      cat "$TMP/spool-mode-override.out"
      echo "FAIL: the spool-mode override must be named as a warning" >&2
      exit 1
  }
  echo "ok - a spool-mode override warns instead of failing" )

( DOCTOR_EVENT_SINK_MODE=both DOCTOR_SPOOL_ROWS=2 make_case "$TMP/spool-mirror" yes
  if ! DOCTOR_EVENT_SINK_MODE=both run_strict "$TMP/spool-mirror" "$TMP/spool-mirror.out"; then
      cat "$TMP/spool-mirror.out"
      echo "FAIL: mirror rows under sink=both must pass strict doctor" >&2
      exit 1
  fi
  grep -q "✓ event sink mirrors to the spool" "$TMP/spool-mirror.out" || {
      cat "$TMP/spool-mirror.out"
      echo "FAIL: sink=both mirror rows must be reported as healthy" >&2
      exit 1
  } ) || exit 1

echo "doctor strict gate tests passed"
