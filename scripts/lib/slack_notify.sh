#!/bin/sh
# Shared Slack failure-notice path for the unattended schedulers (schedule-card.sh,
# schedule-maintenance.sh). One implementation, sourced by both — copied notices drift.
#
# Slack answers HTTP 200 even when the post fails; only a body with "ok":true is a send.
# A notice never fails the caller: a missing token or a failed post is one stderr line,
# because the notice exists to explain a failure, not to add a second one.

# Send one finished line to the owner's DM (SLACK_CARD_CHANNEL — the channel the card
# already posts to). Callers build the text; this owns nothing but the send.
notify_slack() {
    text="$1"
    if [ -z "${SLACK_BOT_TOKEN:-}" ] || [ -z "${SLACK_CARD_CHANNEL:-}" ]; then
        echo "✗ 실패 알림도 못 보냈다: SLACK_BOT_TOKEN·SLACK_CARD_CHANNEL 이 없음" >&2
        return 0
    fi
    body=$(curl -sS https://slack.com/api/chat.postMessage \
        -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
        --data-urlencode "channel=${SLACK_CARD_CHANNEL}" \
        --data-urlencode "text=${text}" 2>&1) || {
        echo "✗ 실패 알림도 못 보냈다: curl: ${body}" >&2
        return 0
    }
    case "$body" in
        *'"ok":true'*) ;;
        *) echo "✗ 실패 알림도 못 보냈다: ${body}" >&2 ;;
    esac
}

# The schedulers' failure-notice shape: one line naming the job's NOTICE, the exit code,
# and the reason. Callers set NOTICE before calling.
notify_failure() {
    notify_slack "${NOTICE} — exit $1 · $2"
}
