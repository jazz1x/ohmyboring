#!/usr/bin/env python3
"""Regression tests for the boring-card hermes plugin.

Run: python3 agents/hermes/plugins/boring-card/test_boring_card.py   (no pytest dependency)

The plugin is the second process that receives the morning card's button presses
(wiki-2049: one Slack app, hermes owns the only socket). These tests pin the contract:
  - the handler acks before any effect runs (Slack demands the ack, hermes within 3 s)
  - an owner advice 「해」 press records card_verdict then writes the used consumption,
    with the same arguments card.py's record_verdict would use — and only after both
    land, one chat_update whose blocks show the pressed row as its judged block
  - a Rejected press runs no effect at all
  - one effect raising stops the fold where card.py would stop it — a dead engine call
    must not leave a verdict record for a consumption it never received — and the
    handler survives on one log line naming the press, the failed effect, and how
    many effects behind it were skipped
  - a press whose row is no longer an actions block on the card (already judged) is
    refused with one log line and no effects; so is a second press racing the first
    before its chat_update lands — (card_ts, idx) is claimed in-process, so two racing
    presses never both run effects
  - a failed effect leaves the card untouched and releases the claim, so the owner can
    press the row again; a failed chat_update is one log line and releases the claim too
  - a repair the door answers with a non-done value is one error line, and the card row
    shows the door's own numbers (done, failed, unanswered each render their own mark)
  - register() without SECRETARY_OWNER_ID (or without BORING_HOME) registers nothing —
    a press nobody vetted must never reach an effect
  - the import path stays langchain-free, because the hermes venv has no langchain

The owner id is generated per run so a plugin that hardcodes the test owner fails.

Mutation targets: moving ack() after the effects kills the ack-first test; letting the
fold continue after a failure kills the stop test; dropping the owner-id check from
register() kills the register-nothing test; a mark_pressed matching rows by position
kills the parity tests in test_card_view; moving chat_update ahead of the effects kills
the order assertion in the in-place edit test.
"""

import asyncio
import contextlib
import importlib.util
import json
import os
import secrets
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
for extra in (REPO / "agents" / "slack", REPO / "agents" / "shared", REPO / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import card_effects  # noqa: E402
import card_press  # noqa: E402
import card_types  # noqa: E402
import card_view  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("boring_card_plugin", HERE / "__init__.py")
PLUGIN = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(PLUGIN)

OWNER = f"U_{secrets.token_hex(4)}"
OTHER = f"U_{secrets.token_hex(4)}"
CARD_TS = "1.0"
CARD_CH = "C1"
# The plugin reads the clock to refuse presses on old cards; tests press a minute after posting.
PLUGIN.time = types.SimpleNamespace(time=lambda: float(CARD_TS) + 60)
ADVICE_VALUE = {"lane": "advice", "note": "wiki-0576"}
REVIEW_VALUE = {"lane": "review", "session": "sess-agent-1", "note": "wiki-0700", "kind": "used"}
REPAIR_VALUE = {"lane": "repair", "subject": "foodspring-front"}

#: The card as a press payload carries it — one repair, two advice, one review row, so
#: action_ids card:0:* … card:3:* all exist. Built once; nothing in these tests mutates it
#: (mark_pressed copies before it re-renders a row).
_REPAIR = card_types.Repair(
    subject="foodspring-front",
    variants=["foodspring front", "foodspring-front"],
    rows=3218,
    notes=212,
)
_PROPOSALS = [
    card_types.Proposal(
        subject="주어 하나",
        note="/vault/wiki/wiki-0576.md",
        register="stalled",
        bottleneck="첫째 병목 열자 이상 문장입니다",
        advice="첫째 조언 열자 이상 문장입니다",
        evidence=[
            card_types.Evidence(note="/vault/wiki/wiki-0576.md", quote="근거 인용문 열자 이상", line=1)
        ],
    ),
    card_types.Proposal(
        subject="주어 둘",
        note="/vault/wiki/wiki-0900.md",
        register="risks",
        bottleneck="둘째 병목 열자 이상 문장입니다",
        advice="둘째 조언 열자 이상 문장입니다",
        evidence=[
            card_types.Evidence(note="/vault/wiki/wiki-0900.md", quote="근거 인용문 열자 이상", line=2)
        ],
    ),
]
_REVIEWS = [
    card_types.ProposedVerdict(
        session_id="sess-agent-1", note="/vault/wiki/wiki-0700.md", kind="used", at="t-1"
    ),
]
BLOCKS = card_view.build_blocks(
    _PROPOSALS, repairs=[_REPAIR], repairs_total_groups=1, reviews=_REVIEWS, lang="ko"
)

#: boring.json pinning the card's display language to Korean, the language BLOCKS render in —
#: the handler resolves the language per press, the way card.py resolves it per run.
_CFG = Path(tempfile.mkdtemp(prefix="boring-card-test-")) / "boring.json"
_CFG.write_text(json.dumps({"note_lang": "ko"}), encoding="utf-8")


class _FakeCtx:
    def __init__(self):
        self.handlers = []

    def register_slack_action_handler(self, action_id, callback):
        self.handlers.append((action_id, callback))


class _FakeClient:
    """Records chat_update calls; raises when asked, to rehearse a dead Slack API."""

    def __init__(self, calls, *, fail=False, current=None):
        self._calls = calls
        self._fail = fail
        # What Slack holds for the card: the last chat_update that landed, or the posted card.
        self.current = current if current is not None else BLOCKS

    async def conversations_history(self, *, channel, latest, inclusive, limit):
        return {"messages": [{"ts": latest, "blocks": self.current}]}

    async def chat_update(self, *, channel, ts, blocks):
        self._calls.append(("chat_update", channel, ts, blocks))
        if self._fail:
            raise RuntimeError("slack unreachable")
        self.current = blocks


@contextlib.contextmanager
def _env():
    """The container's environment — BORING_HOME, the owner, a Korean boring.json — held
    around register() and the presses that follow (the handler resolves the card's language
    per press, so the env must live as long as the run)."""
    with mock.patch.dict(
        os.environ,
        {"BORING_HOME": str(REPO), "SECRETARY_OWNER_ID": OWNER, "BORING_CONFIG": str(_CFG)},
        clear=True,
    ):
        yield


def _register(ctx):
    """register() the way the container runs it. The in-process claim table resets per test
    so one test's presses never refuse as another test's leftovers."""
    PLUGIN._claimed.clear()
    PLUGIN.register(ctx)
    return ctx.handlers


def _body(action_id, value, user=OWNER, blocks=BLOCKS):
    return {
        "type": "block_actions",
        "actions": [{"action_id": action_id, "value": json.dumps(value)}],
        "user": {"id": user},
        "message": {"ts": CARD_TS, "blocks": blocks},
        "channel": {"id": CARD_CH},
    }


def _run(handler, body, calls, *, fail=False):
    async def ack():
        calls.append("ack")

    client = _FakeClient(calls, fail=fail)
    with mock.patch.object(PLUGIN, "_client", client):
        asyncio.run(handler(ack, body, {}))
    return client


def _updates(calls):
    return [c for c in calls if isinstance(c, tuple) and c[0] == "chat_update"]


def _without_updates(calls):
    return [c for c in calls if not (isinstance(c, tuple) and c[0] == "chat_update")]


def _status_at(blocks, idx):
    for b in blocks:
        if b.get("block_id") == f"card:{idx}:status":
            return b["elements"][0]["text"]
    return None


def _assert_single_row_marked(blocks, updated, idx):
    """Exactly one block differs from the card as shown — the row's actions block, now its
    judged block — and the row count is unchanged."""
    prefix = f"card:{idx}:"
    actions_at = next(
        i
        for i, b in enumerate(blocks)
        if b["type"] == "actions" and any(el.get("action_id", "").startswith(prefix) for el in b["elements"])
    )
    assert updated != blocks
    assert len(updated) == len(blocks)
    assert [i for i, (a, b) in enumerate(zip(blocks, updated)) if a != b] == [actions_at]


def _assert_buttons_kept_under_the_reason(blocks, updated, idx):
    """A failed press leaves the card as shown plus one status line right above the row's
    buttons, so the owner can press again."""
    prefix = f"card:{idx}:"
    actions_at = next(
        i
        for i, b in enumerate(blocks)
        if b["type"] == "actions" and any(el.get("action_id", "").startswith(prefix) for el in b["elements"])
    )
    assert updated[:actions_at] == blocks[:actions_at]
    assert updated[actions_at]["block_id"] == f"card:{idx}:status"
    assert updated[actions_at + 1 :] == blocks[actions_at:]


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
            side_effect=execute_repair or (lambda subject, row=None: calls.append(("repair", subject, row))),
        ),
    )
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def test_the_import_path_stays_langchain_free():
    """card_effects, and everything register() imports for the in-place edit, must stay
    importable in the hermes venv — the way back to langchain is card_live, so any of them
    pulling card_live in is the hole this test names."""
    import card_advice  # noqa: F401
    import card_press  # noqa: F401
    import card_view  # noqa: F401

    from ohmyboring import config as boring_config  # noqa: F401

    assert "card_live" not in sys.modules, (
        "the plugin's imports must not import card_live (it pulls langchain)"
    )
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
    with _env():
        handlers = _register(ctx)
    assert len(handlers) == 1
    action_id, handler = handlers[0]
    assert action_id.pattern == r"^card:"
    assert callable(handler)


def test_the_handler_acks_before_any_effect():
    ctx = _FakeCtx()
    with _env():
        _, handler = _register(ctx)[0]
        calls = []
        with _patched_effects(calls):
            _run(handler, _body("card:1:do", ADVICE_VALUE), calls)
    assert calls[0] == "ack", "Slack needs the ack first; an effect before it can time the ack out"


def test_a_press_on_a_card_older_than_the_answerable_hours_runs_no_effect():
    # The next card reads proposals only CARD_ANSWERABLE_HOURS past its verdict window; a
    # later press would leave a verdict with no proposal and stop that card from shipping.
    ctx = _FakeCtx()
    late = float(CARD_TS) + (card_press.CARD_ANSWERABLE_HOURS + 1) * 3600
    with _env(), mock.patch.object(PLUGIN, "time", types.SimpleNamespace(time=lambda: late)):
        _, handler = _register(ctx)[0]
        calls = []
        with _patched_effects(calls):
            _run(handler, _body("card:1:do", ADVICE_VALUE), calls)
    assert calls == ["ack"]


def test_an_owner_advice_do_press_records_then_consumes_and_marks_the_card():
    """The same press card.py would receive must produce the same calls: card_verdict
    first, then the used consumption on the press's own session and note path — and only
    after both land, one chat_update whose blocks show the judged row instead of its
    buttons. A mutant running chat_update before the effects kills the order below."""
    ctx = _FakeCtx()
    with _env():
        _, handler = _register(ctx)[0]
        calls = []
        with _patched_effects(calls):
            _run(handler, _body("card:1:do", ADVICE_VALUE), calls)
    assert [c if isinstance(c, str) else c[0] for c in calls] == [
        "ack",
        "chat_update",
        "record",
        "consumption",
        "chat_update",
    ]
    assert calls[2:4] == [
        ("record", "card_verdict", {"card_ts": CARD_TS, "idx": 1, "choice": "do"}),
        ("consumption", "slack:C1:1.0", "used", ["/vault/wiki/wiki-0576.md"]),
    ]
    progress, final = _updates(calls)
    assert progress[1:3] == (CARD_CH, CARD_TS) and final[1:3] == (CARD_CH, CARD_TS)
    _assert_single_row_marked(BLOCKS, progress[3], 1)
    assert _status_at(progress[3], 1) == "⏳ 진행 중…"
    _assert_single_row_marked(BLOCKS, final[3], 1)
    assert f"✓ 채택 — <@{OWNER}>" in json.dumps(final[3], ensure_ascii=False)


def test_every_lane_shows_progress_first_and_only_that_row_changes():
    """Advice, review and repair-hold presses each: the first chat_update turns just the pressed
    row into 「진행 중」 before any effect ran; the last one settles the same row."""
    cases = [
        ("card:1:do", ADVICE_VALUE, 1),
        ("card:3:drop", REVIEW_VALUE, 3),
        ("card:0:defer", REPAIR_VALUE, 0),
    ]
    for action_id, value, idx in cases:
        ctx = _FakeCtx()
        with _env():
            _, handler = _register(ctx)[0]
            calls = []
            with _patched_effects(calls):
                _run(handler, _body(action_id, value), calls)
        progress, final = _updates(calls)
        first_effect = min(
            (i for i, c in enumerate(calls) if isinstance(c, tuple) and c[0] in ("record", "consumption")),
            default=len(calls),
        )
        assert calls.index(progress) < first_effect, (
            f"{action_id}: progress must land before the first effect"
        )
        _assert_single_row_marked(BLOCKS, progress[3], idx)
        assert _status_at(progress[3], idx) == "⏳ 진행 중…"
        _assert_single_row_marked(BLOCKS, final[3], idx)
        assert _status_at(final[3], idx) is None, (
            f"{action_id}: the settled row is a verdict, not the progress line"
        )


def test_a_rejected_press_runs_no_effect():
    """A non-owner press is a Rejected value — one log line, zero effects."""
    ctx = _FakeCtx()
    with _env():
        _, handler = _register(ctx)[0]
        calls = []
        with (
            _patched_effects(calls),
            mock.patch.object(PLUGIN, "_LOG") as log,
        ):
            _run(handler, _body("card:1:do", ADVICE_VALUE, user=OTHER), calls)
    assert calls == ["ack"]
    assert log.info.called, "the rejection reason must be logged"


def test_a_raising_effect_stops_the_fold_and_the_handler_survives():
    """A flip press is [consumption, record] — the consumption raising must stop the fold
    before the record, or a dead engine leaves a flip verdict it never received. One log
    line names the press, the failed effect, and the one effect skipped; the handler
    itself returns, and the card is not touched."""

    def boom(session, kind, paths):
        raise RuntimeError("engine down")

    ctx = _FakeCtx()
    with _env():
        _, handler = _register(ctx)[0]
        calls = []
        with (
            _patched_effects(calls, consumption=boom),
            mock.patch.object(PLUGIN, "_LOG") as log,
        ):
            _run(handler, _body("card:3:drop", REVIEW_VALUE), calls)
    assert _without_updates(calls) == ["ack"], "the record behind the dead engine call must never run"
    assert _status_at(_updates(calls)[-1][3], 3) == "✕ 실패 — engine down"
    log.error.assert_called_once()
    logged = log.error.call_args.args
    assert CARD_TS in logged and 3 in logged and "consumption" in logged
    assert 1 in logged, "the log line must say how many effects were skipped"


def test_an_already_judged_row_is_refused_with_one_log_line():
    """The card's blocks ride the press: once the row is a judged context block and no
    longer an actions block, the press is refused — no effects, no chat_update, one line."""

    def judged_blocks():
        return card_view.build_blocks(
            _PROPOSALS,
            [card_types.ButtonVerdict(idx=1, choice="do", user=OWNER, at="t")],
            repairs=[_REPAIR],
            repairs_total_groups=1,
            reviews=_REVIEWS,
            lang="ko",
        )

    ctx = _FakeCtx()
    with _env():
        _, handler = _register(ctx)[0]
        calls = []
        with (
            _patched_effects(calls),
            mock.patch.object(PLUGIN, "_LOG") as log,
        ):
            _run(handler, _body("card:1:do", ADVICE_VALUE, blocks=judged_blocks()), calls)
    assert calls == ["ack"]
    log.info.assert_called_once()
    assert "refused" in str(log.info.call_args)


def test_a_racing_second_press_is_refused_while_the_card_still_shows_buttons():
    """Two presses on one row landing before the first chat_update reaches Slack: the second
    press's payload still shows the row's buttons, but (card_ts, idx) is claimed in-process —
    one log line, and the effects and the update run exactly once between the two presses."""
    ctx = _FakeCtx()
    with _env():
        _, handler = _register(ctx)[0]
        calls = []
        with (
            _patched_effects(calls),
            mock.patch.object(PLUGIN, "_LOG") as log,
        ):
            _run(handler, _body("card:1:do", ADVICE_VALUE), calls)
            first = len(calls)
            _run(handler, _body("card:1:do", ADVICE_VALUE), calls)
    assert calls[first:] == ["ack"], "the racing press must not re-run effects or chat_update"
    assert log.info.called, "the refusal is one log line"
    assert len(_updates(calls)) == 2, "one press = its progress and its final; the refused press adds none"
    verdict_records = [
        c for c in calls if c == ("record", "card_verdict", {"card_ts": CARD_TS, "idx": 1, "choice": "do"})
    ]
    assert len(verdict_records) == 1


def test_a_failed_effect_shows_the_reason_on_the_row_and_releases_the_claim():
    """A dead engine call stops the press where card.py would stop it: the row goes from
    「진행 중」 to 「✕ 실패 — reason」, and the (card_ts, idx) claim is released, so a press on a
    card that still shows the buttons (a lost update) runs its effects again."""
    state = {"down": True}

    def flaky(session, kind, paths):
        if state["down"]:
            state["down"] = False
            raise RuntimeError("engine down")
        calls.append(("consumption", session, kind, paths))

    ctx = _FakeCtx()
    with _env():
        _, handler = _register(ctx)[0]
        calls = []
        with (
            _patched_effects(calls, consumption=flaky),
            mock.patch.object(PLUGIN, "_LOG"),
        ):
            _run(handler, _body("card:1:do", ADVICE_VALUE), calls)
            assert _without_updates(calls) == [
                "ack",
                ("record", "card_verdict", {"card_ts": CARD_TS, "idx": 1, "choice": "do"}),
            ]
            progress, failed = _updates(calls)
            assert _status_at(progress[3], 1) == "⏳ 진행 중…"
            assert _status_at(failed[3], 1) == "✕ 실패 — engine down"
            _assert_buttons_kept_under_the_reason(BLOCKS, failed[3], 1)
            _run(handler, _body("card:1:do", ADVICE_VALUE), calls)
    assert ("consumption", "slack:C1:1.0", "used", ["/vault/wiki/wiki-0576.md"]) in calls
    assert len(_updates(calls)) == 4


def test_a_failed_chat_update_is_one_log_line_and_keeps_the_claim():
    """chat_update raising leaves the card showing buttons, but the effects already ran —
    one error line naming the press, and a re-press is refused rather than run twice."""
    ctx = _FakeCtx()
    with _env():
        _, handler = _register(ctx)[0]
        calls = []
        with (
            _patched_effects(calls),
            mock.patch.object(PLUGIN, "_LOG") as log,
        ):
            _run(handler, _body("card:1:do", ADVICE_VALUE), calls, fail=True)
            assert calls[2:4] == [
                ("record", "card_verdict", {"card_ts": CARD_TS, "idx": 1, "choice": "do"}),
                ("consumption", "slack:C1:1.0", "used", ["/vault/wiki/wiki-0576.md"]),
            ]
            assert len(_updates(calls)) == 2, "progress and final are each attempted once"
            assert log.error.call_count == 2, "each failed update is one line, and the effects still ran"
            logged = log.error.call_args.args
            assert CARD_TS in logged and 1 in logged
            assert any("chat_update" in str(a) for a in logged)
            _run(handler, _body("card:1:do", ADVICE_VALUE), calls)
    assert calls[5:] == ["ack"], "the re-press must not repeat the effects"
    assert len(_updates(calls)) == 2


def test_a_repair_answered_with_a_non_done_value_is_one_log_line_and_marks_the_row():
    """The door answers a failed merge with a value (F2), not a raise — one error line, and
    the row shows the door's own numbers: never the plain ✓ 채택 mark, never silence."""

    def merge_fails(subject, row=None):
        calls.append(("repair", subject))
        return card_types.RepairFailed(
            subject=subject,
            deleted_rows=5,
            reread_notes=2,
            reason="engine unreachable: connection refused",
            owner_held=[],
        )

    ctx = _FakeCtx()
    with _env():
        _, handler = _register(ctx)[0]
        calls = []
        with (
            _patched_effects(calls, execute_repair=merge_fails),
            mock.patch.object(PLUGIN, "_LOG") as log,
        ):
            _run(handler, _body("card:0:do", REPAIR_VALUE), calls)
    assert ("repair", "foodspring-front") in calls
    log.error.assert_called_once()
    logged = log.error.call_args.args
    assert CARD_TS in logged and 0 in logged and "RepairFailed" in logged
    updates = _updates(calls)
    assert len(updates) == 2
    _, channel, ts, updated = updates[-1]
    assert (channel, ts) == (CARD_CH, CARD_TS)
    _assert_single_row_marked(BLOCKS, updated, 0)
    rendered = json.dumps(updated, ensure_ascii=False)
    assert "✕ 합침 실패" in rendered and "지운 행 5" in rendered


def test_an_accepted_merge_stays_in_progress_and_hands_the_door_its_row():
    """The door answered 202 and rereads in the background: the plugin writes no final mark —
    the door settles the row when the reread ends — and it names the row in the request."""
    done = card_types.RepairDone(subject="foodspring-front", deleted_rows=5, reread_notes=2)
    seen = []
    ctx = _FakeCtx()
    with _env():
        _, handler = _register(ctx)[0]
        calls = []
        with (
            _patched_effects(calls, execute_repair=lambda subject, row=None: seen.append(row) or done),
            mock.patch.object(PLUGIN, "_LOG") as log,
        ):
            _run(handler, _body("card:0:do", REPAIR_VALUE), calls)
    assert not log.error.called
    assert seen == [card_types.RowRef(channel=CARD_CH, card_ts=CARD_TS, idx=0)]
    (progress,) = _updates(calls)
    _assert_single_row_marked(BLOCKS, progress[3], 0)
    assert _status_at(progress[3], 0) == "⏳ 진행 중…"


def test_a_row_the_door_settled_mid_press_is_not_rolled_back():
    """The door settles row 0 while row 1's effects run; row 1's final update is built from
    the card as Slack holds it then, so row 0 keeps the door's mark instead of the click's
    snapshot putting it back."""
    ctx = _FakeCtx()
    calls = []
    client = _FakeClient(calls)

    def door_settles_row_0(session, kind, paths):
        client.current = card_view.mark_progress(
            client.current, 0, card_types.Done(text="✓ 완료 — door"), lang="ko"
        )
        calls.append(("consumption", session, kind, paths))

    async def ack():
        calls.append("ack")

    with _env():
        _, handler = _register(ctx)[0]
        with (
            _patched_effects(calls, consumption=door_settles_row_0),
            mock.patch.object(PLUGIN, "_client", client),
        ):
            asyncio.run(handler(ack, _body("card:1:do", ADVICE_VALUE), {}))
    final = _updates(calls)[-1][3]
    assert _status_at(final, 0) == "✓ 완료 — door"
    assert _status_at(final, 1) is None, "row 1 is judged, not in progress"


def _press_row_1_with(consumption, times):
    """Press row 1 `times` times through the real handler against one fake Slack that keeps
    the last card it was sent — a re-press carries that card, as Slack's payload would."""
    ctx = _FakeCtx()
    calls = []
    client = _FakeClient(calls)

    async def ack():
        calls.append("ack")

    with _env():
        _, handler = _register(ctx)[0]
        with (
            _patched_effects(calls, consumption=consumption),
            mock.patch.object(PLUGIN, "_client", client),
            mock.patch.object(PLUGIN, "_LOG"),
        ):
            for _ in range(times):
                asyncio.run(handler(ack, _body("card:1:do", ADVICE_VALUE, blocks=client.current), {}))
    for update in _updates(calls):
        ids = [b.get("block_id") for b in update[3] if b.get("block_id")]
        assert len(ids) == len(set(ids)), f"duplicate block_id in an update (Slack refuses it): {ids}"
    return client


def test_the_same_failure_twice_keeps_the_row_and_its_buttons():
    def down(session, kind, paths):
        raise RuntimeError("engine unreachable")

    card = _press_row_1_with(down, 2).current
    assert len(card) == len(BLOCKS) + 1, "one reason line added above the buttons, nothing lost"
    assert _status_at(card, 1) == "✕ 실패 — engine unreachable"
    assert any(
        b["type"] == "actions" and any(el.get("action_id", "").startswith("card:1:") for el in b["elements"])
        for b in card
    )


def test_a_retry_that_succeeds_leaves_no_old_failure_line():
    state = {"down": True}

    def flaky(session, kind, paths):
        if state["down"]:
            state["down"] = False
            raise RuntimeError("engine down")

    card = _press_row_1_with(flaky, 2).current
    assert _status_at(card, 1) is None
    assert len(card) == len(BLOCKS)
    assert not any("✕ 실패" in json.dumps(b, ensure_ascii=False) for b in card)


if __name__ == "__main__":
    test_the_import_path_stays_langchain_free()
    test_register_without_secretary_owner_id_registers_nothing()
    test_register_without_boring_home_registers_nothing()
    test_register_hands_one_handler_the_card_action_ids()
    test_the_handler_acks_before_any_effect()
    test_a_press_on_a_card_older_than_the_answerable_hours_runs_no_effect()
    test_an_owner_advice_do_press_records_then_consumes_and_marks_the_card()
    test_every_lane_shows_progress_first_and_only_that_row_changes()
    test_a_rejected_press_runs_no_effect()
    test_a_raising_effect_stops_the_fold_and_the_handler_survives()
    test_an_already_judged_row_is_refused_with_one_log_line()
    test_a_racing_second_press_is_refused_while_the_card_still_shows_buttons()
    test_a_failed_effect_shows_the_reason_on_the_row_and_releases_the_claim()
    test_a_failed_chat_update_is_one_log_line_and_keeps_the_claim()
    test_a_repair_answered_with_a_non_done_value_is_one_log_line_and_marks_the_row()
    test_an_accepted_merge_stays_in_progress_and_hands_the_door_its_row()
    test_a_row_the_door_settled_mid_press_is_not_rolled_back()
    test_the_same_failure_twice_keeps_the_row_and_its_buttons()
    test_a_retry_that_succeeds_leaves_no_old_failure_line()
    print("ok - boring-card plugin: ack-first, in-place mark, double-press refused, loud refusals")
