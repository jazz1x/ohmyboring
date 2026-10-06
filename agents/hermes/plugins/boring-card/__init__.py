"""The morning card's button handler — hermes owns the only Slack socket (wiki-2049), so
every card button press lands here. The handler acks first, parses the press with the same
card_press.parse_press card.py trusts, refuses a row that is no longer pressable (already
judged on the card, or a racing second press on the same row), and folds the shared decision
table (card_press.effects) through the same interpreter and the same live functions card.py's
record_verdict uses (card_effects.run over card_effects._live_*). One table, one interpreter,
wherever a press lands — and one stop rule: the fold halts at the first failed effect, so a
dead engine never leaves a verdict record for a consumption it never received. Before any
effect the pressed row turns into 「진행 중」 (card_view.mark_progress); a failed effect turns it
into 「실패 — reason」; a merge the door accepted stays in progress until the door, which owns
the reread, settles the row itself. Effects done, the card itself is edited in place:
card_view.mark_pressed re-renders just the pressed row on
the blocks the Slack payload carries, and chat_update swaps them in for the row card.py would
rebuild from its in-memory graph. The display language is the same resolution card.py builds
its graph with (card_advice.resolve_lang(boring_config.note_lang())).

A review-lane 「맡길게요」 press is the one press that judges: the owner hands the call back,
so before the decision table folds, the handler reads the note body and the proposal's
근거 사건 and asks the model once (the host's ctx.llm facade) whether the session's claim
holds — judged kind + 이유 한 줄 ride the agent-lineage edge (judge agent:delegated, never
owner), and the same 이유 shows on the next morning's row (card_live reads the 사건). The
model call lives here and exactly here, once per press — the decision table and
card_delegate stay pure, so 한 누름의 모델 호출 상한 1 is a structure, not a hope. A model
that cannot answer (unreadable note, dead call, malformed JSON) writes no 판정 at all:
the press leaves the 사건 one line that says so, and the row shows the failure — 조용히
낱말 표지로 되돌아가지 않는다.

A repair or review row's buttons carry no value: the handler reads the row's card_row 사건 by
(card_ts, idx) and parses the press with it, and a row it cannot read shows 「✕ 실패 — 줄 정보를
못 읽었어요」 over its buttons without taking the press (a button that still holds its value, from
a card posted before card_row, is pressed as before). 「💬 코멘트」 opens a modal on the click's
trigger_id (views_open); the submission arrives through the Slack app's view handler, wired by
the platform-handler factory register() hands ctx.register_platform_handler, and becomes one
card_comment 사건 (judge owner) plus 「💬 받았어요」 over the row — the buttons stay.

A lane's [더보기] (`card:more:<lane>`, no value) sends the rows the card left out as one new
message in the same DM — no thread, no model: the rows come back from card_row (shown False,
the whole row in `detail`), card_view.fit_page draws them with the card's own row functions,
and the page's rows are recorded under its new ts (card_row for repair/review rows, and — as
the rows are seen only now — card_proposal + handover for advice, verdict_sample_shown for
표본) so every button, comment and press on it runs the first card's path. The pressed line
becomes 「↓ 이어서 보냈어요 (N건)」; a lane's card_more 사건 (card_ts, lane, new ts) answers a
second press with the same line and sends nothing. A failure before the page is out shows
「✕ 실패」 over the button, which stays.

register() refuses loudly instead of half-registering: without SECRETARY_OWNER_ID no
handler is installed at all — a press from anyone must never reach an effect — and without
BORING_HOME the repo's own modules cannot even be found. All module-level imports stay
stdlib so a broken repo aborts register(), not the plugin's import. The AsyncWebClient
chat_update needs is the plugin's own: hermes wraps plugin action handlers in
(ack, body, action) with no client injected, so register() reads the bot token the gateway
loaded from HERMES_HOME/.env into the environment (gateway/run.py:1611 at v2026.9.24) and
builds one. Without ctx.llm (older hermes) the handler still registers — every other lane
keeps working — but a delegate press leaves only the 사건 line naming the missing model seat.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import sys
import time
from datetime import UTC, datetime
from typing import Any

_LOG = logging.getLogger(__name__)

#: Every card button's action_id is `card:<idx>:<choice>` — one handler for the whole card.
_ACTION_ID = re.compile(r"^card:")

_PLUGIN_NAME = "boring-card"

#: Presses already taken, as (card_ts, idx). A row runs its effects at most once per card:
#: the claim lands before the effects run — two presses racing ahead of the first
#: chat_update must not both run effects — and is released only when an effect fails, so
#: the owner can press again. Once the effects ran the claim stays, even if chat_update
#: fails and the buttons are still showing: a re-press would run them twice.
_claimed: set[tuple[str, int]] = set()

#: [더보기] presses already taken, as (card_ts, lane): a lane is continued at most once. Claimed
#: before the page goes out, released only when nothing went out; after that the lane's
#: card_more 사건 answers a repeat.
_more_claimed: set[tuple[str, str]] = set()

#: The chat_update client built by register(); None when slack_sdk or the bot token is
#: absent (one error line at register) — presses still take their effects, the card just
#: keeps its buttons.
_client: Any | None = None

#: The delegate press's model call ceiling, seconds — a local model answers in tens of
#: seconds; a press that outwaits this is a failed judgment (the 사건 line), not a
#: hung row.
DELEGATE_MODEL_TIMEOUT = float(os.environ.get("CARD_DELEGATE_TIMEOUT") or "120")


def _repo_module_dirs() -> tuple[str, str, str] | None:
    """The repo's agents/slack + agents/shared + src dirs via BORING_HOME — no hardcoded path."""
    home = os.environ.get("BORING_HOME")
    if not home:
        return None
    slack = os.path.join(home, "agents", "slack")
    shared = os.path.join(home, "agents", "shared")
    src = os.path.join(home, "src")
    if not (os.path.isdir(slack) and os.path.isdir(shared) and os.path.isdir(src)):
        return None
    return slack, shared, src


def _make_client() -> Any | None:
    """The AsyncWebClient chat_update needs. hermes wraps plugin action handlers in
    (ack, body, action) with no client injected, so the plugin reads the same bot token the
    Slack adapter sends with and builds its own. None when slack_sdk or the token is absent."""
    try:
        from slack_sdk.web.async_client import AsyncWebClient
    except ImportError:
        return None
    raw = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not raw:
        return None
    return AsyncWebClient(token=raw.split(",")[0].strip())


def register(ctx: Any) -> None:
    owner_id = os.environ.get("SECRETARY_OWNER_ID")
    if not owner_id:
        _LOG.error(
            "%s: SECRETARY_OWNER_ID is not set — refusing to register the card button "
            "handler; without an owner check no press is trustworthy, so none is accepted",
            _PLUGIN_NAME,
        )
        return
    dirs = _repo_module_dirs()
    if dirs is None:
        _LOG.error(
            "%s: BORING_HOME does not point at a checkout (agents/slack + agents/shared "
            "+ src not found) — the press parser and effects cannot be imported; no handler "
            "registered",
            _PLUGIN_NAME,
        )
        return
    for path in dirs:
        if path not in sys.path:
            sys.path.insert(0, path)
    import card_advice
    import card_delegate
    import card_effects
    import card_press
    import card_types
    import card_view

    from ohmyboring import config as boring_config
    from ohmyboring.i18n import card as card_i18n

    #: card_row is read over the answerable window (plus the hour it may have posted in).
    ROW_WINDOW_HOURS = math.ceil(card_press.CARD_ANSWERABLE_HOURS) + 1

    global _client
    _client = _make_client()
    if _client is None:
        _LOG.error(
            "%s: slack_sdk or SLACK_BOT_TOKEN missing — presses will apply effects but the "
            "card will not be edited in place",
            _PLUGIN_NAME,
        )

    llm = getattr(ctx, "llm", None)
    if llm is None:
        _LOG.error(
            "%s: ctx.llm is not available — 맡길게요 presses will leave no 판정, only the "
            "사건 line naming the missing model seat",
            _PLUGIN_NAME,
        )

    def _log_unrendered(press, reason) -> None:
        _LOG.error(
            "%s: press card_ts=%s idx=%s could not re-render the card — %s",
            _PLUGIN_NAME,
            press.card_ts,
            press.idx,
            reason,
        )

    async def _judge(press) -> card_types.Delegated:
        """One delegate press's judgment — the model call happens exactly here, exactly once.
        Everything that keeps the model from answering (no LLM seat, unreadable note, missing
        사건, dead call, malformed JSON) comes home as DelegationFailed: no 판정 edge, and the
        decision table writes the 사건 one line that says so."""
        if llm is None:
            return card_types.DelegationFailed(reason="모델 호출 자리가 없다 (ctx.llm 없음)")
        path = card_view.note_path(press.note)
        text = card_delegate.read_note_text(path)
        if text is None:
            return card_types.DelegationFailed(reason=f"노트 {press.note} 본문을 못 읽었다")
        sessions = list(press.sessions) or [press.session]
        evidence = card_delegate.proposal_evidence(sessions, path, press.kind)
        if evidence is None:
            return card_types.DelegationFailed(reason=f"제안 사건(근거 문장)을 못 찾았다: {press.note}")
        try:
            comments = (await asyncio.to_thread(card_delegate.comments_for_note, path))[
                : card_types.COMMENTS_IN_PROMPT
            ]
        except (
            OSError,
            ValueError,
        ) as e:  # an unread comment is a failed judgment, never a judgment as if the owner were silent
            return card_types.DelegationFailed(reason=f"오너 코멘트를 못 읽었다: {' '.join(str(e).split())}")
        prompt = card_delegate.build_judge_prompt(path, text, press.kind, evidence, comments)
        try:
            result = await llm.acomplete(
                [{"role": "user", "content": prompt}],
                timeout=DELEGATE_MODEL_TIMEOUT,
                purpose="boring-card delegate judgment",
            )
        except Exception as e:  # noqa: BLE001 — a dead model is a value: the 사건 line
            return card_types.DelegationFailed(reason=f"모델 호출 실패: {' '.join(str(e).split())}")
        return card_delegate.parse_judgment(result.text, press.kind, [comment.id for comment in comments])

    async def _current_blocks(press, snapshot):
        """The card as Slack holds it now — the door may have settled another row since the
        click. When it cannot be read the click's snapshot stands in, and the log says so."""
        try:
            history = await _client.conversations_history(
                channel=press.channel, latest=press.card_ts, inclusive=True, limit=1
            )
        except Exception as e:  # noqa: BLE001 — a failed read falls back to the snapshot, named
            _LOG.error(
                "%s: press card_ts=%s re-read failed, using the click's card — %s",
                _PLUGIN_NAME,
                press.card_ts,
                e,
            )
            return snapshot
        message = next((m for m in history.get("messages") or [] if m.get("ts") == press.card_ts), None)
        match message:
            case {"blocks": [*blocks]} if blocks:
                return blocks
            case _:
                _LOG.error(
                    "%s: press card_ts=%s not in the re-read, using the click's card",
                    _PLUGIN_NAME,
                    press.card_ts,
                )
                return snapshot

    async def _push(press, snapshot, row) -> None:
        """One chat_update of the card: the pressed row's own blocks spliced into the card as
        Slack holds it now. A Rejected value or a failed call is one log line, never a dead
        handler: the effects do not depend on the card showing them."""
        match row:
            case card_types.Rejected(reason=reason):
                _log_unrendered(press, reason)
                return
        if _client is None:
            return
        await _put(press, await _current_blocks(press, snapshot), row)

    async def _put(press, current, row) -> None:
        blocks = card_view.replace_row(current, press.idx, row)
        match blocks:
            case card_types.Rejected(reason=reason):
                _log_unrendered(press, reason)
                return
        try:
            await _client.chat_update(channel=press.channel, ts=press.card_ts, blocks=blocks)
        except Exception as e:  # noqa: BLE001 — effects may be done; the claim stays so a re-press cannot repeat them
            _LOG.error(
                "%s: press card_ts=%s idx=%s chat_update failed — %s",
                _PLUGIN_NAME,
                press.card_ts,
                press.idx,
                e,
            )

    def _read_row(card_ts: str, idx: int) -> dict | card_types.RowUnread:
        """card_row for (card_ts, idx), read through the door. A dead door and a missing row
        both come home as RowUnread — the row shows it, the press is not taken."""
        try:
            entries = card_delegate.fetch_events("card_row", ROW_WINDOW_HOURS)
        except OSError as e:
            return card_types.RowUnread(reason=" ".join(str(e).split()))
        return card_press.row_from_entries(entries, card_ts, idx)

    async def _show(ref, outcome, lang: str) -> None:
        """One outcome on one row of the card as Slack holds it now — the row keeps its
        buttons for a failure or a received comment (card_view.row_progress)."""
        if _client is None:
            _LOG.error(
                "%s: card_ts=%s idx=%s has no client — cannot show the row's outcome",
                _PLUGIN_NAME,
                ref.card_ts,
                ref.idx,
            )
            return
        current = await _current_blocks(ref, [])
        row = card_view.row_progress(current, ref.idx, outcome, lang=lang)
        match row:
            case card_types.Rejected(reason=reason):
                _log_unrendered(ref, reason)
            case _:
                await _put(ref, current, row)

    async def _open_comment(body) -> None:
        opened = card_press.parse_comment_open(body, owner_id=owner_id)
        if isinstance(opened, card_types.Rejected):
            _LOG.info("%s: comment press rejected — %s", _PLUGIN_NAME, opened.reason)
            return
        if not card_press.answerable(opened.card_ts, time.time()):
            _LOG.info(
                "%s: comment press card_ts=%s idx=%s refused — the card is older than %sh",
                _PLUGIN_NAME,
                opened.card_ts,
                opened.idx,
                card_press.CARD_ANSWERABLE_HOURS,
            )
            return
        lang = card_advice.resolve_lang(boring_config.note_lang())
        view = card_view.comment_modal(card_press.comment_metadata(opened), lang=lang)
        try:
            await _client.views_open(trigger_id=opened.trigger_id, view=view)
        except Exception as e:  # noqa: BLE001 — a modal that would not open is one line and the row's failure mark
            _LOG.error(
                "%s: comment card_ts=%s idx=%s views_open failed — %s",
                _PLUGIN_NAME,
                opened.card_ts,
                opened.idx,
                e,
            )
            reason = card_i18n.STRINGS[lang]["comment_open_failed_reason"]
            await _show(opened, card_types.Failed(reason=reason), lang)

    async def _view_handler(ack, body) -> None:
        """The comment modal's submission: the text, the row it belongs to (card_row, by the
        key the modal carried), one card_comment 사건 with the owner as judge, then the row's
        「💬 받았어요」. Whatever cannot be read or written shows on the row as a failure — and
        the buttons stay."""
        await ack()
        submit = card_press.parse_comment_submit(body, owner_id=owner_id)
        if isinstance(submit, card_types.Rejected):
            _LOG.info("%s: comment rejected — %s", _PLUGIN_NAME, submit.reason)
            return
        if not card_press.answerable(submit.card_ts, time.time()):
            _LOG.info(
                "%s: comment card_ts=%s idx=%s refused — the card is older than %sh",
                _PLUGIN_NAME,
                submit.card_ts,
                submit.idx,
                card_press.CARD_ANSWERABLE_HOURS,
            )
            return
        lang = card_advice.resolve_lang(boring_config.note_lang())
        row = await asyncio.to_thread(_read_row, submit.card_ts, submit.idx)
        if isinstance(row, card_types.RowUnread):
            _LOG.error(
                "%s: comment card_ts=%s idx=%s row unread — %s",
                _PLUGIN_NAME,
                submit.card_ts,
                submit.idx,
                row.reason,
            )
            await _show(submit, card_types.Failed(reason=card_i18n.STRINGS[lang]["row_unread_reason"]), lang)
            return
        fields = card_press.comment_fields(submit, row)
        if isinstance(fields, card_types.Rejected):
            _LOG.error(
                "%s: comment card_ts=%s idx=%s refused — %s",
                _PLUGIN_NAME,
                submit.card_ts,
                submit.idx,
                fields.reason,
            )
            await _show(submit, card_types.Failed(reason=fields.reason), lang)
            return
        try:
            await asyncio.to_thread(card_effects._live_record, "card_comment", fields)
        except Exception as e:  # noqa: BLE001 — a comment that was not stored must not look received
            _LOG.error(
                "%s: comment card_ts=%s idx=%s not recorded — %s", _PLUGIN_NAME, submit.card_ts, submit.idx, e
            )
            await _show(submit, card_types.Failed(reason=" ".join(str(e).split()) or type(e).__name__), lang)
            return
        await _show(submit, card_view.received_outcome(submit.text), lang)

    def _wire_comment_modal(native, adapter) -> None:
        native.view(card_types.COMMENT_CALLBACK_ID)(_view_handler)

    async def _settle_more(press, outcome, lang: str, snapshot) -> None:
        """The lane's [더보기] line on the card as Slack holds it now, settled: only that block
        changes. The page itself is already out (or the press failed before it); this is the
        owner's view of it."""
        if _client is None:
            _LOG.error(
                "%s: card_ts=%s lane=%s has no client — cannot settle the [더보기] line",
                _PLUGIN_NAME,
                press.card_ts,
                press.lane,
            )
            return
        current = await _current_blocks(press, snapshot)
        blocks = card_view.more_progress(current, press.lane, outcome, lang=lang)
        match blocks:
            case card_types.Rejected(reason=reason):
                _LOG.error(
                    "%s: card_ts=%s lane=%s [더보기] line not settled — %s",
                    _PLUGIN_NAME,
                    press.card_ts,
                    press.lane,
                    reason,
                )
                return
        try:
            await _client.chat_update(channel=press.channel, ts=press.card_ts, blocks=blocks)
        except Exception as e:  # noqa: BLE001 — the page is out; a line that did not update is one log line
            _LOG.error(
                "%s: card_ts=%s lane=%s [더보기] chat_update failed — %s",
                _PLUGIN_NAME,
                press.card_ts,
                press.lane,
                e,
            )

    def _read_more_inputs(press) -> card_types.MoreSent | list[card_view.PageRow] | card_types.RowUnread:
        """What a [더보기] press needs from the event log: the lane's card_more (already
        continued → its MoreSent) or else its left-out card_row details as page rows. A dead
        door, a missing row and a row that does not rebuild all come home as RowUnread."""
        try:
            sent = card_press.sent_from_entries(
                card_delegate.fetch_events("card_more", ROW_WINDOW_HOURS), press.card_ts, press.lane
            )
            if sent is not None:
                return sent
            left = card_press.left_out_from_entries(
                card_delegate.fetch_events("card_row", ROW_WINDOW_HOURS), press.card_ts, press.lane
            )
        except OSError as e:
            return card_types.RowUnread(reason=" ".join(str(e).split()))
        if isinstance(left, card_types.RowUnread):
            return left
        rows = card_view.page_rows(press.lane, left)
        if isinstance(rows, card_types.Rejected):
            return card_types.RowUnread(reason=rows.reason)
        return rows

    def _write_page(writes, session: str) -> None:
        """The page's records in their order — the handover between the two groups, as card.py
        writes it, so the page's advice rows can be answered by a verdict on its session."""
        for record in writes.before:
            card_effects._live_record(record.event, record.fields)
        if writes.paths:
            card_effects._live_handover(session, datetime.now(UTC).isoformat(), writes.paths)
        for record in writes.after:
            card_effects._live_record(record.event, record.fields)

    async def _more(body) -> None:
        """[더보기]: the lane's left-out rows go out as one new message in the same DM (no
        thread), drawn from their card_row by the card's own drawing functions — no model. The
        pressed line becomes 「↓ 이어서 보냈어요 (N건)」; a lane already continued is answered with
        the same line and sends nothing. A failure before the page is out shows its reason over
        the button, which stays pressable."""
        press = card_press.parse_more(body, owner_id=owner_id)
        if isinstance(press, card_types.Rejected):
            _LOG.info("%s: more press rejected — %s", _PLUGIN_NAME, press.reason)
            return
        if not card_press.answerable(press.card_ts, time.time()):
            _LOG.info(
                "%s: more press card_ts=%s lane=%s refused — the card is older than %sh",
                _PLUGIN_NAME,
                press.card_ts,
                press.lane,
                card_press.CARD_ANSWERABLE_HOURS,
            )
            return
        key = (press.card_ts, press.lane)
        if key in _more_claimed:
            _LOG.info("%s: more press card_ts=%s lane=%s refused — already taken", _PLUGIN_NAME, *key)
            return
        _more_claimed.add(key)
        lang = card_advice.resolve_lang(boring_config.note_lang())
        message = body.get("message")
        snapshot = (message.get("blocks") if isinstance(message, dict) else None) or []

        async def fail(reason: str) -> None:
            _more_claimed.discard(key)
            await _settle_more(press, card_types.Failed(reason=reason), lang, snapshot)

        read = await asyncio.to_thread(_read_more_inputs, press)
        match read:
            case card_types.MoreSent():
                await _settle_more(press, read, lang, snapshot)
                return
            case card_types.RowUnread(reason=reason):
                _LOG.error("%s: more card_ts=%s lane=%s rows unread — %s", _PLUGIN_NAME, *key, reason)
                await fail(card_i18n.STRINGS[lang]["row_unread_reason"])
                return
        try:
            page = card_view.fit_page(press.lane, read, lang=lang, note_links=boring_config.note_links())
        except ValueError as e:
            _LOG.error("%s: more card_ts=%s lane=%s page refused — %s", _PLUGIN_NAME, *key, e)
            await fail(" ".join(str(e).split()))
            return
        if _client is None:
            _LOG.error("%s: more card_ts=%s lane=%s has no client — cannot post the page", _PLUGIN_NAME, *key)
            _more_claimed.discard(key)
            return
        try:
            posted = await _client.chat_postMessage(
                channel=press.channel, blocks=page.blocks, text=card_view.page_title(press.lane, lang)
            )
        except Exception as e:  # noqa: BLE001 — a page that did not go out is the row's failure mark, and the press can be repeated
            _LOG.error("%s: more card_ts=%s lane=%s post failed — %s", _PLUGIN_NAME, *key, e)
            await fail(" ".join(str(e).split()) or type(e).__name__)
            return
        new_ts = posted["ts"]
        writes = card_press.more_writes(page, press, new_ts, lang)
        try:
            await asyncio.to_thread(_write_page, writes, card_press.session_name(press.channel, new_ts))
        except Exception as e:  # noqa: BLE001 — the page is out and cannot be un-sent; its rows will say they are unreadable
            _LOG.error(
                "%s: more card_ts=%s lane=%s page %s records failed — %s", _PLUGIN_NAME, *key, new_ts, e
            )
        await _settle_more(press, card_types.MoreSent(ts=new_ts, n=len(page.shown)), lang, snapshot)

    async def _handler(ack, body, action) -> None:
        await ack()
        if card_press.is_more_action(body):
            await _more(body)
            return
        if card_press.is_comment_action(body):
            await _open_comment(body)
            return
        parsed = card_press.parse_press(body, owner_id=owner_id)
        if isinstance(parsed, card_types.Rejected):
            _LOG.info("%s: press rejected — %s", _PLUGIN_NAME, parsed.reason)
            return
        if not card_press.answerable(parsed.card_ts, time.time()):
            _LOG.info(
                "%s: press card_ts=%s idx=%s refused — the card is older than %sh",
                _PLUGIN_NAME,
                parsed.card_ts,
                parsed.idx,
                card_press.CARD_ANSWERABLE_HOURS,
            )
            return
        message = body.get("message")
        blocks = message.get("blocks") if isinstance(message, dict) else None
        lang = card_advice.resolve_lang(boring_config.note_lang())
        press = parsed
        if isinstance(parsed, card_types.NeedsRow):
            row = await asyncio.to_thread(_read_row, parsed.card_ts, parsed.idx)
            if isinstance(row, card_types.RowUnread):
                _LOG.error(
                    "%s: press card_ts=%s idx=%s row unread — %s",
                    _PLUGIN_NAME,
                    parsed.card_ts,
                    parsed.idx,
                    row.reason,
                )
                failed = card_types.Failed(reason=card_i18n.STRINGS[lang]["row_unread_reason"])
                await _push(
                    parsed, blocks or [], card_view.row_progress(blocks or [], parsed.idx, failed, lang=lang)
                )
                return
            press = card_press.parse_press(body, owner_id=owner_id, row=row)
            if isinstance(press, card_types.Rejected):
                _LOG.info("%s: press rejected — %s", _PLUGIN_NAME, press.reason)
                return
        key = (press.card_ts, press.idx)
        if key in _claimed:
            _LOG.info(
                "%s: press card_ts=%s idx=%s refused — already pressed",
                _PLUGIN_NAME,
                press.card_ts,
                press.idx,
            )
            return
        marked = card_view.mark_pressed(blocks or [], press, lang=lang)
        if isinstance(marked, card_types.Rejected):
            _LOG.info(
                "%s: press card_ts=%s idx=%s refused — %s",
                _PLUGIN_NAME,
                press.card_ts,
                press.idx,
                marked.reason,
            )
            return
        _claimed.add(key)
        row = card_types.RowRef(channel=press.channel, card_ts=press.card_ts, idx=press.idx)
        snapshot = blocks or []
        await _push(
            press, snapshot, card_view.row_progress(snapshot, press.idx, card_types.Pending(), lang=lang)
        )
        delegated = None
        if isinstance(press, card_types.ReviewPress) and press.choice == card_types.DELEGATE:
            # 맡길게요만 판다: 한 누름에 모델 호출 딱 1회, 진행 중 줄이 떠 있는 동안 — 판정
            # 값이 없는 채 결정표를 부르는 일은 없다(effects 가 loud 하게 거절).
            delegated = await _judge(press)
        effects = card_press.effects(press, delegated=delegated)

        def _apply():
            # One fold over the whole list, in the thread: a failed engine call stops the
            # press where card.py would have stopped it — never a verdict record for a
            # consumption the engine never received.
            return card_effects.run(
                effects,
                card_effects._live_record,
                card_effects._live_consumption,
                lambda subject: card_effects._live_execute_repair(subject, row),
            )

        try:
            results = await asyncio.to_thread(_apply)
        except Exception as e:  # noqa: BLE001 — one bad effect is one line, never a dead handler
            _claimed.discard(key)
            failed = getattr(e, "card_failed_effect", None)
            reason = " ".join(str(e).split()) or type(e).__name__
            await _push(
                press,
                snapshot,
                card_view.row_progress(snapshot, press.idx, card_types.Failed(reason=reason), lang=lang),
            )
            _LOG.error(
                "%s: press card_ts=%s idx=%s effect=%s failed — %s effect(s) skipped — %s",
                _PLUGIN_NAME,
                press.card_ts,
                press.idx,
                failed.effect if failed is not None else "?",
                getattr(e, "card_effects_skipped", "?"),
                e,
            )
            return
        for result in results:
            # A failed merge comes home as a value (F2): one error line, and the card shows
            # the door's own numbers on the row (mark_pressed renders it below).
            if not isinstance(result, card_types.RepairDone):
                _LOG.error(
                    "%s: press card_ts=%s idx=%s execute_repair %s — %s",
                    _PLUGIN_NAME,
                    press.card_ts,
                    press.idx,
                    result.__class__.__name__,
                    getattr(result, "reason", ""),
                )
        repair_result = results[0] if results else None
        _LOG.info(
            "%s: press card_ts=%s idx=%s %s %s applied",
            _PLUGIN_NAME,
            press.card_ts,
            press.idx,
            press.lane,
            press.choice,
        )
        match repair_result:
            case card_types.RepairDone():
                # The door committed the merge and rereads in the background; it settles this
                # row itself when the reread ends, so the row stays "in progress" until then.
                return
            case _:
                if isinstance(delegated, card_types.DelegationFailed):
                    # 모델이 못 답한 누름: 사건 한 줄은 이미 떴고 판정 간선은 없다 — 실패 줄을
                    # 버튼 위에 얹고 클레임을 풀어, 소유자가 다시 누를 수 있게 한다.
                    _claimed.discard(key)
                    await _push(
                        press,
                        snapshot,
                        card_view.row_progress(
                            snapshot, press.idx, card_types.Failed(reason=delegated.reason), lang=lang
                        ),
                    )
                    return
                await _push(
                    press,
                    snapshot,
                    card_view.row_pressed(snapshot, press, lang=lang, repair_result=repair_result),
                )

    ctx.register_slack_action_handler(_ACTION_ID, _handler)
    register_platform_handler = getattr(ctx, "register_platform_handler", None)
    if register_platform_handler is None:
        _LOG.error(
            "%s: ctx.register_platform_handler is not available — the 💬 코멘트 modal opens but "
            "its submission cannot be received",
            _PLUGIN_NAME,
        )
    else:
        register_platform_handler("slack", _wire_comment_modal)
    _LOG.info("%s: card button handler registered (owner %s)", _PLUGIN_NAME, owner_id)
