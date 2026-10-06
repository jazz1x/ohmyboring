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
  - a review 「맡길게요」 press judges: the model is called exactly once (a fake LLM counts),
    and the judged kind — not the proposed word flag — rides the agent:delegated edge; a
    model that cannot answer (dead call, malformed JSON, no model seat, unreadable note)
    leaves no consumption at all, records the 사건 one line with the reason, and the row
    shows 「✕ 실패 — reason」 over its buttons with the claim released
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

import card_delegate  # noqa: E402
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
#: The ts Slack gives the [더보기] page a press posts.
PAGE_TS = "2.0"
CARD_CH = "C1"
# The plugin reads the clock to refuse presses on old cards; tests press a minute after posting.
PLUGIN.time = types.SimpleNamespace(time=lambda: float(CARD_TS) + 60)
ADVICE_VALUE = {"lane": "advice", "note": "wiki-0576"}
REVIEW_VALUE = {"lane": "review", "session": "sess-agent-1", "note": "wiki-0700", "kind": "used"}
REVIEW_GROUPED_VALUE = {
    "lane": "review",
    "session": "sess-agent-1",
    "sessions": ["sess-agent-1", "sess-agent-2"],
    "note": "wiki-0700",
    "kind": "used",
}
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
_BUILT = card_view.build_card(
    _PROPOSALS, repairs=[_REPAIR], repairs_total_groups=1, reviews=_REVIEWS, lang="ko"
)
BLOCKS = _BUILT.blocks
#: The card_row 사건 entries card.post_card would have written for BLOCKS, as /events returns them.
ROW_ENTRIES = [
    {
        "id": 100 + row.idx,
        "observed_at": "2026-10-06T00:00:00+00:00",
        "attributes": {"card_ts": CARD_TS, "idx": row.idx, "lane": row.lane, "value": row.value},
    }
    for row in _BUILT.rows
]

#: boring.json pinning the card's display language to Korean, the language BLOCKS render in —
#: the handler resolves the language per press, the way card.py resolves it per run.
_CFG = Path(tempfile.mkdtemp(prefix="boring-card-test-")) / "boring.json"
_CFG.write_text(json.dumps({"note_lang": "ko"}), encoding="utf-8")


class _FakeLlm:
    """The host's ctx.llm facade, scripted: a JSON answer or a raise, counting calls so a
    press can never spend more than its one model call."""

    def __init__(self, calls, *, answer=None, error=None):
        self._calls = calls
        self._answer = answer
        self._error = error

    async def acomplete(self, messages, **kwargs):
        self._calls.append(("model", messages[0]["content"], kwargs))
        if self._error is not None:
            raise self._error
        return types.SimpleNamespace(text=self._answer)


class _FakeCtx:
    def __init__(self, llm=None):
        self.handlers = []
        self.platform_factories = []
        self.llm = llm

    def register_slack_action_handler(self, action_id, callback):
        self.handlers.append((action_id, callback))

    def register_platform_handler(self, platform, factory):
        self.platform_factories.append((platform, factory))


class _FakeApp:
    """The slack_bolt AsyncApp a platform-handler factory receives: records `app.view(id)(fn)`."""

    def __init__(self):
        self.views = {}

    def view(self, callback_id):
        def wire(fn):
            self.views[callback_id] = fn
            return fn

        return wire


class _FakeClient:
    """Records chat_update calls; raises when asked, to rehearse a dead Slack API."""

    def __init__(self, calls, *, fail=False, current=None, open_fail=False, post_fail=False):
        self._calls = calls
        self._fail = fail
        self._open_fail = open_fail
        self._post_fail = post_fail
        # What Slack holds for the card: the last chat_update that landed, or the posted card.
        self.current = current if current is not None else BLOCKS

    async def conversations_history(self, *, channel, latest, inclusive, limit):
        return {"messages": [{"ts": latest, "blocks": self.current}]}

    async def views_open(self, *, trigger_id, view):
        self._calls.append(("views_open", trigger_id, view))
        if self._open_fail:
            raise RuntimeError("trigger_id expired")

    async def chat_postMessage(self, **kwargs):
        self._calls.append(("post", kwargs))
        if self._post_fail:
            raise RuntimeError("slack down")
        return {"ts": PAGE_TS}

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
    PLUGIN._more_claimed.clear()
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


def _run(handler, body, calls, *, fail=False, open_fail=False, post_fail=False, current=None):
    async def ack():
        calls.append("ack")

    client = _FakeClient(calls, fail=fail, open_fail=open_fail, post_fail=post_fail, current=current)
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
            or (
                lambda session, kind, paths, judge=None: calls.append(
                    ("consumption", session, kind, paths, judge)
                )
            ),
        ),
        mock.patch.object(
            card_effects,
            "_live_execute_repair",
            side_effect=execute_repair or (lambda subject, row=None: calls.append(("repair", subject, row))),
        ),
        mock.patch.object(
            card_effects,
            "_live_handover",
            side_effect=lambda session, at, paths: calls.append(("handover", session, paths)),
        ),
    )
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


@contextlib.contextmanager
def _patched_delegate(
    calls, note_text="---\ntitle: t\n---\n노트 본문입니다", evidence="채점이 잡은 문장입니다.", comments=None
):
    """The judgment seat's live reads replaced by fakes — a press must never touch the
    host vault or the engine's /events in these tests. `comments` is what the owner left on
    the note: a list of OwnerComment, or an exception to raise."""

    def read_comments(path):
        if isinstance(comments, Exception):
            raise comments
        return list(comments or [])

    with (
        mock.patch.object(card_delegate, "comments_for_note", side_effect=read_comments),
        mock.patch.object(
            card_delegate,
            "read_note_text",
            side_effect=lambda path: calls.append(("read_note", path)) or note_text,
        ),
        mock.patch.object(
            card_delegate,
            "proposal_evidence",
            side_effect=lambda sessions, path, kind: (
                calls.append(("read_evidence", tuple(sessions), path, kind)) or evidence
            ),
        ),
    ):
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
        ("consumption", "slack:C1:1.0", "used", ["/vault/wiki/wiki-0576.md"], None),
    ]
    progress, final = _updates(calls)
    assert progress[1:3] == (CARD_CH, CARD_TS) and final[1:3] == (CARD_CH, CARD_TS)
    _assert_single_row_marked(BLOCKS, progress[3], 1)
    assert _status_at(progress[3], 1) == "⏳ 진행 중…"
    _assert_single_row_marked(BLOCKS, final[3], 1)
    assert f"✓ 채택 — <@{OWNER}>" in json.dumps(final[3], ensure_ascii=False)


def test_an_owner_review_delegate_press_consumes_with_the_agent_judge():
    """맡길게요: the model judges once and its call stands — REVIEW_VALUE proposes 'used';
    the fake model answers wrong, so the judged edge is contested (never the word flag),
    judge agent:delegated (never owner), the verdict_reviewed 사건 carries the judged kind
    and the model's 이유, and only then the row settles as delegated. A mutant stamping the
    proposed kind, or skipping the model call, goes red here."""
    calls = []
    ctx = _FakeCtx(llm=_FakeLlm(calls, answer='{"verdict": "wrong", "reason": "본문과 맞지 않는 옛 기록"}'))
    with _env():
        _, handler = _register(ctx)[0]
        with _patched_effects(calls), _patched_delegate(calls):
            _run(handler, _body("card:3:delegate", REVIEW_VALUE), calls)
    assert [c if isinstance(c, str) else c[0] for c in calls] == [
        "ack",
        "chat_update",
        "read_note",
        "read_evidence",
        "model",
        "consumption",
        "record",
        "chat_update",
    ]
    assert len([c for c in calls if isinstance(c, tuple) and c[0] == "model"]) == 1
    assert calls[5:7] == [
        ("consumption", "sess-agent-1", "contested", ["/vault/wiki/wiki-0700.md"], "agent:delegated"),
        (
            "record",
            "verdict_reviewed",
            {
                "session_id": "sess-agent-1",
                "note": "/vault/wiki/wiki-0700.md",
                "proposed_kind": "used",
                "card_ts": CARD_TS,
                "choice": "delegate",
                "judge": "agent:delegated",
                "kind": "contested",
                "reason": "본문과 맞지 않는 옛 기록",
                "comment_ids": [],
            },
        ),
    ]
    progress, final = _updates(calls)
    _assert_single_row_marked(BLOCKS, final[3], 3)
    assert "✓ 맡김 — 에이전트 판정 그대로" in json.dumps(final[3], ensure_ascii=False)


def test_a_delegate_press_where_the_model_agrees_keeps_the_proposed_kind():
    calls = []
    ctx = _FakeCtx(llm=_FakeLlm(calls, answer='{"verdict": "right", "reason": "본문 그대로입니다"}'))
    with _env():
        _, handler = _register(ctx)[0]
        with _patched_effects(calls), _patched_delegate(calls):
            _run(handler, _body("card:3:delegate", REVIEW_VALUE), calls)
    assert ("consumption", "sess-agent-1", "used", ["/vault/wiki/wiki-0700.md"], "agent:delegated") in calls
    record = next(c for c in calls if isinstance(c, tuple) and c[0] == "record")
    assert record[2]["kind"] == "used" and record[2]["reason"] == "본문 그대로입니다"


def test_a_grouped_delegate_press_calls_the_model_once_and_fans_out_the_judgment():
    """묶인 줄의 한 누름에 모델 호출은 1회 — 판정은 묶인 세션 전부에 간다."""
    calls = []
    ctx = _FakeCtx(llm=_FakeLlm(calls, answer='{"verdict": "wrong", "reason": "모델 이유"}'))
    with _env():
        _, handler = _register(ctx)[0]
        with _patched_effects(calls), _patched_delegate(calls):
            _run(handler, _body("card:3:delegate", REVIEW_GROUPED_VALUE), calls)
    assert len([c for c in calls if isinstance(c, tuple) and c[0] == "model"]) == 1
    assert (
        "consumption",
        "sess-agent-1",
        "contested",
        ["/vault/wiki/wiki-0700.md"],
        "agent:delegated",
    ) in calls
    assert (
        "consumption",
        "sess-agent-2",
        "contested",
        ["/vault/wiki/wiki-0700.md"],
        "agent:delegated",
    ) in calls
    assert ("read_evidence", ("sess-agent-1", "sess-agent-2"), "/vault/wiki/wiki-0700.md", "used") in calls


def test_a_model_failure_leaves_no_edge_and_records_the_fact():
    """모델이 못 답하면(여기선 죽은 호출) 판정을 남기지 않는다: consumption 0, error 를 실은
    사건 한 줄, 그리고 실패 줄은 이유와 함께 「✕ 실패」로 버튼을 살린 채 남는다 — 조용히
    낱말 표지로 되돌아가는 변이(제안 kind 의 간선)는 이 시험이 잡는다. 클레임은 풀려
    다시 누를 수 있고, 다시 누름에도 모델 호출은 누름당 정확히 1번이다."""
    calls = []
    llm = _FakeLlm(calls, error=RuntimeError("model unreachable"))
    ctx = _FakeCtx(llm=llm)
    with _env():
        _, handler = _register(ctx)[0]
        with (
            _patched_effects(calls),
            _patched_delegate(calls),
            mock.patch.object(PLUGIN, "_LOG"),
        ):
            _run(handler, _body("card:3:delegate", REVIEW_VALUE), calls)
            assert not [c for c in calls if isinstance(c, tuple) and c[0] == "consumption"]
            record = next(c for c in calls if isinstance(c, tuple) and c[0] == "record")
            assert record[1] == "verdict_reviewed"
            assert record[2]["choice"] == "delegate" and "model unreachable" in record[2]["error"]
            assert "kind" not in record[2] and "reason" not in record[2]
            failed = _updates(calls)[-1][3]
            assert _status_at(failed, 3).startswith("✕ 실패")
            _assert_buttons_kept_under_the_reason(BLOCKS, failed, 3)
            _run(handler, _body("card:3:delegate", REVIEW_VALUE), calls)
    assert len([c for c in calls if isinstance(c, tuple) and c[0] == "model"]) == 2


def test_a_malformed_model_answer_is_a_failure_not_a_verdict():
    calls = []
    ctx = _FakeCtx(llm=_FakeLlm(calls, answer="맞는 것 같아요"))
    with _env():
        _, handler = _register(ctx)[0]
        with _patched_effects(calls), _patched_delegate(calls):
            _run(handler, _body("card:3:delegate", REVIEW_VALUE), calls)
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "consumption"]
    record = next(c for c in calls if isinstance(c, tuple) and c[0] == "record")
    assert "JSON" in record[2]["error"]


def test_a_delegate_press_without_the_model_seat_leaves_the_fact_only():
    """ctx.llm 이 없는 hermes에서도 누름은 살아 있다 — 판정 없이 사건 한 줄만 남긴다."""
    ctx = _FakeCtx(llm=None)
    with _env():
        _, handler = _register(ctx)[0]
        calls = []
        with _patched_effects(calls), _patched_delegate(calls):
            _run(handler, _body("card:3:delegate", REVIEW_VALUE), calls)
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "consumption"]
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "model"]
    record = next(c for c in calls if isinstance(c, tuple) and c[0] == "record")
    assert "ctx.llm" in record[2]["error"]


def test_a_delegate_press_with_an_unreadable_note_never_calls_the_model():
    calls = []
    ctx = _FakeCtx(llm=_FakeLlm(calls, answer='{"verdict": "right", "reason": "x"}'))
    with _env():
        _, handler = _register(ctx)[0]
        with _patched_delegate(calls, note_text=None), _patched_effects(calls):
            _run(handler, _body("card:3:delegate", REVIEW_VALUE), calls)
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "model"]
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "consumption"]
    record = next(c for c in calls if isinstance(c, tuple) and c[0] == "record")
    assert "못 읽었다" in record[2]["error"]


def test_an_owner_review_hold_press_leaves_no_edge_and_marks_the_row():
    """보류: no consumption at all — one verdict_reviewed 사건 carrying the held sessions,
    then the row settles as held. The 이레 re-raise is the read side's job (card_live)."""
    ctx = _FakeCtx()
    with _env():
        _, handler = _register(ctx)[0]
        calls = []
        with _patched_effects(calls):
            _run(handler, _body("card:3:defer", REVIEW_VALUE), calls)
    assert [c if isinstance(c, str) else c[0] for c in calls] == [
        "ack",
        "chat_update",
        "record",
        "chat_update",
    ]
    assert calls[2] == (
        "record",
        "verdict_reviewed",
        {
            "session_id": "sess-agent-1",
            "sessions": ["sess-agent-1"],
            "note": "/vault/wiki/wiki-0700.md",
            "proposed_kind": "used",
            "card_ts": CARD_TS,
            "choice": "defer",
        },
    )
    progress, final = _updates(calls)
    _assert_single_row_marked(BLOCKS, final[3], 3)
    assert "⏸ 보류 — 이레 뒤에 다시 올려요" in json.dumps(final[3], ensure_ascii=False)


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

    def boom(session, kind, paths, judge=None):
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

    def flaky(session, kind, paths, judge=None):
        if state["down"]:
            state["down"] = False
            raise RuntimeError("engine down")
        calls.append(("consumption", session, kind, paths, judge))

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
    assert ("consumption", "slack:C1:1.0", "used", ["/vault/wiki/wiki-0576.md"], None) in calls
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
                ("consumption", "slack:C1:1.0", "used", ["/vault/wiki/wiki-0576.md"], None),
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

    def door_settles_row_0(session, kind, paths, judge=None):
        client.current = card_view.mark_progress(
            client.current, 0, card_types.Done(text="✓ 완료 — door"), lang="ko"
        )
        calls.append(("consumption", session, kind, paths, judge))

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
    def down(session, kind, paths, judge=None):
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

    def flaky(session, kind, paths, judge=None):
        if state["down"]:
            state["down"] = False
            raise RuntimeError("engine down")

    card = _press_row_1_with(flaky, 2).current
    assert _status_at(card, 1) is None
    assert len(card) == len(BLOCKS)
    assert not any("✕ 실패" in json.dumps(b, ensure_ascii=False) for b in card)


def _bare_body(action_id, user=OWNER, blocks=BLOCKS):
    """A press on a button that carries no value — the card_row shape."""
    return {
        "type": "block_actions",
        "actions": [{"action_id": action_id}],
        "user": {"id": user},
        "message": {"ts": CARD_TS, "blocks": blocks},
        "channel": {"id": CARD_CH},
        "trigger_id": "trig-1",
    }


@contextlib.contextmanager
def _rows(entries=None, error=None):
    """card_row as the door's /events would answer it — or a dead door."""

    def fetch(name, hours):
        assert name == "card_row", name
        if error is not None:
            raise error
        return list(ROW_ENTRIES if entries is None else entries)

    with mock.patch.object(card_delegate, "fetch_events", side_effect=fetch):
        yield


def _records(calls):
    return [c for c in calls if isinstance(c, tuple) and c[0] == "record"]


def test_a_bare_button_press_reads_its_card_row_and_does_what_the_old_value_did():
    """The review row's buttons carry no value now: the plugin reads card_row by (card_ts, idx)
    and the press lands exactly as the same press on a button holding its value."""
    old, new = [], []
    with _env():
        for calls, body in ((old, _body("card:3:defer", REVIEW_VALUE)), (new, _bare_body("card:3:defer"))):
            _, handler = _register(_FakeCtx())[0]
            with _patched_effects(calls), _rows():
                _run(handler, body, calls)
    assert _records(new) and _records(new) == _records(old)
    (record,) = _records(new)
    assert record[1:] == (
        "verdict_reviewed",
        {
            "note": "/vault/wiki/wiki-0700.md",
            "proposed_kind": "used",
            "card_ts": CARD_TS,
            "session_id": "sess-agent-1",
            "sessions": ["sess-agent-1"],
            "choice": "defer",
        },
    )


def test_an_old_card_button_still_holding_its_value_is_pressed_without_reading_any_row():
    calls = []
    with _env():
        _, handler = _register(_FakeCtx())[0]
        with (
            _patched_effects(calls),
            mock.patch.object(
                card_delegate,
                "fetch_events",
                side_effect=AssertionError("no card_row read for an old button"),
            ),
        ):
            _run(handler, _body("card:3:defer", REVIEW_VALUE), calls)
    assert len(_records(calls)) == 1


def test_a_card_row_that_cannot_be_read_shows_the_failure_keeps_the_buttons_and_does_not_take_the_press():
    """Nothing in the row's key reaches an effect on a guess: no card_row (none written, wrong
    idx), or no way to read it (dead door), is 「✕ 실패 — 줄 정보를 못 읽었어요」 over the row's
    buttons, and the next press — once the row can be read — goes through."""
    for rows in (
        _rows(entries=[]),
        _rows(entries=[e for e in ROW_ENTRIES if e["attributes"]["idx"] != 3]),
        _rows(error=OSError("door down")),
    ):
        calls = []
        with _env():
            _, handler = _register(_FakeCtx())[0]
            with _patched_effects(calls), mock.patch.object(PLUGIN, "_LOG"):
                with rows:
                    client = _run(handler, _bare_body("card:3:defer"), calls)
                assert not _records(calls) and not [c for c in calls if c[0] == "consumption"]
                (update,) = _updates(calls)
                assert _status_at(update[3], 3) == "✕ 실패 — 줄 정보를 못 읽었어요"
                _assert_buttons_kept_under_the_reason(BLOCKS, update[3], 3)
                assert (CARD_CH, CARD_TS) == (update[1], update[2])
                assert not PLUGIN._claimed
                with _rows():
                    _run(handler, _bare_body("card:3:defer", blocks=client.current), calls)
                assert len(_records(calls)) == 1


def test_the_comment_button_opens_the_modal_with_the_rows_key_and_runs_no_effect():
    calls = []
    with _env():
        _, handler = _register(_FakeCtx())[0]
        with _patched_effects(calls):
            _run(handler, _bare_body("card:3:comment"), calls)
    assert [c if isinstance(c, str) else c[0] for c in calls] == ["ack", "views_open"]
    _, trigger_id, view = calls[1]
    assert trigger_id == "trig-1"
    assert view["callback_id"] == "card:comment"
    assert json.loads(view["private_metadata"]) == {"card_ts": CARD_TS, "channel": CARD_CH, "idx": 3}
    assert not PLUGIN._claimed


def test_a_comment_press_by_anyone_but_the_owner_opens_nothing():
    calls = []
    with _env():
        _, handler = _register(_FakeCtx())[0]
        with _patched_effects(calls):
            _run(handler, _bare_body("card:3:comment", user=OTHER), calls)
    assert calls == ["ack"]


def test_a_modal_that_will_not_open_shows_the_failure_over_the_buttons():
    calls = []
    with _env():
        _, handler = _register(_FakeCtx())[0]
        with _patched_effects(calls), mock.patch.object(PLUGIN, "_LOG"):
            _run(handler, _bare_body("card:3:comment"), calls, open_fail=True)
    (update,) = _updates(calls)
    assert _status_at(update[3], 3) == "✕ 실패 — 코멘트 창을 못 열었어요"
    _assert_buttons_kept_under_the_reason(BLOCKS, update[3], 3)


def _submit_body(text, user=OWNER, idx=3, card_ts=CARD_TS):
    return {
        "type": "view_submission",
        "user": {"id": user},
        "view": {
            "callback_id": "card:comment",
            "private_metadata": json.dumps({"card_ts": card_ts, "channel": CARD_CH, "idx": idx}),
            "state": {"values": {"comment": {"text": {"type": "plain_text_input", "value": text}}}},
        },
    }


def _submit(body, calls, *, rows=None, effects_calls=None, **effects_kw):
    """register() the plugin, let it wire its modal onto a fake bolt app the way hermes's
    adapter does, and submit `body` to the handler it put there."""
    ctx = _FakeCtx()
    with _env():
        _register(ctx)
        assert [p for p, _ in ctx.platform_factories] == ["slack"]
        app = _FakeApp()
        ctx.platform_factories[0][1](app, None)
        view_handler = app.views["card:comment"]

        async def ack():
            calls.append("ack")

        client = _FakeClient(calls)
        with (
            mock.patch.object(PLUGIN, "_client", client),
            mock.patch.object(PLUGIN, "_LOG"),
            _patched_effects(calls, **effects_kw),
            rows or _rows(),
        ):
            asyncio.run(view_handler(ack, body))
    return client


def test_a_submitted_comment_becomes_an_owner_event_and_the_row_says_it_got_it():
    """The whole path from the modal: ack, the row read back by the modal's key, one card_comment
    사건 with judge=owner and the row's session/note, then 「💬 받았어요」 over the row, buttons
    kept. A mutant that skips the 받았어요 or the event goes red here."""
    calls = []
    _submit(_submit_body("이 노트는 지금도 맞는 말이에요\n다음 판정에서 참고해 주세요"), calls)
    assert calls[0] == "ack"
    (record,) = _records(calls)
    assert record[1] == "card_comment"
    assert record[2] == {
        "judge": "owner",
        "card_ts": CARD_TS,
        "idx": 3,
        "text": "이 노트는 지금도 맞는 말이에요\n다음 판정에서 참고해 주세요",
        "lane": "review",
        "session_id": "sess-agent-1",
        "sessions": ["sess-agent-1"],
        "note": "/vault/wiki/wiki-0700.md",
        "proposed_kind": "used",
    }
    (update,) = _updates(calls)
    assert _status_at(update[3], 3) == "💬 받았어요 — 이 노트는 지금도 맞는 말이에요 다음 판정에서 참고해…"
    _assert_buttons_kept_under_the_reason(BLOCKS, update[3], 3)
    assert not [c for c in calls if isinstance(c, tuple) and c[0] in ("consumption", "repair")]


def test_a_comment_on_a_name_repair_row_names_its_subject_and_spellings():
    calls = []
    _submit(_submit_body("이건 합치면 안 돼요", idx=0), calls)
    (record,) = _records(calls)
    assert record[2]["lane"] == "repair"
    assert (record[2]["subject"], record[2]["variants"]) == (
        "foodspring-front",
        ["foodspring front", "foodspring-front"],
    )
    assert record[2]["judge"] == "owner"
    (update,) = _updates(calls)
    assert _status_at(update[3], 0) == "💬 받았어요 — 이건 합치면 안 돼요"


def test_a_submission_whose_card_row_cannot_be_read_records_nothing_and_says_so_on_the_row():
    calls = []
    _submit(_submit_body("글"), calls, rows=_rows(error=OSError("door down")))
    assert not _records(calls)
    (update,) = _updates(calls)
    assert _status_at(update[3], 3) == "✕ 실패 — 줄 정보를 못 읽었어요"
    _assert_buttons_kept_under_the_reason(BLOCKS, update[3], 3)
    calls = []
    _submit(_submit_body("글"), calls, rows=_rows(entries=[]))
    assert not _records(calls) and "줄 정보를 못 읽었어요" in _status_at(_updates(calls)[0][3], 3)


def test_a_comment_that_could_not_be_stored_is_never_shown_as_received():
    calls = []

    def boom(event, fields):
        raise RuntimeError("event sink down")

    with mock.patch.object(card_effects, "_live_record", side_effect=boom):
        ctx = _FakeCtx()
        with _env():
            _register(ctx)
            app = _FakeApp()
            ctx.platform_factories[0][1](app, None)

            async def ack():
                calls.append("ack")

            client = _FakeClient(calls)
            with mock.patch.object(PLUGIN, "_client", client), mock.patch.object(PLUGIN, "_LOG"), _rows():
                asyncio.run(app.views["card:comment"](ack, _submit_body("글")))
    (update,) = _updates(calls)
    assert _status_at(update[3], 3) == "✕ 실패 — event sink down"
    assert "받았어요" not in json.dumps(update[3], ensure_ascii=False)


def test_a_submission_nobody_may_make_runs_nothing():
    for body in (_submit_body("글", user=OTHER), _submit_body("   ")):
        calls = []
        _submit(body, calls)
        assert calls == ["ack"], calls


def test_a_submission_on_a_card_past_its_answerable_hours_runs_nothing():
    calls = []
    PLUGIN.time = types.SimpleNamespace(time=lambda: float(CARD_TS) + 24 * 3600)
    try:
        _submit(_submit_body("글"), calls)
    finally:
        PLUGIN.time = types.SimpleNamespace(time=lambda: float(CARD_TS) + 60)
    assert calls == ["ack"], calls


def test_a_delegate_press_reads_the_owners_comments_into_its_prompt_and_names_their_ids():
    """맡길게요 reads what the owner said about the note: the comment is in the one model call's
    prompt, and the verdict_reviewed 사건 names the 사건 id it was given. A judge that never
    reads the comments goes red here."""
    calls = []
    comments = [
        card_types.OwnerComment(id=31, at="2026-10-06T02:00:00+00:00", text="이 노트는 옛 접근이라 틀렸어요"),
    ]
    ctx = _FakeCtx(llm=_FakeLlm(calls, answer='{"verdict": "wrong", "reason": "오너가 옛 접근이라 했다"}'))
    with _env():
        _, handler = _register(ctx)[0]
        with _patched_effects(calls), _patched_delegate(calls, comments=comments):
            _run(handler, _body("card:3:delegate", REVIEW_VALUE), calls)
    (model,) = [c for c in calls if isinstance(c, tuple) and c[0] == "model"]
    assert "소유자가 이 노트에 직접 남긴 말" in model[1]
    assert "- 이 노트는 옛 접근이라 틀렸어요" in model[1]
    (record,) = _records(calls)
    assert record[2]["comment_ids"] == [31]
    assert record[2]["judge"] == "agent:delegated"


def test_a_delegate_press_that_cannot_read_the_owners_comments_does_not_judge_as_if_they_were_silent():
    calls = []
    ctx = _FakeCtx(llm=_FakeLlm(calls, answer='{"verdict": "right", "reason": "x"}'))
    with _env():
        _, handler = _register(ctx)[0]
        with (
            _patched_effects(calls),
            _patched_delegate(calls, comments=OSError("events unreadable")),
            mock.patch.object(PLUGIN, "_LOG"),
        ):
            _run(handler, _body("card:3:delegate", REVIEW_VALUE), calls)
    assert not [c for c in calls if isinstance(c, tuple) and c[0] in ("model", "consumption")]
    (record,) = _records(calls)
    assert "오너 코멘트를 못 읽었다" in record[2]["error"]
    assert "✕ 실패" in json.dumps(_updates(calls)[-1][3], ensure_ascii=False)


#: A card that left rows out of every lane (repairs 0-1, advice 2-4, reviews 5-7, samples 8-9):
#: the second repair, the last two advice rows, the third review and the second sample stay in
#: card_row for the [더보기] pages.
_MORE_PROPOSALS = [
    *_PROPOSALS,
    _PROPOSALS[0].model_copy(update={"subject": "주어 셋", "note": "/vault/wiki/wiki-0950.md"}),
]
_MORE_REVIEWS = [
    card_types.ProposedVerdict(
        session_id=f"sess-agent-{i}", note=f"/vault/wiki/wiki-07{i:02d}.md", kind="used", at=f"t-{i}"
    )
    for i in range(3)
]
_MORE_SAMPLES = [
    card_types.ProposedVerdict(
        session_id=f"sx{i}", note=f"/vault/wiki/wiki-08{i:02d}.md", kind="used", at=f"u-{i}"
    )
    for i in range(2)
]
_MORE_BUILT = card_view.build_card(
    _MORE_PROPOSALS,
    repairs=[_REPAIR, _REPAIR.model_copy(update={"subject": "other-subject", "variants": ["o s", "o-s"]})],
    repairs_total_groups=2,
    reviews=_MORE_REVIEWS,
    samples=_MORE_SAMPLES[:1],
    samples_hidden=_MORE_SAMPLES[1:],
    caps=card_view.RowCaps(repairs=1, advice=1, reviews=2),
    lang="ko",
)
MORE_BLOCKS = _MORE_BUILT.blocks


def _as_entries(rows, card_ts=CARD_TS):
    """card_row rows as /events returns them."""
    return [
        {"id": 500 + i, "observed_at": "2026-10-06T00:00:00+00:00", "attributes": row.fields(card_ts)}
        for i, row in enumerate(rows)
    ]


def _more_body(lane, blocks=MORE_BLOCKS, user=OWNER):
    return {
        "type": "block_actions",
        "actions": [{"action_id": f"card:more:{lane}"}],
        "user": {"id": user},
        "message": {"ts": CARD_TS, "blocks": blocks},
        "channel": {"id": CARD_CH},
    }


@contextlib.contextmanager
def _more_events(rows=None, sent=(), error=None):
    """card_row and card_more as the door's /events would answer them — or a dead door."""

    def fetch(name, hours):
        if error is not None:
            raise error
        return (
            list(_as_entries(_MORE_BUILT.rows) if rows is None else rows)
            if name == "card_row"
            else list(sent)
        )

    with mock.patch.object(card_delegate, "fetch_events", side_effect=fetch):
        yield


def _posts(calls):
    return [c[1] for c in calls if isinstance(c, tuple) and c[0] == "post"]


def _more_press(lane, calls, *, blocks=MORE_BLOCKS, ctx=None, **run_kw):
    with _env():
        _, handler = _register(ctx or _FakeCtx())[0]
        with _patched_effects(calls), mock.patch.object(PLUGIN, "_LOG"):
            return _run(handler, _more_body(lane, blocks), calls, current=blocks, **run_kw)


def _page_entries(calls):
    return [
        {"id": 700 + i, "observed_at": "2026-10-06T01:00:00+00:00", "attributes": c[2]}
        for i, c in enumerate(_records(calls))
        if c[1] == "card_row"
    ]


def test_a_more_press_sends_one_new_message_in_the_same_dm_never_a_thread_and_marks_that_line():
    calls = []
    llm_calls = []
    with _more_events():
        _more_press("advice", calls, ctx=_FakeCtx(llm=_FakeLlm(llm_calls, answer="{}")))
    (post,) = _posts(calls)
    assert post["channel"] == CARD_CH
    assert "thread_ts" not in post and "reply_broadcast" not in post
    buttons = [el for b in post["blocks"] if b["type"] == "actions" for el in b["elements"]]
    assert {el["action_id"] for el in buttons} == {
        f"card:{i}:{c}" for i in (3, 4) for c in ("do", "defer", "drop")
    }
    (update,) = _updates(calls)
    assert (update[1], update[2]) == (CARD_CH, CARD_TS)
    assert [i for i, (a, b) in enumerate(zip(MORE_BLOCKS, update[3])) if a != b] == [
        next(i for i, b in enumerate(MORE_BLOCKS) if b.get("block_id") == "card:more:advice")
    ]
    sent = next(b for b in update[3] if b.get("block_id") == "card:more:advice")
    assert sent["elements"][0]["text"] == "↓ 이어서 보냈어요 (2건)"
    assert len(update[3]) == len(MORE_BLOCKS)
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "model"] and not llm_calls


def test_a_more_press_records_the_page_then_the_proposals_it_showed_and_hands_over_their_notes():
    calls = []
    with _more_events():
        _more_press("advice", calls)
    names = [c[1] if c[0] == "record" else c[0] for c in calls if isinstance(c, tuple)]
    assert names == ["post", "card_more", "handover", "card_proposal", "card_proposal", "chat_update"]
    more = next(c for c in _records(calls) if c[1] == "card_more")
    assert more[2] == {"card_ts": CARD_TS, "lane": "advice", "ts": PAGE_TS, "n": 2}
    proposals = [c[2] for c in _records(calls) if c[1] == "card_proposal"]
    assert [(p["card_ts"], p["idx"], p["more_of"]) for p in proposals] == [
        (PAGE_TS, 3, CARD_TS),
        (PAGE_TS, 4, CARD_TS),
    ]
    (handover,) = [c for c in calls if isinstance(c, tuple) and c[0] == "handover"]
    assert handover[1] == f"slack:{CARD_CH}:{PAGE_TS}"
    assert "/vault/wiki/wiki-0900.md" in handover[2]


def test_a_second_more_press_on_the_same_lane_sends_nothing_more():
    calls = []
    with _more_events():
        with _env():
            _, handler = _register(_FakeCtx())[0]
            with _patched_effects(calls), mock.patch.object(PLUGIN, "_LOG"):
                _run(handler, _more_body("advice"), calls, current=MORE_BLOCKS)
                _run(handler, _more_body("advice"), calls, current=MORE_BLOCKS)  # a press from the stale card
    assert len(_posts(calls)) == 1
    assert len(_updates(calls)) == 1
    # after a restart the claim is gone; the lane's card_more 사건 still answers
    sent = [
        {
            "id": 9,
            "observed_at": "2026-10-06T00:00:00+00:00",
            "attributes": next(c[2] for c in _records(calls) if c[1] == "card_more"),
        }
    ]
    again = []
    with _more_events(sent=sent):
        _more_press("advice", again)
    assert not _posts(again) and not _records(again)
    (update,) = _updates(again)
    assert any(b.get("block_id") == "card:more:advice" and b["type"] == "context" for b in update[3])
    assert "(2건)" in json.dumps(update[3], ensure_ascii=False)


def test_a_more_press_for_each_lane_sends_that_lanes_rows_and_only_those():
    for lane, idxs in (("repair", {1}), ("review", {7}), ("sample", {9})):
        calls = []
        with _more_events():
            _more_press(lane, calls)
        (post,) = _posts(calls)
        buttons = [el["action_id"] for b in post["blocks"] if b["type"] == "actions" for el in b["elements"]]
        assert {int(a.split(":")[1]) for a in buttons} == idxs, lane


def test_a_page_rows_buttons_comment_and_press_work_as_on_the_first_card():
    calls = []
    with _more_events():
        _more_press("review", calls)
    (post,) = _posts(calls)
    page = post["blocks"]
    page_rows = _page_entries(calls)
    assert [
        (e["attributes"]["idx"], e["attributes"]["card_ts"], e["attributes"]["shown"]) for e in page_rows
    ] == [(7, PAGE_TS, True)]

    def on_page(body):
        return {**body, "message": {"ts": PAGE_TS, "blocks": page}}

    # the 맞아요/보류 press reads the page's own card_row and lands as the first card's would
    press_calls = []
    with _env():
        _, handler = _register(_FakeCtx())[0]
        with _patched_effects(press_calls), _rows(entries=page_rows):
            _run(handler, on_page(_bare_body("card:7:defer")), press_calls, current=page)
    (record,) = _records(press_calls)
    assert record[1] == "verdict_reviewed"
    assert record[2]["card_ts"] == PAGE_TS and record[2]["session_id"] == "sess-agent-2"
    # the 💬 코멘트 opens the modal keyed to the page
    comment_calls = []
    with _env():
        _, handler = _register(_FakeCtx())[0]
        with _patched_effects(comment_calls):
            _run(handler, on_page(_bare_body("card:7:comment")), comment_calls, current=page)
    (opened,) = [c for c in comment_calls if isinstance(c, tuple) and c[0] == "views_open"]
    assert json.loads(opened[2]["private_metadata"]) == {"card_ts": PAGE_TS, "channel": CARD_CH, "idx": 7}


def test_an_advice_page_row_is_judged_against_the_pages_own_session():
    calls = []
    with _more_events():
        _more_press("advice", calls)
    (post,) = _posts(calls)
    value = next(
        el["value"]
        for b in post["blocks"]
        if b["type"] == "actions"
        for el in b["elements"]
        if el["action_id"] == "card:3:do"
    )
    press_calls = []
    body = {**_body("card:3:do", json.loads(value)), "message": {"ts": PAGE_TS, "blocks": post["blocks"]}}
    with _env():
        _, handler = _register(_FakeCtx())[0]
        with _patched_effects(press_calls):
            _run(handler, body, press_calls, current=post["blocks"])
    verdict = next(c for c in _records(press_calls) if c[1] == "card_verdict")
    assert verdict[2] == {"card_ts": PAGE_TS, "idx": 3, "choice": "do"}
    (consumption,) = [c for c in press_calls if isinstance(c, tuple) and c[0] == "consumption"]
    assert consumption[1] == f"slack:{CARD_CH}:{PAGE_TS}"


def test_a_page_that_still_overflows_carries_its_own_more_button_and_keeps_the_rest_in_card_row():
    left = card_view.page_rows(
        "advice",
        [(r.idx, r.detail) for r in _MORE_BUILT.rows if not r.shown and r.lane == "advice"],
    )
    two_rows = card_view.card_chars(card_view.build_page("advice", left, lang="ko", cap=1).blocks)
    calls = []
    with _more_events(), mock.patch.object(card_view, "CARD_CHARS_BUDGET", two_rows):
        _more_press("advice", calls)
    (post,) = _posts(calls)
    (line,) = [b for b in post["blocks"] if "accessory" in b]
    assert line["accessory"]["action_id"] == "card:more:advice" and "+1건" in line["text"]["text"]
    rows = [e["attributes"] for e in _page_entries(calls)]
    assert [(r["idx"], r["card_ts"], r["shown"]) for r in rows] == [(4, PAGE_TS, False)]
    assert next(c for c in _records(calls) if c[1] == "card_more")[2]["n"] == 1
    assert "(1건)" in json.dumps(_updates(calls)[-1][3], ensure_ascii=False)


def test_a_page_the_card_left_unopened_leaves_nothing_that_says_it_was_seen():
    calls = []
    with _more_events():
        _more_press("review", calls)
    assert [c[1] for c in _records(calls)] == ["card_more", "card_row"]
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "handover"]


def test_a_sample_page_records_the_sample_as_shown_on_the_new_message():
    calls = []
    with _more_events():
        _more_press("sample", calls)
    shown = [c[2] for c in _records(calls) if c[1] == "verdict_sample_shown"]
    assert [(s["session_id"], s["card_ts"]) for s in shown] == [("sx1", PAGE_TS)]


def test_a_more_press_whose_rows_cannot_be_read_shows_the_failure_over_the_button_and_can_be_pressed_again():
    for events in (_more_events(rows=[]), _more_events(error=OSError("door down"))):
        calls = []
        with events:
            client = _more_press("advice", calls)
        assert not _posts(calls) and not _records(calls)
        (update,) = _updates(calls)
        status = next(b for b in update[3] if b.get("block_id") == "card:more:advice:status")
        assert status["elements"][0]["text"] == "✕ 실패 — 줄 정보를 못 읽었어요"
        assert any(b.get("accessory", {}).get("action_id") == "card:more:advice" for b in update[3])
        assert not PLUGIN._more_claimed
        again = []
        with _more_events(), _env():
            _, handler = _register(_FakeCtx())[0]
            with _patched_effects(again), mock.patch.object(PLUGIN, "_LOG"):
                _run(handler, _more_body("advice", client.current), again, current=client.current)
        assert len(_posts(again)) == 1


def test_a_page_that_slack_refuses_records_nothing_and_leaves_the_button_pressable():
    calls = []
    with _more_events():
        _more_press("advice", calls, post_fail=True)
    assert not _records(calls) and not PLUGIN._more_claimed
    (update,) = _updates(calls)
    assert "✕ 실패 — slack down" in json.dumps(update[3], ensure_ascii=False)
    assert any(b.get("accessory", {}).get("action_id") == "card:more:advice" for b in update[3])


def test_a_more_press_by_anyone_but_the_owner_or_on_an_old_card_does_nothing():
    calls = []
    with _env():
        _, handler = _register(_FakeCtx())[0]
        with (
            _patched_effects(calls),
            mock.patch.object(card_delegate, "fetch_events", side_effect=AssertionError("no read")),
        ):
            _run(handler, _more_body("advice", user=OTHER), calls)
            with mock.patch.object(
                PLUGIN, "time", types.SimpleNamespace(time=lambda: float(CARD_TS) + 24 * 3600)
            ):
                _run(handler, _more_body("advice"), calls)
    assert not _posts(calls) and not _records(calls) and not _updates(calls)


if __name__ == "__main__":
    test_a_more_press_sends_one_new_message_in_the_same_dm_never_a_thread_and_marks_that_line()
    test_a_more_press_records_the_page_then_the_proposals_it_showed_and_hands_over_their_notes()
    test_a_second_more_press_on_the_same_lane_sends_nothing_more()
    test_a_more_press_for_each_lane_sends_that_lanes_rows_and_only_those()
    test_a_page_rows_buttons_comment_and_press_work_as_on_the_first_card()
    test_an_advice_page_row_is_judged_against_the_pages_own_session()
    test_a_page_that_still_overflows_carries_its_own_more_button_and_keeps_the_rest_in_card_row()
    test_a_page_the_card_left_unopened_leaves_nothing_that_says_it_was_seen()
    test_a_sample_page_records_the_sample_as_shown_on_the_new_message()
    test_a_more_press_whose_rows_cannot_be_read_shows_the_failure_over_the_button_and_can_be_pressed_again()
    test_a_page_that_slack_refuses_records_nothing_and_leaves_the_button_pressable()
    test_a_more_press_by_anyone_but_the_owner_or_on_an_old_card_does_nothing()
    test_the_import_path_stays_langchain_free()
    test_register_without_secretary_owner_id_registers_nothing()
    test_register_without_boring_home_registers_nothing()
    test_register_hands_one_handler_the_card_action_ids()
    test_the_handler_acks_before_any_effect()
    test_a_press_on_a_card_older_than_the_answerable_hours_runs_no_effect()
    test_an_owner_advice_do_press_records_then_consumes_and_marks_the_card()
    test_an_owner_review_delegate_press_consumes_with_the_agent_judge()
    test_a_delegate_press_where_the_model_agrees_keeps_the_proposed_kind()
    test_a_grouped_delegate_press_calls_the_model_once_and_fans_out_the_judgment()
    test_a_model_failure_leaves_no_edge_and_records_the_fact()
    test_a_malformed_model_answer_is_a_failure_not_a_verdict()
    test_a_delegate_press_without_the_model_seat_leaves_the_fact_only()
    test_a_delegate_press_with_an_unreadable_note_never_calls_the_model()
    test_an_owner_review_hold_press_leaves_no_edge_and_marks_the_row()
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
    test_a_bare_button_press_reads_its_card_row_and_does_what_the_old_value_did()
    test_an_old_card_button_still_holding_its_value_is_pressed_without_reading_any_row()
    test_a_card_row_that_cannot_be_read_shows_the_failure_keeps_the_buttons_and_does_not_take_the_press()
    test_the_comment_button_opens_the_modal_with_the_rows_key_and_runs_no_effect()
    test_a_comment_press_by_anyone_but_the_owner_opens_nothing()
    test_a_modal_that_will_not_open_shows_the_failure_over_the_buttons()
    test_a_submitted_comment_becomes_an_owner_event_and_the_row_says_it_got_it()
    test_a_comment_on_a_name_repair_row_names_its_subject_and_spellings()
    test_a_submission_whose_card_row_cannot_be_read_records_nothing_and_says_so_on_the_row()
    test_a_comment_that_could_not_be_stored_is_never_shown_as_received()
    test_a_submission_nobody_may_make_runs_nothing()
    test_a_submission_on_a_card_past_its_answerable_hours_runs_nothing()
    test_a_delegate_press_reads_the_owners_comments_into_its_prompt_and_names_their_ids()
    test_a_delegate_press_that_cannot_read_the_owners_comments_does_not_judge_as_if_they_were_silent()
    print("ok - boring-card plugin: ack-first, in-place mark, double-press refused, loud refusals")
