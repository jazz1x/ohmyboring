#!/usr/bin/env python3
"""Regression tests for the boring-memory hermes plugin.

Run: python3 agents/hermes/plugins/boring-memory/test_boring_memory.py   (no pytest dependency)

The plugin hands hermes the owner's own memory before it answers a slack turn: one block
per `wiki-NNNN` token the message names (read from the vault through vault_note's
splitter, missing notes said out loud) and a recall block built from recall_core's engine
search and formatting, handed over to the hermes session. These tests pin the contract:
  - a named note that exists lands with its title, date, and the head of its body
  - a named note that does not exist is stated as such (「은 볼트에 없음」) — never silently
    skipped, because "I don't know that note" is the failure the plugin exists to kill
  - the recall block reuses recall_core's shapes (snippet line, claim line, consumption
    note) and hands the injected paths to the hermes session id on the engine
  - a dead engine raises inside the hook and comes home as no context at all, one error
    line — the turn itself still runs
  - a non-slack platform gets no context and no log line
  - register() without BORING_HOME registers nothing
  - the import path stays langchain-free, because the hermes venv has no langchain

Mutation targets: deleting the named-note lookup kills the present-note test; swallowing
the missing-note case silently (returning "" instead of the 없음 line, or skipping the
token) kills the missing-note test; dropping the handover kills the handover test;
raising through the engine-down test kills the survive test.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
for extra in (REPO / "agents" / "shared",):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import recall_core  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("boring_memory_plugin", HERE / "__init__.py")
PLUGIN = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(PLUGIN)

SESSION = "slack:C9:1727496000.000100"

#: A note with the shape the real vault compiles: quoted title, scalar date, a body.
_NOTE = """---
id: wiki-2121
title: '카드·주간 브리핑을 hermes 가 실행'
kind: note
origin: personal
date: 2026-09-28
---

첫째 문단 — 카드와 주간 브리핑을 hermes 가 실행하게 했다. 문에 /run/morning-card 가 생겼고
launchd 등록은 배달하는 그 자리에서 지웠다.

둘째 문단 — 둘째 주자가 두 번 올라가지 못하게 카드는 오늘 자기 사건을 읽는다.
"""

#: One engine hit with everything recall_core renders: a snippet over the salient cut, a
#: consumption count, a claim row, and a vector distance under the ceiling.
_HIT = {
    "source_path": "/vault/wiki/wiki-1234.md",
    "snippet": "배경 " * 120 + "## 결정 노드 완료 = 커밋 수 완료 통지가 곧 엣지",
    "dist": 0.4123,
    "dist_kind": "vector_cosine",
    "used_count": 2,
    "contested_count": 0,
    "claims": [
        {
            "kind": "decision",
            "subject": "morning-card-runner",
            "predicate": "status",
            "value": "hermes cron 이 문을 통해 카드를 실행",
        }
    ],
    "claims_total": 1,
    "related": [],
}


class _FakeCtx:
    def __init__(self):
        self.hooks = []

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))


class _FakeDrudgeClient:
    """Stands in for recall_core.DrudgeClient; the class attributes steer every instance."""

    hits = []
    down = False
    instances = []

    def __init__(self, **kwargs):
        self.calls = []
        _FakeDrudgeClient.instances.append(self)

    def search(self, prompt, knobs=None):
        self.calls.append(("search", prompt, knobs))
        if _FakeDrudgeClient.down:
            raise ConnectionError("engine unreachable")
        return list(_FakeDrudgeClient.hits)

    def handover(self, session_id, observed_at, paths):
        self.calls.append(("handover", session_id, list(paths)))
        return {"ok": True}


def _vault(tmp: str) -> Path:
    root = Path(tmp) / "vault"
    (root / "wiki").mkdir(parents=True)
    (root / "wiki" / "wiki-2121.md").write_text(_NOTE, encoding="utf-8")
    return root


def _env(vault_dir: str, boring_home: str = str(REPO)):
    """The container's environment — BORING_HOME for the repo modules, BORING_VAULT_DIR at a
    fixture vault, the injection ledger redirected into the void — held around register()
    AND the hook calls, because the hook reads the vault dir per turn."""
    return mock.patch.dict(
        os.environ,
        {
            "BORING_HOME": boring_home,
            "BORING_VAULT_DIR": vault_dir,
            "BORING_INJECTION_LEDGER": str(Path(vault_dir).parent / "ledger.jsonl"),
        },
        clear=True,
    )


def _register(ctx: _FakeCtx, *, vault_dir: str):
    """register() the way the container runs it, under the fixture env."""
    with _env(vault_dir):
        PLUGIN.register(ctx)
    return ctx.hooks


def _hook(
    ctx: _FakeCtx, *, vault_dir: str, platform="slack", message="wiki-2121 변경되었나요 ?", session=SESSION
):
    """Invoke the registered hook under the fixture env — the hook reads BORING_VAULT_DIR
    per turn, so the env must be live at call time, not only at register()."""
    assert len(ctx.hooks) == 1
    name, hook = ctx.hooks[0]
    assert name == "pre_llm_call"
    with _env(vault_dir):
        return hook(platform=platform, user_message=message, session_id=session)


def test_the_import_path_stays_langchain_free():
    """Everything register() imports must stay importable in the hermes venv — no langchain
    anywhere on this path, the way boring-card's test pins its own imports."""
    import uptake_core  # noqa: F401
    import vault_note  # noqa: F401

    assert "langchain" not in sys.modules
    leaked = sorted(m for m in sys.modules if m.startswith(("langchain", "langgraph")))
    assert not leaked, f"the hermes venv has no langchain — the plugin's imports leaked: {leaked}"


def test_register_without_boring_home_registers_nothing():
    ctx = _FakeCtx()
    with (
        mock.patch.dict(os.environ, {}, clear=True),
        mock.patch.object(PLUGIN, "_LOG") as log,
    ):
        PLUGIN.register(ctx)
    assert ctx.hooks == []
    assert log.error.called


def test_a_named_note_lands_with_title_date_and_body():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeCtx()
        vault_dir = str(_vault(tmp))
        _register(ctx, vault_dir=vault_dir)
        with mock.patch.object(recall_core, "DrudgeClient", _FakeDrudgeClient):
            out = _hook(ctx, vault_dir=vault_dir)
    context = out["context"]
    assert context.startswith(PLUGIN._HEADER)
    assert "노트 wiki-2121 — 카드·주간 브리핑을 hermes 가 실행" in context
    assert "날짜: 2026-09-28" in context
    assert "첫째 문단 — 카드와 주간 브리핑을 hermes 가 실행하게 했다." in context


def test_a_named_note_that_does_not_exist_is_stated_not_skipped():
    """The owner names a note the vault lacks: the context says so in words — hermes must
    never again answer "no file found" for a note its own card cited."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeCtx()
        vault_dir = str(_vault(tmp))
        _register(ctx, vault_dir=vault_dir)
        with mock.patch.object(recall_core, "DrudgeClient", _FakeDrudgeClient):
            out = _hook(ctx, vault_dir=vault_dir, message="wiki-9999 바뀌었나 ?")
    assert "wiki-9999 은 볼트에 없음" in out["context"]


def test_the_recall_block_reuses_recall_core_shapes_and_hands_over():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeCtx()
        vault_dir = str(_vault(tmp))
        _register(ctx, vault_dir=vault_dir)
        _FakeDrudgeClient.hits = [_HIT]
        try:
            with mock.patch.object(recall_core, "DrudgeClient", _FakeDrudgeClient):
                out = _hook(ctx, vault_dir=vault_dir)
        finally:
            _FakeDrudgeClient.hits = []
    context = out["context"]
    # recall_core's own line shapes, not a re-implementation
    assert "[wiki-1234.md]" in context
    assert "reused 2×" in context
    assert "· [decision] morning-card-runner status: hermes cron 이 문을 통해 카드를 실행" in context
    assert recall_core.FENCE.splitlines()[0] in context
    # handed to THIS hermes session so the engine records what it handed
    handovers = [c for c in _FakeDrudgeClient.instances[-1].calls if c[0] == "handover"]
    assert handovers, "the injected notes must be handed over to the hermes session"
    _, session, paths = handovers[0]
    assert session == SESSION
    assert paths == ["/vault/wiki/wiki-1234.md"]


def test_an_engine_down_returns_no_context_and_one_log_line():
    """The engine raising must not cost the owner the turn: no context at all, one error
    line naming the plugin. The named-note lookup already happened — it is dropped too,
    because a half context is worse than none."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeCtx()
        vault_dir = str(_vault(tmp))
        _register(ctx, vault_dir=vault_dir)
        _FakeDrudgeClient.down = True
        try:
            with (
                mock.patch.object(recall_core, "DrudgeClient", _FakeDrudgeClient),
                mock.patch.object(PLUGIN, "_LOG") as log,
            ):
                out = _hook(ctx, vault_dir=vault_dir)
        finally:
            _FakeDrudgeClient.down = False
    assert out is None
    log.error.assert_called_once()
    assert PLUGIN._PLUGIN_NAME in str(log.error.call_args)


def test_a_non_slack_platform_gets_no_context():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeCtx()
        vault_dir = str(_vault(tmp))
        _register(ctx, vault_dir=vault_dir)
        with mock.patch.object(PLUGIN, "_LOG") as log:
            out = _hook(ctx, vault_dir=vault_dir, platform="cli")
    assert out is None
    assert not log.error.called, "silence on other platforms is the specified behaviour"


def test_a_message_with_nothing_to_recall_gets_no_context():
    """No wiki token and a prompt under recall_core's floor: nothing to hand over, so no
    context block at all — the prefix line alone would be noise on every turn."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeCtx()
        vault_dir = str(_vault(tmp))
        _register(ctx, vault_dir=vault_dir)
        _FakeDrudgeClient.instances.clear()
        with mock.patch.object(recall_core, "DrudgeClient", _FakeDrudgeClient):
            out = _hook(ctx, vault_dir=vault_dir, message="ㅋㅋ")
    assert out is None
    assert not _FakeDrudgeClient.instances, "an unrecallable prompt must never reach the engine"


def test_the_plugin_keeps_no_renderer_of_its_own():
    """한 벌 원칙 — 번호 찾기와 노트 블록 렌더는 ohmyboring.recall.named 에만 있다(문도 같은
    모듈을 쓴다). 플러그인에 옛 사본(_WIKI_TOKEN·_note_block·_frontmatter_value)이 남으면
    두 벌이 어긋나기 시작하니까 실패로 못 박는다."""
    for name in ("_WIKI_TOKEN", "_note_block", "_frontmatter_value", "_FRONTMATTER_FIELD", "_BODY_CHARS"):
        assert not hasattr(PLUGIN, name), f"plugin must not keep its own copy: {name}"


if __name__ == "__main__":
    test_the_import_path_stays_langchain_free()
    test_register_without_boring_home_registers_nothing()
    test_a_named_note_lands_with_title_date_and_body()
    test_a_named_note_that_does_not_exist_is_stated_not_skipped()
    test_the_recall_block_reuses_recall_core_shapes_and_hands_over()
    test_an_engine_down_returns_no_context_and_one_log_line()
    test_a_non_slack_platform_gets_no_context()
    test_a_message_with_nothing_to_recall_gets_no_context()
    test_the_plugin_keeps_no_renderer_of_its_own()
    print("ok - boring-memory plugin: named notes, 없음 line, recall handover, dead-engine silence")
