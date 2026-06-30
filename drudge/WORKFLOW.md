# Workflow Graph Contract

`drudge/src/workflow.rs` is the Rust-side workflow graph contract for session
memory ingestion.

It is intentionally small:

- It models workflow states, outcomes, edges, terminals, and graph shape checks.
- It does not execute hooks, call an LLM, inspect launchd/cron, or read markers.
- It does not replace `drudge/src/graph.rs`; that module remains the semantic
  memory graph for tools, concepts, claims, and recall.

## Boundary

The host adapters still own external orchestration:

- `agents/shared/distill_core.py` builds and verifies distilled notes.
- `agents/*/distill-session.py` adapt each agent's transcript format.
- `agents/*/collect-sessions.py` discover sessions and manage markers.
- `scripts/doctor.sh` projects host/container/model/marker state into readiness.

The Rust workflow graph owns the invariant:

> A session may only move through known nodes and labelled transitions before it
> reaches the readiness projection.

This keeps the project aligned with the core philosophy:

- **Layer 1**: impossible states are represented by closed enums.
- **Layer 2**: the write-door flow is one direction.
- **Layer 3**: the first slice is a contract, not a new orchestrator.

## Current Graph

Entry:

- `session_discovered`

Terminals:

- `readiness_projected`

Key paths:

- happy path: `session_discovered -> transcript_prepared -> distill_requested -> resolution_verified -> remember_requested -> done_marked -> resolution_event_recorded -> readiness_projected`
- repair path: `resolution_verified --fail--> resolution_repaired --pass--> remember_requested`
- retry path: `resolution_repaired --fail--> retry_marked -> resolution_event_recorded -> readiness_projected`
- skip path: `transcript_prepared --skip--> skipped -> resolution_event_recorded -> readiness_projected`

## Acceptance Gate

This graph is acceptable only while these stay true:

- `memory_ingest_graph().validate()` has no issues.
- Existing hook, worker, `doctor`, and `readiness` behavior is unchanged.
- Any future adapter integration maps to these nodes instead of inventing local
  state names.
