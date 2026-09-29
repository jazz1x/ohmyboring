from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ohmyboring.weekly.nodes import (
    engine,
    payload,
    post,
    posted,
    read_week,
    record,
    render_engine,
    render_trend,
)
from ohmyboring.weekly.state import PostFailed, WeeklyState

Node = Callable[[WeeklyState], dict[str, Any]]


@dataclass(frozen=True)
class Steps:
    check_posted: Node
    read_week: Node
    render_trend: Node
    engine: Node
    render_engine: Node
    to_payload: Node
    post: Node
    record: Node


def _start(state: WeeklyState) -> str:
    return "check_posted" if state["deliver"] else "read_week"


def _after_check(state: WeeklyState) -> str:
    return END if "outcome" in state else "read_week"


def _after_read(state: WeeklyState) -> str:
    return "render_trend" if state["projects"] else "engine"


def _after_render(state: WeeklyState) -> str:
    return "to_payload" if state["deliver"] else END


def _after_engine(state: WeeklyState) -> str:
    return "render_engine" if state.get("answer") else _after_render(state)


def _after_payload(state: WeeklyState) -> str:
    return "post" if "payload" in state else END


def _after_post(state: WeeklyState) -> str:
    return END if isinstance(state.get("outcome"), PostFailed) else "record"


def build(steps: Steps) -> CompiledStateGraph:
    graph = StateGraph(WeeklyState)
    for name in Steps.__dataclass_fields__:
        graph.add_node(name, getattr(steps, name))

    graph.add_conditional_edges(START, _start)
    graph.add_conditional_edges("check_posted", _after_check)
    graph.add_conditional_edges("read_week", _after_read)
    graph.add_conditional_edges("render_trend", _after_render)
    graph.add_conditional_edges("engine", _after_engine)
    graph.add_conditional_edges("render_engine", _after_render)
    graph.add_conditional_edges("to_payload", _after_payload)
    graph.add_conditional_edges("post", _after_post)
    graph.add_edge("record", END)
    return graph.compile()


graph = build(
    Steps(
        check_posted=posted.check_posted,
        read_week=read_week.read_week,
        render_trend=render_trend.render_trend,
        engine=engine.engine,
        render_engine=render_engine.render_engine,
        to_payload=payload.to_payload,
        post=post.post,
        record=record.record,
    )
)
