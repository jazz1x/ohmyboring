from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ohmyboring.distill.nodes import draft, language, note, remember, repair, skip, verify
from ohmyboring.distill.state import DistillState

Node = Callable[[DistillState], dict[str, Any]]


@dataclass(frozen=True)
class Steps:
    draft: Node
    skip: Node
    retry_language: Node
    prepare: Node
    verify: Node
    repair_call: Node
    repair_prepare: Node
    repair_verify: Node
    repair_passed: Node
    repair_failed: Node
    give_up: Node
    remember: Node


def _after_draft(state: DistillState) -> str:
    parsed = state["parsed"]
    if parsed is None:
        return END
    if parsed.get("skip"):
        return "skip"
    return "retry_language" if state["wants_language_retry"] else "prepare"


def _after_prepare(state: DistillState) -> str:
    return END if state["note"] is None else "verify"


def _after_verify(state: DistillState) -> str:
    return "remember" if state["verified"] else "repair_call"


def _after_repair_call(state: DistillState) -> str:
    repaired = state["repaired"]
    return "give_up" if repaired is None or repaired.get("skip") else "repair_prepare"


def _after_repair_prepare(state: DistillState) -> str:
    return "give_up" if state["repaired_note"] is None else "repair_verify"


def _after_repair_verify(state: DistillState) -> str:
    return "repair_passed" if state["repaired_verified"] else "repair_failed"


def build(steps: Steps) -> CompiledStateGraph:
    graph = StateGraph(DistillState)
    for name in (
        "draft",
        "skip",
        "retry_language",
        "prepare",
        "verify",
        "repair_call",
        "repair_prepare",
        "repair_verify",
        "repair_passed",
        "repair_failed",
        "give_up",
        "remember",
    ):
        graph.add_node(name, getattr(steps, name))

    graph.add_edge(START, "draft")
    graph.add_conditional_edges("draft", _after_draft)
    graph.add_edge("retry_language", "prepare")
    graph.add_conditional_edges("prepare", _after_prepare)
    graph.add_conditional_edges("verify", _after_verify)
    graph.add_conditional_edges("repair_call", _after_repair_call)
    graph.add_conditional_edges("repair_prepare", _after_repair_prepare)
    graph.add_conditional_edges("repair_verify", _after_repair_verify)
    graph.add_edge("repair_passed", "remember")
    for terminal in ("skip", "repair_failed", "give_up", "remember"):
        graph.add_edge(terminal, END)
    return graph.compile()


graph = build(
    Steps(
        draft=draft.draft,
        skip=skip.skip,
        retry_language=language.retry_language,
        prepare=note.prepare,
        verify=verify.verify,
        repair_call=repair.repair_call,
        repair_prepare=note.repair_prepare,
        repair_verify=verify.repair_verify,
        repair_passed=repair.repair_passed,
        repair_failed=repair.repair_failed,
        give_up=repair.give_up,
        remember=remember.remember,
    )
)
