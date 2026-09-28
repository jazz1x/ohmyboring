#!/bin/sh
# Source the repo's ./.env (exporting every name) when the file exists. launchd/cron start
# the nightly jobs with none of the interactive shell's env — no SLACK_BOT_TOKEN, no
# BORING_DOOR_URL — so both schedulers load it the same way; `make card` sources .env
# likewise. A job that only ever ran from an interactive shell was never going to fire
# on schedule.
#
# Call after cd-ing to the repo root; the path is relative on purpose.
load_dot_env() {
    if [ -f ./.env ]; then
        set -a
        # shellcheck disable=SC1091
        . ./.env
        set +a
    fi
}
