#!/usr/bin/env python3
"""Adapter-facing workflow vocabulary for memory ingestion events.

The canonical graph shape lives in `drudge/src/workflow.rs`. This module keeps
Python adapters on the same stable event vocabulary without making them shell
out to the Rust binary during hooks.
"""

WORKFLOW_MEMORY_INGEST = "memory_ingest"


class Node:
    SESSION_DISCOVERED = "session_discovered"
    TRANSCRIPT_PREPARED = "transcript_prepared"
    DISTILL_REQUESTED = "distill_requested"
    RESOLUTION_VERIFIED = "resolution_verified"
    RESOLUTION_REPAIRED = "resolution_repaired"
    REMEMBER_REQUESTED = "remember_requested"
    DONE_MARKED = "done_marked"
    RETRY_MARKED = "retry_marked"
    SKIPPED = "skipped"
    RESOLUTION_EVENT_RECORDED = "resolution_event_recorded"
    READINESS_PROJECTED = "readiness_projected"


class Outcome:
    CONTINUE = "continue"
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    DUPLICATE = "duplicate"


NODES = (
    Node.SESSION_DISCOVERED,
    Node.TRANSCRIPT_PREPARED,
    Node.DISTILL_REQUESTED,
    Node.RESOLUTION_VERIFIED,
    Node.RESOLUTION_REPAIRED,
    Node.REMEMBER_REQUESTED,
    Node.DONE_MARKED,
    Node.RETRY_MARKED,
    Node.SKIPPED,
    Node.RESOLUTION_EVENT_RECORDED,
    Node.READINESS_PROJECTED,
)

OUTCOMES = (
    Outcome.CONTINUE,
    Outcome.PASS,
    Outcome.FAIL,
    Outcome.SKIP,
    Outcome.DUPLICATE,
)


def fields(node: str, outcome: str) -> dict[str, str]:
    return {
        "workflow": WORKFLOW_MEMORY_INGEST,
        "workflow_node": node,
        "workflow_outcome": outcome,
    }


def resolution_fields(verifier_status: str, remember_status: str) -> dict[str, str]:
    if verifier_status in {"pass", "repaired"}:
        if remember_status == "remembered":
            return fields(Node.REMEMBER_REQUESTED, Outcome.PASS)
        if remember_status == "duplicate":
            return fields(Node.REMEMBER_REQUESTED, Outcome.DUPLICATE)
        if remember_status == "failed":
            return fields(Node.REMEMBER_REQUESTED, Outcome.FAIL)
    if verifier_status == "failed" and remember_status == "not_called":
        return fields(Node.RESOLUTION_REPAIRED, Outcome.FAIL)
    raise ValueError(
        "unknown resolution workflow projection: "
        f"verifier_status={verifier_status!r} remember_status={remember_status!r}"
    )


def skip_fields() -> dict[str, str]:
    return fields(Node.SKIPPED, Outcome.SKIP)


def readiness_fields(ready: bool) -> dict[str, str]:
    return fields(Node.READINESS_PROJECTED, Outcome.PASS if ready else Outcome.FAIL)


def collector_run_fields(status: str, batch: int) -> dict[str, str]:
    if status == "ok" and batch == 0:
        return readiness_fields(True)
    if status == "ok":
        return fields(Node.DONE_MARKED, Outcome.CONTINUE)
    if status == "failed":
        return fields(Node.RETRY_MARKED, Outcome.FAIL)
    raise ValueError(f"unknown collector workflow projection: status={status!r}")


def worker_fields(event: str, status: str) -> dict[str, str]:
    if event == "ingest_offer" and status == "pending":
        return fields(Node.TRANSCRIPT_PREPARED, Outcome.CONTINUE)
    if event == "ingest_offer" and status == "skipped":
        return fields(Node.SKIPPED, Outcome.SKIP)
    if event == "ingest_offer" and status == "ok":
        return fields(Node.READINESS_PROJECTED, Outcome.PASS)
    if event == "ingest_reconcile" and status == "ok":
        return fields(Node.DONE_MARKED, Outcome.CONTINUE)
    if event == "ingest_reconcile" and status in {"retry", "failed"}:
        return fields(Node.RETRY_MARKED, Outcome.FAIL)
    raise ValueError(f"unknown worker workflow projection: event={event!r} status={status!r}")
