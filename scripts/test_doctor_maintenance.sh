#!/bin/sh
# Guardrails for the two maintenance lines doctor grew: (a2c) a compact that never finishes,
# and (a2d) the repo's own pre-commit gate left unwired.
#
# (a2c) exists because a failing compact was only ever an `eprintln!` in the docker log —
# /health did not carry it, doctor did not ask, and the scheduler stamped its window as though
# the run had worked (#304). Weeks of maintenance doing nothing, every gate green.
#
# The interesting case is not "it reports a failure". It is "it stays quiet when there is none":
# a line that fires on a healthy engine is an alarm nobody reads within a week, which is how the
# warning this replaced died. Both directions are pinned here.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STUB="$ROOT/scripts/lib/health_stub.py"
fails=0
SRV=""

serve_once() {
    python3 "$STUB" "$1" "$2" &
    SRV=$!
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        curl -sf -m1 "http://127.0.0.1:$2/health" >/dev/null 2>&1 && return 0
        sleep 0.3
    done
    echo "FAIL: stub server did not come up on $2"
    fails=$((fails + 1))
    return 1
}

stop_server() {
    [ -n "${SRV:-}" ] && kill "$SRV" 2>/dev/null
    SRV=""
    sleep 0.2
}

# The (a2c) parse, exercised through doctor itself rather than a copy of its sed: a test that
# reimplements the expression it is checking passes when the expression is wrong.
check_compact() {
    label="$1"; want="$2"; url="$3"
    out="$(BORING_URL="$url" BORING_SKIP_LEDGER_PROBE=1 sh "$ROOT/scripts/doctor.sh" 2>&1 || true)"
    got="$(printf '%s' "$out" | grep -c 'COMPACT FAILING' || true)"
    if [ "$got" = "$want" ]; then
        echo "ok - $label"
    else
        echo "FAIL: $label — expected COMPACT FAILING x$want, saw x$got"
        printf '%s\n' "$out" | grep -i 'compact' || true
        fails=$((fails + 1))
    fi
}

healthy='{"status":"ok","vector":true,"sync":"idle","corpus_count":5,"db_healthy":true}'
failing='{"status":"ok","vector":true,"sync":"idle","corpus_count":5,"db_healthy":true,"compact_failure":{"at":"2026-09-09T12:00:00Z","consecutive":37,"error":"reindex table claim: could not resize shared memory segment"}}'

if serve_once "$healthy" 7861; then
    check_compact "a healthy engine says nothing about compact" 0 "http://127.0.0.1:7861"
fi
stop_server

if serve_once "$failing" 7862; then
    check_compact "a failing compact is reported" 1 "http://127.0.0.1:7862"
fi
stop_server

# (a2d) The hook check reads the working tree, not the network, so it is exercised against a
# throwaway directory instead of the real .git — a test that moves the developer's own hook aside
# fails destructively the moment it is interrupted.
hook_probe() {
    label="$1"; want="$2"; home="$3"
    out="$(BORING_HOME="$home" BORING_URL="http://127.0.0.1:1" BORING_SKIP_LEDGER_PROBE=1 \
        sh "$ROOT/scripts/doctor.sh" 2>&1 || true)"
    got="$(printf '%s' "$out" | grep -c 'PRE-COMMIT HOOK MISSING' || true)"
    if [ "$got" = "$want" ]; then
        echo "ok - $label"
    else
        echo "FAIL: $label — expected PRE-COMMIT HOOK MISSING x$want, saw x$got"
        fails=$((fails + 1))
    fi
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/.git/hooks"
touch "$tmp/.pre-commit-config.yaml"
hook_probe "a config with no installed hook fails" 1 "$tmp"
printf '#!/bin/sh\nexit 0\n' > "$tmp/.git/hooks/pre-commit"
chmod +x "$tmp/.git/hooks/pre-commit"
hook_probe "an installed hook passes" 0 "$tmp"

# A repo without the config is not misconfigured, it simply does not use pre-commit. The check
# must be silent there rather than demanding a hook for a config that does not exist.
rm "$tmp/.pre-commit-config.yaml"
rm "$tmp/.git/hooks/pre-commit"
hook_probe "no config means no demand for a hook" 0 "$tmp"

if [ "$fails" -eq 0 ]; then
    echo "doctor maintenance guardrails: 5 passed, 0 failed."
    exit 0
fi
echo "doctor maintenance guardrails: $fails failed."
exit 1
