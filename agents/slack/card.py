#!/usr/bin/env python3
"""The morning card — one LangGraph run: read the registers, propose, resolve, post, interrupt.

`make card` runs g_card once. The graph reads the engine's four registers, cross-checks
the past 48h of 「해」 against today's registers for the card's head line (a door failure
leaves an approval 확인불가 — never a false 「했다」), the local model picks three proposals
grounded in the registers — 「아직」 subjects asked for first — the resolve node turns each
proposal's subject into the note path the door's /claim-source names (dropping what no
longer resolves, re-proposing once on the remainder), posts to SLACK_CARD_CHANNEL, and
stops at an interrupt — a Slack button press (해 · 미뤄 · 빼) resumes it with
Command(resume=…), and record_verdict writes the verdict to the engine. Sent and judged
stay in one state, so the card a person answered is exactly the card the engine
remembers.

The socket lives here, not in the secretary: hermes can only text, and the secretary answers
questions while this file asks them. Everything decided lives in card_core; everything
external is a collaborator.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, NamedTuple, TypedDict

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))

import card_core  # noqa: E402
from drudge_client import DrudgeClient  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.graph.state import CompiledStateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402

DEFAULT_MODEL = os.environ.get("CARD_MODEL") or "gemma4:12b"
# Register answers carry up to 50 claims each; the door timeout lesson (brief p95 77s) says the
# point read is far cheaper, but a cold engine still earns more than a point-read default.
ENGINE_TIMEOUT = float(os.environ.get("CARD_ENGINE_TIMEOUT") or "30")
# The confirmation window on the card's head line — yesterday's and the day before's approvals.
CONFIRM_SINCE_HOURS = 48


def _append_verdicts(
    existing: list[card_core.ButtonVerdict] | None, updates: list[card_core.ButtonVerdict]
) -> list[card_core.ButtonVerdict]:
    return (existing or []) + updates


class CardState(TypedDict):
    registers: card_core.Registers
    proposals: list[card_core.Proposal]
    message: card_core.PostedCard | None
    confirmation: card_core.Confirmation | None
    priority_subjects: list[str]
    verdicts: Annotated[list[card_core.ButtonVerdict], _append_verdicts]


class Collaborators(NamedTuple):
    """Everything the graph reaches outside itself. Injected so the graph runs in tests with
    no engine, no model, no door and no Slack; live defaults are the module-level makers below."""

    fetch: Callable[[str], dict[str, Any]]  # register path → engine JSON
    propose: Callable[[str], str]  # prompt → proposal JSON text
    send: Callable[[list[dict]], card_core.PostedCard]  # Block Kit → posted card
    handover: Callable[[str, str, list[str]], dict]  # session, at, note paths
    consumption: Callable[[str, str, list[str]], dict]  # session, used|contested, paths
    resolve: Callable[[str, str], card_core.ResolvedNote | card_core.Unresolved]
    approved: Callable[[int], list[card_core.PastApproved]]  # since_hours → past approvals
    record: Callable[[str, dict], None]  # event name, fields → engine event log


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
            consumption=_live_consumption,
            resolve=_live_resolve,
            approved=_live_approved,
            record=_live_record,
        )

    def read_registers(_: CardState) -> dict:
        return {"registers": card_core.collect_registers(collabs.fetch)}

    #: subject → resolution, shared by cross_check and resolve so the door is asked once
    #: per subject per run.
    resolve_cache: dict[tuple[str, str], card_core.ResolvedNote | card_core.Unresolved] = {}

    def _resolve(subject: str, register: str) -> card_core.ResolvedNote | card_core.Unresolved:
        key = (subject, register)
        if key not in resolve_cache:
            resolve_cache[key] = collabs.resolve(subject, register)
        return resolve_cache[key]

    def cross_check(state: CardState) -> dict:
        """The card's head line, before the model picks: yesterday's 「해」, each checked
        against whether its note path still resolves from today's register subjects. A 404
        establishes absence; a door failure (5xx·불통) leaves the approval unknown — the
        card never reads a dead door as 「했다」. A dead /approved is not a value either:
        the exception stops the run, and main exits 3 without posting."""
        registers = state["registers"]
        past = collabs.approved(CONFIRM_SINCE_HOURS)
        if not past:
            return {"confirmation": None, "priority_subjects": []}
        today_notes: set[str] = set()
        note_to_subject: dict[str, str] = {}
        failures: list[str] = []
        for name in card_core.ANSWER_REGISTERS:
            for subject in registers.sources.get(name, []):
                result = _resolve(subject, name)
                if isinstance(result, card_core.ResolvedNote):
                    today_notes.add(result.note)
                    note_to_subject.setdefault(result.note, subject)
                elif not card_core.is_absence(result.reason):
                    failures.append(result.reason)
        for path in registers.sources.get("recurrences", []):
            today_notes.add(path)
            note_to_subject.setdefault(path, path)
        confirmation = card_core.confirm_past(past, today_notes, failures)
        priority = sorted({note_to_subject[note] for note in confirmation.pending if note in note_to_subject})
        return {"confirmation": confirmation, "priority_subjects": priority}

    def propose(state: CardState) -> dict:
        registers = state["registers"]
        llm_json = collabs.propose(card_core.build_prompt(registers, priority=state["priority_subjects"]))
        return {"proposals": card_core.parse_proposals(llm_json, registers)}

    def resolve(state: CardState) -> dict:
        """subject → note path through the door. A subject that does not resolve is dropped
        as a value — 「근거 노트 없음」 never rides a card — and if that leaves fewer than
        three, the model re-picks once from the subjects this node has not yet tried. Zero
        after both rounds is a refusal, not an empty card: the graph raises and main exits 3."""
        registers = state["registers"]
        picked: list[card_core.Proposal] = []
        seen_notes: set[str] = set()
        dropped: list[card_core.Unresolved] = []
        tried: set[str] = set()
        round_proposals = state["proposals"]
        for round_no in range(2):
            for proposal in round_proposals:
                if proposal.subject in tried:
                    continue
                tried.add(proposal.subject)
                result = _resolve(proposal.subject, proposal.register_)
                if isinstance(result, card_core.Unresolved):
                    dropped.append(result)
                    continue
                if result.note in seen_notes:
                    continue
                seen_notes.add(result.note)
                picked.append(proposal.model_copy(update={"note": result.note}))
            if len(picked) >= 3:
                break
            remaining = {
                subject
                for name in card_core.REGISTER_NAMES
                for subject in registers.sources.get(name, [])
                if subject not in tried
            }
            if not remaining or round_no == 1:
                break
            llm_json = collabs.propose(card_core.build_prompt(registers, tried))
            round_proposals = card_core.parse_proposals(llm_json, registers, tried)
        if not picked:
            reasons = "; ".join(f"{u.subject}: {u.reason}" for u in dropped)
            raise ValueError(f"근거 노트 없음 — 제안 0: {reasons or 'no proposals'}")
        return {"proposals": picked}

    def post_card(state: CardState) -> dict:
        message = collabs.send(card_core.build_blocks(state["proposals"], confirmation=state["confirmation"]))
        collabs.handover(
            session_name(message),
            datetime.now(UTC).isoformat(),
            [p.note for p in state["proposals"]],
        )
        confirmation = state["confirmation"]
        if confirmation is not None:
            fields: dict[str, Any] = {
                "done": len(confirmation.done),
                "pending": len(confirmation.pending),
                "session": confirmation.session,
            }
            if confirmation.unknown:
                fields["unknown"] = len(confirmation.unknown)
            collabs.record("card_confirmation", fields)
        return {"message": message}

    def await_verdict(state: CardState) -> dict:
        verdict = interrupt({"judged": len(state["verdicts"]), "of": len(state["proposals"])})
        return {"verdicts": [verdict]}

    def record_verdict(state: CardState) -> dict:
        verdict = state["verdicts"][-1]
        if verdict.choice == "defer":
            return {}  # recorded in state only — no engine call
        kind = "used" if verdict.choice == "do" else "contested"
        note = state["proposals"][verdict.idx].note
        collabs.consumption(session_name(state["message"]), kind, [note])
        return {}

    graph = StateGraph(CardState)
    graph.add_node("read_registers", read_registers)
    graph.add_node("cross_check", cross_check)
    graph.add_node("propose", propose)
    graph.add_node("resolve", resolve)
    graph.add_node("post_card", post_card)
    graph.add_node("await_verdict", await_verdict)
    graph.add_node("record_verdict", record_verdict)
    graph.add_edge(START, "read_registers")
    graph.add_edge("read_registers", "cross_check")
    graph.add_edge("cross_check", "propose")
    graph.add_edge("propose", "resolve")
    graph.add_edge("resolve", "post_card")
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


def _live_consumption(session: str, kind: str, paths: list[str]) -> dict:
    # Row-level, not session-level: a button judges the note its row cited, and only that one.
    at = datetime.now(UTC).isoformat()
    if kind == "used":
        return DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).consumption(session, at, used=paths)
    return DrudgeClient(timeout=ENGINE_TIMEOUT, retries=0).consumption(session, at, contested=paths)


def _live_resolve(subject: str, register: str) -> card_core.ResolvedNote | card_core.Unresolved:
    """subject → current claim's note path through the door. A recurrence source is already
    a note path — it resolves to itself without a door round-trip. Everything else asks
    /claim-source; every failure mode is an Unresolved value: the card drops the proposal,
    and only an all-dropped run stops the card (with the reasons, in main's exit 3)."""
    if subject.startswith("/"):
        return card_core.ResolvedNote(subject=subject, note=subject)
    url = f"{_door_url()}/claim-source?subject={urllib.parse.quote(subject)}"
    try:
        with urllib.request.urlopen(url, timeout=ENGINE_TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            reason = card_core.NO_CURRENT_CLAIM
        else:
            reason = f"claim-source answered {e.code}"
        return card_core.Unresolved(subject=subject, register=register, reason=reason)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return card_core.Unresolved(
            subject=subject, register=register, reason=f"claim-source unreachable: {e}"
        )
    return card_core.ResolvedNote(subject=subject, note=payload["note"])


def _live_approved(since_hours: int) -> list[card_core.PastApproved]:
    url = f"{_door_url()}/approved?since_hours={since_hours}"
    with urllib.request.urlopen(url, timeout=ENGINE_TIMEOUT) as r:
        payload = json.loads(r.read().decode("utf-8"))
    return [
        card_core.PastApproved(session=item["session"], note=item["note"], at=item["at"])
        for item in payload["approved"]
    ]


def _live_record(event: str, fields: dict) -> None:
    import event_log

    event_log.append_event("slack-card", event, "ok", **fields)


def _door_url() -> str:
    return os.environ["BORING_DOOR_URL"].rstrip("/")


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
    if not os.environ.get("BORING_DOOR_URL"):
        print(
            "[card] BORING_DOOR_URL must be set — the card cannot attach 판정 to notes without "
            "the door's /claim-source, and cannot cross-check past approvals without /approved "
            "(see .env.example)",
            file=sys.stderr,
        )
        return 2
    owner_id = os.environ.get("SECRETARY_OWNER_ID") or None
    if owner_id is None:
        print("[card] no SECRETARY_OWNER_ID — any user may judge", file=sys.stderr)

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
    try:
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
        except (ValueError, OSError) as e:
            # A malformed register, an ungrounded proposal, a dead door, a refused resolve —
            # say why in one line and stop. OSError covers URLError: the door was the only
            # road to /claim-source and /approved, and a card that cannot confirm is a card
            # that must not ship quietly.
            print(f"[card] 카드 거부: {' '.join(str(e).split())}", file=sys.stderr)
            return 3
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
