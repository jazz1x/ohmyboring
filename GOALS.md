# GOALS

This file is the SSOT for the current self-verification loop. It turns the broad
"keep improving ingestion condition" intent into measurable gates.

## North Star

oh-my-boring should ingest useful local work memory without interrupting the user.
The write door stays gated, deterministic, and observable; the read door stays fast.

## Current Slice

Scope for this slice:

- Codex session harvesting keeps moving without zombie/duplicate accumulation.
- Weak duplicate notes can be replaced only through conservative identity signals.
- Compile, lint, test, readiness, and data hygiene are continuously observed.
- Rust workflow graph remains the transition contract; Python adapters project host I/O into that contract.

Out of scope for this slice:

- Bulk vault mutation.
- Renumbering.
- DB reset.
- Moving host I/O orchestration into Rust.

## Verification Contract

All numbers below are gate thresholds, not descriptive examples.

| Gate | Threshold | Failure Handling |
| --- | ---: | --- |
| `codex-status-strict` | 100% pass | Block stage transition; inspect worker, marker, queue, newest note. |
| `make readiness` | 100% pass | Block briefing/release stage; inspect doctor output. |
| `make quality` | 100% pass | Block PR/release stage; fix MCP/docs/quality drift. |
| `make guard` | 100% pass for scheduled guard runs | Block PR/release stage; fix compile/lint/test/root cause. |
| failed self-verify steps | 0 | Block next stage. |
| stale pending markers | 0 | Block readiness. |
| stale retry markers | 0 | Block readiness. |
| dead-letter markers | 0 | Block readiness. |
| recent resolution failures | 0 in doctor window | Block readiness. |
| sync-degraded collector event | allowed only if remember batch succeeded | Keep visible; does not block bootstrap/soak unless paired with failed batch. |

## Stage Contract

The live self-verification loop writes a TSV summary with one row per step.
`scripts/self-verify-contract.py` evaluates that summary.

| Stage | Required Cycles | Required Guard Runs | Required Steps | Transition |
| --- | ---: | ---: | --- | --- |
| `bootstrap` | 1 | 1 | `codex-status-strict`, `readiness`, `quality`, `recent-events`, `guard` | Pass -> `soak-2h` |
| `soak-2h` | 6 | 2 | every cycle has status/readiness/quality/events; cycle 1 and 6 include guard | Pass -> `day` |
| `day` | 72 | 13 | every cycle has status/readiness/quality/events; guard at cycle 1 and every 6 cycles | Pass -> release-candidate briefing confidence |

No stage may advance with a failed row in the evaluated summary.

## Graph Contract

The Rust workflow graph is the closed transition vocabulary.

| Metric | Value |
| --- | ---: |
| graph name | `memory_ingest` |
| nodes | 11 |
| edges | 16 |
| terminal nodes | 1 |
| entry | `session_discovered` |
| terminal | `readiness_projected` |

Required graph paths:

- happy path: `session_discovered -> transcript_prepared -> distill_requested -> resolution_verified -> remember_requested -> done_marked -> resolution_event_recorded -> readiness_projected`
- repair path: `resolution_verified --fail--> resolution_repaired --pass--> remember_requested`
- retry path: `resolution_repaired --fail--> retry_marked -> resolution_event_recorded -> readiness_projected`
- duplicate path: `remember_requested --duplicate--> done_marked`
- skip path: `transcript_prepared --skip--> skipped -> resolution_event_recorded -> readiness_projected`

## Operating Policy

- If a gate fails, do not add timeout/retry/null-check symptom treatment before writing the root cause.
- If the root cause is a contract mismatch, update the graph/test contract first.
- If the root cause is host state, keep it in Python/launchd/doctor and project it into workflow events.
- If the root cause is weak content, improve resolution/quality gates before increasing ingestion rate.
- If the root cause requires bulk vault mutation, propose candidates first and do not apply automatically.

## Current Live Loop

The active one-day loop is expected to produce:

- 72 cycles over 24 hours at 20-minute intervals.
- 13 guard runs (`cycle 1`, then every 6-cycle boundary through cycle 72).
- 0 failed rows.

The check command is:

```sh
make self-verify-check STAGE=bootstrap
make self-verify-check STAGE=soak-2h
make self-verify-check STAGE=day
```
