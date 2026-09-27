"""The morning card's button handler — hermes owns the only Slack socket (wiki-2049), so
every card button press lands here. The handler acks first, parses the press with the same
card_press.parse_press card.py trusts, and folds the shared decision table
(card_press.effects) through the same interpreter and the same live functions card.py's
record_verdict uses (card_effects.run over card_effects._live_*). One table, one
interpreter, wherever a press lands — and one stop rule: the fold halts at the first
failed effect, so a dead engine never leaves a verdict record for a consumption it never
received. A repair the door answers with a non-done value is one log line until slice ②
gives it a card to show on.

register() refuses loudly instead of half-registering: without SECRETARY_OWNER_ID no
handler is installed at all — a press from anyone must never reach an effect — and without
BORING_HOME the repo's own modules cannot even be found. All module-level imports stay
stdlib so a broken repo aborts register(), not the plugin's import.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from typing import Any

_LOG = logging.getLogger(__name__)

#: Every card button's action_id is `card:<idx>:<choice>` — one handler for the whole card.
_ACTION_ID = re.compile(r"^card:")

_PLUGIN_NAME = "boring-card"


def _repo_module_dirs() -> tuple[str, str] | None:
    """The repo's agents/slack + agents/shared dirs via BORING_HOME — no hardcoded path."""
    home = os.environ.get("BORING_HOME")
    if not home:
        return None
    slack = os.path.join(home, "agents", "slack")
    shared = os.path.join(home, "agents", "shared")
    if not (os.path.isdir(slack) and os.path.isdir(shared)):
        return None
    return slack, shared


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
            "not found) — the press parser and effects cannot be imported; no handler "
            "registered",
            _PLUGIN_NAME,
        )
        return
    for path in dirs:
        if path not in sys.path:
            sys.path.insert(0, path)
    import card_effects
    import card_press
    import card_types

    async def _handler(ack, body, action) -> None:
        await ack()
        press = card_press.parse_press(body, owner_id=owner_id)
        if isinstance(press, card_types.Rejected):
            _LOG.info("%s: press rejected — %s", _PLUGIN_NAME, press.reason)
            return
        effects = card_press.effects(press)

        def _apply():
            # One fold over the whole list, in the thread: a failed engine call stops the
            # press where card.py would have stopped it — never a verdict record for a
            # consumption the engine never received.
            return card_effects.run(
                effects,
                card_effects._live_record,
                card_effects._live_consumption,
                card_effects._live_execute_repair,
            )

        try:
            results = await asyncio.to_thread(_apply)
        except Exception as e:  # noqa: BLE001 — one bad effect is one line, never a dead handler
            failed = getattr(e, "card_failed_effect", None)
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
            # A failed merge comes home as a value (F2), and there is no card here to show
            # it on — one line, until slice ② gives it a surface.
            if not isinstance(result, card_types.RepairDone):
                _LOG.error(
                    "%s: press card_ts=%s idx=%s execute_repair %s — %s",
                    _PLUGIN_NAME,
                    press.card_ts,
                    press.idx,
                    result.__class__.__name__,
                    getattr(result, "reason", ""),
                )

    ctx.register_slack_action_handler(_ACTION_ID, _handler)
    _LOG.info("%s: card button handler registered (owner %s)", _PLUGIN_NAME, owner_id)
