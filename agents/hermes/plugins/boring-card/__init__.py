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

register() refuses loudly instead of half-registering: without SECRETARY_OWNER_ID no
handler is installed at all — a press from anyone must never reach an effect — and without
BORING_HOME the repo's own modules cannot even be found. All module-level imports stay
stdlib so a broken repo aborts register(), not the plugin's import. The AsyncWebClient
chat_update needs is the plugin's own: hermes wraps plugin action handlers in
(ack, body, action) with no client injected, so register() reads the bot token the gateway
loaded from HERMES_HOME/.env into the environment (gateway/run.py:1611 at v2026.9.24) and
builds one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
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

#: The chat_update client built by register(); None when slack_sdk or the bot token is
#: absent (one error line at register) — presses still take their effects, the card just
#: keeps its buttons.
_client: Any | None = None


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
    import card_effects
    import card_press
    import card_types
    import card_view

    from ohmyboring import config as boring_config

    global _client
    _client = _make_client()
    if _client is None:
        _LOG.error(
            "%s: slack_sdk or SLACK_BOT_TOKEN missing — presses will apply effects but the "
            "card will not be edited in place",
            _PLUGIN_NAME,
        )

    async def _push(press, blocks) -> None:
        """One chat_update of the card — a Rejected value or a failed call is one log line,
        never a dead handler: the effects do not depend on the card showing them."""
        if isinstance(blocks, card_types.Rejected):
            _LOG.error(
                "%s: press card_ts=%s idx=%s could not re-render the card — %s",
                _PLUGIN_NAME,
                press.card_ts,
                press.idx,
                blocks.reason,
            )
            return
        if _client is None:
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

    async def _handler(ack, body, action) -> None:
        await ack()
        press = card_press.parse_press(body, owner_id=owner_id)
        if isinstance(press, card_types.Rejected):
            _LOG.info("%s: press rejected — %s", _PLUGIN_NAME, press.reason)
            return
        if not card_press.answerable(press.card_ts, time.time()):
            _LOG.info(
                "%s: press card_ts=%s idx=%s refused — the card is older than %sh",
                _PLUGIN_NAME,
                press.card_ts,
                press.idx,
                card_press.CARD_ANSWERABLE_HOURS,
            )
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
        message = body.get("message")
        blocks = message.get("blocks") if isinstance(message, dict) else None
        lang = card_advice.resolve_lang(boring_config.note_lang())
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
        effects = card_press.effects(press)
        row = card_types.RowRef(channel=press.channel, card_ts=press.card_ts, idx=press.idx)
        await _push(press, card_view.mark_progress(blocks or [], press.idx, card_types.Pending(), lang=lang))

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
                card_view.mark_progress(blocks or [], press.idx, card_types.Failed(reason=reason), lang=lang),
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
                await _push(
                    press,
                    card_view.mark_pressed(blocks or [], press, lang=lang, repair_result=repair_result),
                )

    ctx.register_slack_action_handler(_ACTION_ID, _handler)
    _LOG.info("%s: card button handler registered (owner %s)", _PLUGIN_NAME, owner_id)
