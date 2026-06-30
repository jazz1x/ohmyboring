# 2026-06-30 Rust Workflow Graph Decision

## BLUF

Rust LangGraph 도입은 "외부 Rust LangGraph 런타임 추가"가 아니라 `drudge` 내부의 typed workflow graph 계약으로 시작한다.

이 결정은 현재 릴리즈 직전 상태에서 가장 싸고 안전하다. Python hook/worker/readiness가 실제 호스트 I/O를 계속 담당하고, Rust는 노드/엣지/터미널/검증 규칙을 닫힌 타입으로 고정한다.

## 현재 상태

| 영역 | 현재 SSOT | 판단 |
|---|---|---|
| Semantic memory graph | `drudge/src/graph.rs`, `drudge/src/ingest.rs` | 유지 |
| Session distill/repair | `agents/shared/distill_core.py` | 유지 |
| Codex/Claude/Kimi discovery | `agents/*/collect-sessions.py`, `agents/*/distill-session.py` | 유지 |
| Readiness projection | `scripts/doctor.sh`, marker/event readers | 유지 |
| Workflow graph vocabulary | `drudge/src/workflow.rs` | 신규 SSOT |

## 결정

- `drudge/src/workflow.rs`를 Rust LangGraph-style 계약으로 둔다.
- 첫 PR에서는 런타임 제어 흐름을 바꾸지 않는다.
- 외부 dependency를 추가하지 않는다.
- Python adapter는 이후 PR에서 이 vocabulary에 맞춰 이벤트/상태명을 맞춘다.
- readiness는 계속 `make readiness`가 검증한다.

## 왜 지금 이 방식인가

- Layer 1: 상태 이름이 문자열로 흩어지면 거짓 상태가 생긴다. Rust enum으로 닫는다.
- Layer 2: 적재 흐름은 발견 -> 준비 -> 증류 -> 검증/보강 -> 기억 -> 마커 -> 이벤트 -> readiness 한 방향이다.
- Layer 3: 릴리즈 전에는 새 오케스트레이터보다 작은 계약이 싸다.

## 수용 기준

- `memory_ingest_graph().validate()`가 빈 issue를 반환한다.
- resolution 실패는 반드시 repair 또는 retry로만 흐른다.
- duplicate remember는 실패가 아니라 done marker로 흐른다.
- 기존 doctor/readiness/hook/worker 동작은 바꾸지 않는다.
- README 3개와 `drudge/WORKFLOW.md`가 "무엇을 도입했고 무엇을 도입하지 않았는지" 명확히 말한다.

## 다음 슬라이스

- Python event names를 `WorkflowNode::as_str()` vocabulary와 맞춘다.
- readiness event projection에 workflow node/outcome 필드를 붙인다.
- Graph view용 Mermaid/NDJSON export를 추가할지 평가한다.
- host I/O까지 Rust로 옮기는 것은 launchd/cron/Docker/model readiness drift를 먼저 줄인 뒤 판단한다.

## 2026-06-30 follow-up

- Python adapter 이벤트에 `workflow=memory_ingest`, `workflow_node`, `workflow_outcome` 필드를 붙이는 슬라이스를 진행한다.
- 이 슬라이스도 런타임 오케스트레이션을 바꾸지 않는다. 기존 이벤트에 graph projection metadata만 추가한다.
- Codex collector, Hermes ingest worker, Claude/Codex shared distill events가 Rust workflow vocabulary를 투영한다.
- Python mapper는 unknown event/status 조합을 조용히 generic node로 보내지 않고 실패시킨다.
- Guard는 Python vocabulary가 `drudge/src/workflow.rs`의 `as_str()` 값과 드리프트하지 않는지 확인한다.
