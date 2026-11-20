<!-- derived-from: PRD v4 -->
# GOALS

Derived from `docs/PRD.md`. The PRD says what the product is; this file says which of its
requirements the current slice enforces, and with which gates. Every gate row carries the `[R#]`
it comes from — a gate that derives from no requirement does not belong here, which is how this
file stops becoming a defect log again.

## North Star

oh-my-boring should ingest useful local work memory without interrupting the user.
The write door stays gated, deterministic, and observable; the read door stays fast.

## Current Slice

**Memory management and use.** Storing knowledge is fine; how it is managed and used now matters
more. The slice is these five steps, in this order. Each step after the first gets its own
contract, and that contract names the requirement it carries.

In scope:

1. Measure use path by path, reads only **[R1]**.
2. Put verdicts and freshness into ranking **[R2]**.
3. Reduce the paths nobody uses.
4. Close finished next-actions, group what is related, and name it.
5. Turn the morning card into advice.

Also in scope:

- Confirm delivery the day a PR merges — `/health.build_sha == HEAD` plus the installer run — for
  anything that reaches `drudge/` or `~/.hermes`. Seven times now a merge has left production on
  the old build, twice on 2026-09-02 alone **[R4]**.

Out of scope for this slice:

- Bulk vault mutation, renumbering, DB reset **[R4]**.

Requirements this slice carries no new work for, and why:

- **[R3]** is not one of the five steps.
- **[R5]** boundaries (origin split, PII gate, injection fence) are in place and enforced by the
  existing gates; this slice guards against regression only. The `forget` path-traversal question
  is the one open item and it is the owner's to confirm.
- **[R6]** is not one of the five steps; the read door stays fast and LLM-free.
- **[R7]** (injection surfaces as declaration rather than per-adapter code) is adopted in the PRD
  but carries no gate here, and it is not one of the five steps.

## Verification Contract [R4]

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

## Stage Contract [R4]

The live self-verification loop writes a TSV summary with one row per step.
`scripts/self-verify-contract.py` evaluates that summary.

| Stage | Required Cycles | Required Guard Runs | Required Steps | Transition |
| --- | ---: | ---: | --- | --- |
| `bootstrap` | 1 | 1 | `codex-status-strict`, `readiness`, `quality`, `recent-events`, `guard` | Pass -> `soak-2h` |
| `soak-2h` | 6 | 2 | every cycle has status/readiness/quality/events; cycle 1 and 6 include guard | Pass -> `day` |
| `day` | 72 | 13 | every cycle has status/readiness/quality/events; guard at cycle 1 and every 6 cycles | Pass -> release-candidate briefing confidence |

No stage may advance with a failed row in the evaluated summary.

## Graph Contract [R2]

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

## Operating Policy [R3]

- If a gate fails, do not add timeout/retry/null-check symptom treatment before writing the root cause.
- If the root cause is a contract mismatch, update the graph/test contract first.
- If the root cause is host state, keep it in Python/launchd/doctor and project it into workflow events.
- If the root cause is weak content, improve resolution/quality gates before increasing ingestion rate.
- If the root cause requires bulk vault mutation, propose candidates first and do not apply automatically.

## Current Live Loop

Retired 2026-08-26. Nothing in the PRD requires 72 self-verification cycles a day, and nobody
had read the result in weeks — enforcement of **[R3]** and **[R4]** is carried by readiness,
dead-letter, and the CI gates, which are checked. `make self-verify-check` still exists for
anyone who wants a stage report; it is no longer a standing expectation.
