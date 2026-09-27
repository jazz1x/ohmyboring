#!/usr/bin/env python3
"""Regression tests for the boring-card hermes plugin.

Run: python3 agents/hermes/plugins/boring-card/test_boring_card.py   (no pytest dependency)

The plugin is the second process that receives the morning card's button presses
(wiki-2049: one Slack app, hermes owns the only socket). These tests pin the contract:
  - the handler acks before any effect runs (Slack demands the ack, hermes within 3 s)
  - an owner advice 「해」 press records card_verdict then writes the used consumption,
    with the same arguments card.py's record_verdict would use
  - a Rejected press runs no effect at all
  - one effect raising is one log line naming the press and the effect — the handler
    survives and the remaining effects still run
  - register() without SECRETARY_OWNER_ID (or without BORING_HOME) registers nothing —
    a press nobody vetted must never reach an effect
  - the import path stays langchain-free, because the hermes venv has no langchain

Mutation targets: moving ack() after the effects kills the first test; dropping the
owner-id check from register() kills the register-nothing test.
"""

import asyncio
import contextlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
for extra in (REPO / "agents" / "slack", REPO / "agents" / "shared"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import card_effects  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("boring_card_plugin", HERE / "__init__.py")
PLUGIN = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(PLUGIN)

OWNER = "U_OWNER"
OTHER = "U_OTHER"
CARD_TS = "1.0"
CARD_CH = "C1"
ADVICE_VALUE = {"lane": "advice", "note": "wiki-0576"}
REVIEW_VALUE = {"lane": "review", "session": "sess-agent-1", "note": "wiki-0700", "kind": "used"}


class _FakeCtx:
    def __init__(self):
        self.handlers = []

    def register_slack_action_handler(self, action_id, callback):
        self.handlers.append((action_id, callback))


def _body(action_id, value, user=OWNER):
    return {
        "type": "block_actions",
        "actions": [{"action_id": action_id, "value": json.dumps(value)}],
        "user": {"id": user},
        "message": {"ts": CARD_TS},
        "channel": {"id": CARD_CH},
    }


def _register(ctx):
    """register() the way the container runs it: BORING_HOME + SECRETARY_OWNER_ID set."""
    with mock.patch.dict(os.environ, {"BORING_HOME": str(REPO), "SECRETARY_OWNER_ID": OWNER}, clear=True):
        PLUGIN.register(ctx)
    return ctx.handlers


def _run(handler, body, calls):
    async def ack():
        calls.append("ack")

    asyncio.run(handler(ack, body, {}))
    return calls


@contextlib.contextmanager
def _patched_effects(calls, consumption=None, execute_repair=None):
    """The plugin's live functions replaced by recorders (or the given raising stub)."""
    patches = (
        mock.patch.object(
            card_effects,
            "_live_record",
            side_effect=lambda event, fields: calls.append(("record", event, fields)),
        ),
        mock.patch.object(
            card_effects,
            "_live_consumption",
            side_effect=consumption
            or (lambda session, kind, paths: calls.append(("consumption", session, kind, paths))),
        ),
        mock.patch.object(
            card_effects,
            "_live_execute_repair",
            side_effect=execute_repair or (lambda subject: calls.append(("repair", subject))),
        ),
    )
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def test_the_import_path_stays_langchain_free():
    """card_effects must stay importable in the hermes venv — the way back to langchain
    is card_live, so card_effects pulling card_live in is the hole this test names."""
    import card_press  # noqa: F401

    assert "card_live" not in sys.modules, "card_effects must not import card_live (it pulls langchain)"
    leaked = sorted(m for m in sys.modules if m.startswith(("langchain", "langgraph")))
    assert not leaked, f"the hermes venv has no langchain — the plugin's imports leaked: {leaked}"


def test_register_without_secretary_owner_id_registers_nothing():
    """No owner configured → no handler at all: never accept a press from anyone. BORING_HOME
    stays valid so the refusal is attributable to the missing owner alone."""
    ctx = _FakeCtx()
    with (
        mock.patch.dict(os.environ, {"BORING_HOME": str(REPO)}, clear=True),
        mock.patch.object(PLUGIN, "_LOG") as log,
    ):
        PLUGIN.register(ctx)
    assert ctx.handlers == []
    assert log.error.called


def test_register_without_boring_home_registers_nothing():
    ctx = _FakeCtx()
    with (
        mock.patch.dict(os.environ, {"SECRETARY_OWNER_ID": OWNER}, clear=True),
        mock.patch.object(PLUGIN, "_LOG") as log,
    ):
        PLUGIN.register(ctx)
    assert ctx.handlers == []
    assert log.error.called


def test_register_hands_one_handler_the_card_action_ids():
    ctx = _FakeCtx()
    handlers = _register(ctx)
    assert len(handlers) == 1
    action_id, handler = handlers[0]
    assert action_id.pattern == r"^card:"
    assert callable(handler)


def test_the_handler_acks_before_any_effect():
    ctx = _FakeCtx()
    _, handler = _register(ctx)[0]
    calls = []
    with _patched_effects(calls):
        _run(handler, _body("card:1:do", ADVICE_VALUE), calls)
    assert calls[0] == "ack", "Slack needs the ack first; an effect before it can time the ack out"


def test_an_owner_advice_do_press_records_then_consumes_with_the_card_args():
    """The same press card.py would receive must produce the same calls: card_verdict
    first, then the used consumption on the press's own session and note path."""
    ctx = _FakeCtx()
    _, handler = _register(ctx)[0]
    calls = []
    with _patched_effects(calls):
        _run(handler, _body("card:1:do", ADVICE_VALUE), calls)
    assert calls == [
        "ack",
        ("record", "card_verdict", {"card_ts": CARD_TS, "idx": 1, "choice": "do"}),
        ("consumption", "slack:C1:1.0", "used", ["/vault/wiki/wiki-0576.md"]),
    ]


def test_a_rejected_press_runs_no_effect():
    """A non-owner press is a Rejected value — one log line, zero effects."""
    ctx = _FakeCtx()
    _, handler = _register(ctx)[0]
    calls = []
    with (
        _patched_effects(calls),
        mock.patch.object(PLUGIN, "_LOG") as log,
    ):
        _run(handler, _body("card:1:do", ADVICE_VALUE, user=OTHER), calls)
    assert calls == ["ack"]
    assert log.info.called, "the rejection reason must be logged"


def test_a_raising_effect_is_one_log_line_and_the_rest_still_runs():
    """A flip press is [consumption, record] — the consumption raising must not eat the
    record behind it, and must not escape the handler as a crash."""

    def boom(session, kind, paths):
        raise RuntimeError("engine down")

    ctx = _FakeCtx()
    _, handler = _register(ctx)[0]
    calls = []
    with (
        _patched_effects(calls, consumption=boom),
        mock.patch.object(PLUGIN, "_LOG") as log,
    ):
        _run(handler, _body("card:2:drop", REVIEW_VALUE), calls)
    assert (
        "record",
        "verdict_reviewed",
        {
            "session_id": "sess-agent-1",
            "note": "/vault/wiki/wiki-0700.md",
            "proposed_kind": "used",
            "card_ts": CARD_TS,
            "choice": "flip",
        },
    ) in calls, "the effect behind the failing one must still run"
    log.error.assert_called_once()
    logged = log.error.call_args.args
    assert CARD_TS in logged and 2 in logged and "consumption" in logged


if __name__ == "__main__":
    test_the_import_path_stays_langchain_free()
    test_register_without_secretary_owner_id_registers_nothing()
    test_register_without_boring_home_registers_nothing()
    test_register_hands_one_handler_the_card_action_ids()
    test_the_handler_acks_before_any_effect()
    test_an_owner_advice_do_press_records_then_consumes_with_the_card_args()
    test_a_rejected_press_runs_no_effect()
    test_a_raising_effect_is_one_log_line_and_the_rest_still_runs()
    print("ok - boring-card plugin: ack-first, same effects, loud refusals, langchain-free")
