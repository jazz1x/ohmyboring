# Event DB Primary RALPH Review

Date: 2026-07-01
Branch: `feat/event-db-primary`

## BLUF

이벤트 로깅은 DB-primary가 맞다. NDJSON은 기본 저장소가 아니라 엔진/DB 장애 시 세션 종료 훅을 막지 않기 위한 fallback spool이다. 이번 변경은 그 계약을 코드, 테스트, 문서, 런타임 검증으로 맞춘다.

## Requirements

| Requirement | Status | Evidence |
|---|---:|---|
| DB에만 적재되는 기본 흐름 | Done | `agents/shared/event_log.py` default `BORING_EVENT_SINK=db`; live probe created DB row and no fallback file |
| fallback이 필요한 사유 설명 | Done | README 3개 언어와 `.env.example`에 engine-down fallback으로 명시 |
| Claude/Kimi/Codex/Hermes 허용 구조 | Done | 모든 adapter가 `agents/shared/event_log.py`를 사용; file-assert tests force `BORING_EVENT_SINK=spool` |
| MCP/HTTP에서 로그 조회 | Done | `/events`, `/otel-events`, MCP `events` 유지 |
| SRP/clean architecture 점검 | Done | sink selection, OTel payload build, DB HTTP/MCP store responsibilities 분리 확인 |
| 적대적 RALPH loop | Done | subagent audit + local adversarial checks + guard/live DB proof |

## Design Decision

- Primary sink: local engine DB `event_log`.
- Fallback sink: NDJSON file at `BORING_EVENT_LOG`.
- Default: `BORING_EVENT_SINK=db`, `BORING_EVENT_SPOOL=on_failure`.
- Strict DB-only: set `BORING_EVENT_SPOOL=off`.
- Spool-only: set `BORING_EVENT_SINK=spool`.
- Legacy compatibility: `BORING_EVENT_DB_MIRROR=0` maps to spool-only, `=1` maps to both.

## Why Not Remove The File Spool Entirely

Claude/Kimi/Codex/Hermes hooks run at host/session boundaries. If the engine is down during SessionEnd, a DB-only hard dependency can either block the user flow or lose the local diagnostic event. The fallback spool keeps the event recoverable while preserving DB as the normal query path. This is not a second source of truth for normal operation; it is an outage buffer.

## Subagent Inputs

| Agent | Model | Focus | Result |
|---|---|---|---|
| Plato | `gpt-5.3-codex-spark` | Claude/Kimi/Codex/Hermes compatibility | Confirmed central shared logger path; requested test env hygiene |
| Popper | `gpt-5.3-codex-spark` | RALPH/SRP/Clean Architecture | Flagged mirror wording, silent batch truncation, negative `since_hours`, lifecycle gaps |

## RALPH Loop

| Step | Attack | Hardening |
|---|---|---|
| Role | First-time operator expects DB logs | Docs now say DB-first, file fallback only |
| Attack | Engine down during hook | fallback spool remains available |
| Limit | Batch with 101 events silently truncates | HTTP now rejects oversized batch with 400 |
| Parse | Negative `since_hours` | HTTP returns 400; MCP returns `-32602` |
| Hardening | Local tests accidentally depend on running engine | File-assert tests force `BORING_EVENT_SINK=spool` |

## Clean Architecture / SRP Notes

- Adapter layer owns host I/O and fallback policy: `agents/shared/event_log.py`.
- Engine layer owns durable queryable event storage: `drudge/src/store.rs`.
- HTTP/MCP layer owns request contracts and input rejection: `drudge/src/serve/http.rs`, `drudge/src/serve/mcp.rs`.
- Tests now separate DB-primary behavior from spool-only fixture behavior.
- Remaining acceptable debt: `Store::log_event` still normalizes OTel payload and persists it in one method. It is contained, tested, and can be extracted later if more event schemas appear.

## Verification

| Check | Result |
|---|---:|
| `python3 agents/shared/test_event_log.py` | Pass |
| `python3 agents/shared/test_distill_core.py` | Pass |
| `python3 agents/claude-code/test_hooks.py` | Pass |
| `python3 agents/kimi/test_kimi.py` | Pass |
| `python3 agents/codex/test_codex.py` | Pass |
| `python3 agents/schedulers/test_collectors.py` | Pass |
| `python3 agents/hermes/test_ingest_worker.py` | Pass with local bind permission |
| `cargo clippy --quiet --all-targets -- -D warnings` | Pass |
| `cargo test --quiet` | Pass |
| `make guard` | Pass |
| live `/events?limit=1` | 200 |
| live `/events?since_hours=-5` | 400 |
| live Python event record | DB row exists; fallback file absent |
| live Postgres integration test | Pass |

## Conclusion

The acceptable structure is DB-primary with fallback spool, not permanent mirror-first. This satisfies the user intent while keeping Claude/Kimi/Codex/Hermes hooks resilient when the local engine is temporarily unavailable.
