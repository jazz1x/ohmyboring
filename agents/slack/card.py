#!/usr/bin/env python3
"""The morning card — one LangGraph run: read the registers, propose, post, interrupt.

`make card` runs g_card once. The graph reads the engine's four registers, the local model
picks three proposals grounded in them, the card posts to SLACK_CARD_CHANNEL, and the graph
stops at an interrupt — a Slack button press (해 · 미뤄 · 빼) resumes it with
Command(resume=…), and record_verdict writes the verdict to the engine. Sent and judged stay
in one state, so the card a person answered is exactly the card the engine remembers.

The socket lives here, not in the secretary: hermes can only text, and the secretary answers
questions while this file asks them. Everything decided lives in card_core; everything
external is a collaborator.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, NamedTuple, TypedDict

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))

import card_core  # noqa: E402
import secretary_core  # noqa: E402
from drudge_client import DrudgeClient  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.graph.state import CompiledStateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402

DEFAULT_MODEL = os.environ.get("CARD_MODEL") or "gemma4:12b"
# Register answers carry up to 50 claims each; the door timeout lesson (brief p95 77s) says the
# point read is far cheaper, but a cold engine still earns more than a point-read default.
ENGINE_TIMEOUT = float(os.environ.get("CARD_ENGINE_TIMEOUT") or "30")


def _append_verdicts(
    existing: list[card_core.ButtonVerdict] | None, updates: list[card_core.ButtonVerdict]
) -> list[card_core.ButtonVerdict]:
    return (existing or []) + updates


class CardState(TypedDict):
    registers: card_core.Registers
    proposals: list[card_core.Proposal]
    message: card_core.PostedCard | None
    verdicts: Annotated[list[card_core.ButtonVerdict], _append_verdicts]


class Collaborators(NamedTuple):
    """Everything the graph reaches outside itself. Injected so the graph runs in tests with
    no engine, no model and no Slack; live defaults are the module-level makers below."""

    fetch: Callable[[str], dict[str, Any]]  # register path → engine JSON
    propose: Callable[[str], str]  # prompt → proposal JSON text
    send: Callable[[list[dict]], card_core.PostedCard]  # Block Kit → posted card
    handover: Callable[[str, str, list[str]], dict]  # session, at, source notes
    feedback: Callable[[str, str], dict]  # session, verdict → engine answer


def session_name(card: card_core.PostedCard) -> str:
    """The engine's name for this card — the same key /handover wrote and /consumption judges."""
    return f"slack:{card.channel}:{card.ts}"


def build_graph(collabs: Collaborators | None = None) -> CompiledStateGraph:
    if collabs is None:
        collabs = Collaborators(
            fetch=_live_fetch,
            propose=make_propose(),
            send=_env_send,
            handover=_live_handover,
            feedback=secretary_core.feedback,
        )

    def read_registers(_: CardState) -> dict:
        return {"registers": card_core.collect_registers(collabs.fetch)}

    def propose(state: CardState) -> dict:
        registers = state["registers"]
        llm_json = collabs.propose(card_core.build_prompt(registers))
        return {"proposals": card_core.parse_proposals(llm_json, registers)}

    def post_card(state: CardState) -> dict:
        message = collabs.send(card_core.build_blocks(state["proposals"]))
        collabs.handover(
            session_name(message),
            datetime.now(UTC).isoformat(),
            [p.source_note for p in state["proposals"]],
        )
        return {"message": message}

    def await_verdict(state: CardState) -> dict:
        verdict = interrupt({"judged": len(state["verdicts"]), "of": len(state["proposals"])})
        return {"verdicts": [verdict]}

    def record_verdict(state: CardState) -> dict:
        verdict = state["verdicts"][-1]
        key = session_name(state["message"])
        if verdict.choice == "do":
            collabs.feedback(key, "used")
        elif verdict.choice == "drop":
            collabs.feedback(key, "contested")
        return {}  # defer is recorded in state only — no engine verdict

    graph = StateGraph(CardState)
    graph.add_node("read_registers", read_registers)
    graph.add_node("propose", propose)
    graph.add_node("post_card", post_card)
    graph.add_node("await_verdict", await_verdict)
    graph.add_node("record_verdict", record_verdict)
    graph.add_edge(START, "read_registers")
    graph.add_edge("read_registers", "propose")
    graph.add_edge("propose", "post_card")
    graph.add_edge("post_card", "await_verdict")
    graph.add_edge("await_verdict", "record_verdict")
    graph.add_conditional_edges(
        "record_verdict",
        lambda state: "await_verdict" if len(state["verdicts"]) < len(state["proposals"]) else END,
    )
    return graph.compile(checkpointer=MemorySaver())


def _live_fetch(path: str) -> dict[str, Any]:
    # DrudgeClient has no public raw-POST door; _retry is the one that speaks JSON both ways.
    return DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0)._retry("POST", path, {})


def _live_handover(session: str, at: str, paths: list[str]) -> dict:
    return DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).handover(session, at, paths)


def _env_send(blocks: list[dict]) -> card_core.PostedCard:
    from slack_sdk.web import WebClient

    channel = os.environ["SLACK_CARD_CHANNEL"]  # main() validated before the graph runs
    web = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))
    resp = web.chat_postMessage(channel=channel, blocks=blocks)
    return card_core.PostedCard(channel=channel, ts=resp["ts"])


def make_propose(model: str = DEFAULT_MODEL) -> Callable[[str], str]:
    """The structured-output seam: prompt in, proposal JSON text out. Structured output keeps
    gemma4 inside the schema; parse_proposals keeps it inside the registers. `reasoning=False`
    — gemma4 is a thinking variant and the thinking is latency with no pick to show for it."""

    from langchain_core.runnables import Runnable
    from langchain_ollama import ChatOllama

    llm = ChatOllama(model=model, format="json", temperature=0, reasoning=False, num_ctx=16384)
    chain: Runnable = llm.with_structured_output(card_core.Proposals)

    def propose(prompt: str) -> str:
        return chain.invoke(prompt).model_dump_json(by_alias=True)

    return propose


class _Holder:
    """What the socket listener and the main loop share: the posted card, the proposals, the
    presses already judged, and a queue of verdicts waiting to resume the graph."""

    def __init__(self) -> None:
        from queue import Queue

        self.queue: Queue[card_core.ButtonVerdict] = Queue()
        self.message: card_core.PostedCard | None = None
        self.proposals: list[card_core.Proposal] = []
        self.judged: set[int] = set()


def _listener(holder: _Holder, owner_id: str | None) -> Callable:
    def on_request(client, req) -> None:
        try:
            from slack_sdk.socket_mode.response import SocketModeResponse

            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception as e:  # noqa: BLE001 — an unacked envelope is retried; a dead loop is not
            print(f"[card] ack failed: {e}", file=sys.stderr)
            return
        if getattr(req, "type", None) != "interactive":
            return
        payload = getattr(req, "payload", None) or {}
        if payload.get("type") != "block_actions":
            return
        if holder.message is None:
            print("[card] button before card — ignored", file=sys.stderr)
            return
        if (payload.get("message") or {}).get("ts") != holder.message.ts:
            return
        verdict = card_core.parse_action(payload, owner_id=owner_id, n_proposals=len(holder.proposals))
        if isinstance(verdict, card_core.Rejected):
            print(f"[card] rejected: {verdict.reason}", file=sys.stderr)
            return
        if verdict.idx in holder.judged:
            print(f"[card] proposal {verdict.idx} already judged — ignored", file=sys.stderr)
            return
        holder.queue.put(verdict)

    return on_request


def main() -> int:
    app_token = os.environ.get("SLACK_APP_TOKEN")
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not app_token or not bot_token:
        print(
            "[card] SLACK_APP_TOKEN and SLACK_BOT_TOKEN must be set (see .env.example)",
            file=sys.stderr,
        )
        return 2
    if not os.environ.get("SLACK_CARD_CHANNEL"):
        print(
            "[card] SLACK_CARD_CHANNEL must be set — the channel id the morning card posts to "
            "(see .env.example)",
            file=sys.stderr,
        )
        return 2
    owner_id = os.environ.get("SECRETARY_OWNER_ID") or None

    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.web import WebClient

    web_client = WebClient(token=bot_token)
    try:
        web_client.auth_test()
    except Exception as e:  # noqa: BLE001 — say why in one line, not with a stack trace and no token
        print(f"[card] auth_test failed: {e}", file=sys.stderr)
        return 1

    holder = _Holder()
    client = SocketModeClient(app_token=app_token, web_client=web_client)
    client.socket_mode_request_listeners.append(_listener(holder, owner_id))
    client.connect()

    graph = build_graph()
    config = {"configurable": {"thread_id": f"card-{datetime.now(UTC):%Y%m%d}"}}
    try:
        state = graph.invoke({"verdicts": []}, config)
        holder.message = state["message"]
        holder.proposals = state["proposals"]
        while True:
            verdict = holder.queue.get()
            holder.judged.add(verdict.idx)
            state = graph.invoke(Command(resume=verdict), config)
            web_client.chat_update(
                channel=holder.message.channel,
                ts=holder.message.ts,
                blocks=card_core.build_blocks(state["proposals"], state["verdicts"]),
            )
            if "__interrupt__" not in state:
                break
    except KeyboardInterrupt:
        print("[card] 결재 대기를 멈춘다 — 카드는 슬랙에 남아 있다.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
