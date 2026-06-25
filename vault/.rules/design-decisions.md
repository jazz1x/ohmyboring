# Design Decisions — oh-my-boring

> 이 문서는 oh-my-boring(drudge 엔진 포함)의 **non-negotiable 설계 결정**을 기록한 SSOT다.
> 각 결정은 "왜 이렇게 설계했는가"와 "코드 근거"를 함께 담고 있다.
> 새 기능을 추가하거나 리팩토링할 때 이 표를 먼저 읽고, 결정을 위반하지 않는지 확인하라.
> 중요한 변경이 생기면 `vault/wiki/`에 `kind: decision` 노트를 추가하고 이 문서를 업데이트한다.

---

## Decision table

| ID | Decision | Why | 코드 근거 | 변경 시 확인 |
|---|---|---|---|---|
| **D1** | **Wiki-first, pgvector는 선택적 가속기** | 소규모 개인 코퍼스에서는 markdown 직접 읽기가 임베딩+RAG보다 단순·디버깅 용이·신뢰 가능하다. pgvector는 규모/정확도 트리거를 넘었을 때 켜는 가속기다. | `drudge/src/wiki_recall.rs`<br>`drudge/src/main.rs:106-119`<br>`README.md:42-43`<br>`drudge/CLAUDE.md:39-43` | `BORING_VECTOR=off` 기본 동작이 깨지지 않는가? |
| **D2** | **Deterministic graph — kernel에선 LLM extraction 금지** | Semantic graph(tool/concept/claim)은 에이전트가 이미 정제한 frontmatter에서만 나온다. drudge는 **embed+link**만 하고 생성/추출은 하지 않는다. | `drudge/src/ingest.rs:130-200`<br>`drudge/src/frontmatter.rs:29-37`<br>`drudge/src/graph.rs`<br>`drudge/CLAUDE.md:18-28` | `ingest.rs`에 LLM 호출이 새로 들어가지 않는가? |
| **D3** | **Write door gated / read door open** | 쓰기는 세션 종료 → `distill` → `remember`로 게이트를 두고 LLM 정제를 거친다. 읽기는 LLM 없이 vault/wiki를 직접 읽어 빠르게 응답한다. | `README.md:76-99`<br>`drudge/src/serve.rs` MCP `remember/forget/recall`<br>`agents/claude-code/distill-session.py` | `recall`/`ask`가 vault 직접 읽기를 유지하는가? |
| **D4** | **Secret scrub = 단일 leak boundary** | git-tracked vault에 들어가기 전, 그리고 query_log에 쓰이기 전 regex로 토큰/키를 scrub한다. "원인은 외부 입력"이라 가정하고 쓰기 경계에서 한 번에 정규화한다. | `drudge/src/redact.rs`<br>`drudge/src/store.rs:787-823`<br>`drudge/CLAUDE.md:36-37` | 새 write path마다 `scrub()`가 호출되는가? |
| **D5** | **Claim 기반 temporal fact authority (supersede)** | `(subject, predicate)`는 시간축으로 버전화한다. 새 값이 들어오면 기존 row를 `superseded_at`로 봉인하고 최신값만 current. | `drudge/src/store.rs:500-617`<br>`drudge/src/frontmatter.rs:40-47`<br>`drudge/src/ask.rs:126-153` | claim upsert 순서(seal → insert)가 유지되는가? |
| **D6** | **No-panic / ROP / Layer 1>2>3** | 실패는 `Result` rail. `unwrap/expect/panic/todo` 금지. 철학 충돌 시 Layer 1(정직) → Layer 2(흐름) → Layer 3(절제) 우선순위. | `drudge/PHILOSOPHY.md`<br>`drudge/RUST-STYLE.md`<br>`drudge/ENFORCEMENT.md`<br>`drudge/Cargo.toml:41-63` | `[lints]`가 완화되지 않는가? |
| **D7** | **Vault/wiki가 SSOT, DB는 rebuildable 파생물** | 진짜 기억은 markdown 파일 + git history. DB는 검색용 인덱스. `sync`로 재구축, `renumber`는 파일만 다루고 DB는 sync로 다시 맞춘다. | `drudge/src/renumber.rs:1-14`<br>`drudge/src/ingest.rs:285-287`<br>`drudge/src/vault.rs:258-270`<br>`README.md:38-40` | 파일 기반 도구가 DB에 의존하지 않는가? |

---

## Layer priority quick reference

`drudge/PHILOSOPHY.md`의 충돌 해결 순서다. 설계 갈등이 생기면 다음 순서로 판단한다.

1. **Layer 1 — Honesty**: "Does this code lie?" 타입/상태가 거짓말을 하지 않는가?
2. **Layer 2 — Flow**: "Is the flow one-directional?" 데이터가 한 방향으로 흐르는가?
3. **Layer 3 — Restraint**: "Who would miss this structure?" 불필요한 추상화는 없는가?

자세한 휴리스틱은 `.agents/skills/ohmyboring/SKILL.md`를 참조한다.

---

## Change log

| 날짜 | 변경 내용 | Decision note |
|---|---|---|
| 2026-06-25 | 최초 작성, 7가지 결정 정리 | `vault/wiki/wiki-0100.md` |
