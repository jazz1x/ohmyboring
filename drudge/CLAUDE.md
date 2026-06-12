# drudge — Rust 가드레일

> **바인딩 바이블 (정합성의 설계자):**
> - [`PHILOSOPHY.md`](./PHILOSOPHY.md) — *왜* (3겹: 인식론/미학/윤리, 우선순위 1>2>3겹)
> - [`RUST-STYLE.md`](./RUST-STYLE.md) — *어떻게* (A 관용구 / B 설계구현 / C 보편 / D 도구 / E 금지)
> - [`ENFORCEMENT.md`](./ENFORCEMENT.md) — *무엇이 강제하나* (clippy/fmt/pre-commit/eval-gate/리뷰 매핑)
>
> 막힘질문: **"이 코드가 거짓말하는가?"(1겹) · "흐름이 한 방향인가?"(2겹) · "이 구조 없으면 누가 아쉬운가?"(3겹).**
> ([`PRINCIPLES.md`](./PRINCIPLES.md) 는 이전 요약 — 위 3문서가 SSOT.) 아래는 보조 가드레일.

## 강제 (코드로 게이트 — Cargo.toml `[lints]` 가 SSOT)
- `unsafe_code = "forbid"` · clippy `all` / `pedantic` / `nursery` = **deny**.
  → **`cargo clippy -- -D warnings` 통과 못 하면 머지 금지.** 새 코드는 이 게이트가 기본값.
- edition **2024** · toolchain **stable** + clippy + rustfmt 고정(`rust-toolchain.toml`).
- 코드/파일 검색은 `rg` / `fd`. `grep -r` / `find -name` 금지.
- pre-commit hook 우회(`git commit --no-verify`) **절대 금지** — 실패 시 근본원인(lint/format) 픽스.

## 철학 (원칙)
- **ROP** (Wlaschin): fallible 은 `Result` 레일. `thiserror` 로 구조화된 에러 타입.
  침묵 fallback · *defensive* timeout(`{timeout:200}` 식 에러 은폐) · early-return 위장 throw 금지.
  단 **IO 경계 timeout**(network / subprocess)은 graceful boundary 로 *정당* — 무한 hang 방지가 목적, 제거 금지.
- **Parse, don't validate** (Alexis King): raw 입력은 경계에서 1회 typed 로 parse 후 그대로 신뢰.
  버전별 필드명 / 스키마 버전 하드코딩 금지(SSOT).
- **Use the simplest thing that works** (Karpathy): 작은 규모면 단순함 > 과한 추상.
  필요 trigger(코퍼스 크기·정확도 부족) 전엔 escalate 안 함.
- **Clean Architecture**: 의존성 화살표 항상 outer → inner.
  `store` / `ollama`(어댑터·framework) → `ingest` / `retrieve`(use case) → `main`/CLI(interface).
  단, `ingest`/`retrieve`/`extract`(use case)는 현재 구체 타입 `store::Store`를 직접 참조한다.
  이는 의도적 설계 — 백엔드 교체가 rule-of-three(3회 이상 반복) 기준을 충족하지 않아
  trait 추상화 비용이 이득을 초과한다고 판단했다(§C 제1원칙/rule-of-three).
- **Composition over duplication** · **반쪽 상태 회피**(한 작업 단위로 완주 가능한 scope) ·
  **발명 전 기존 자산 확인**(`rg`/`fd` 로 먼저 찾고 없을 때만 신설).
- **테스트 = 가드레일**: "이 테스트가 *어느* 가드레일을 책임지나?" 답 못 하면 쓰지 말 것. 무지성 추가 = 노이즈.

## deps 규율
- 버전 pin 우선. 새 dep 추가 = 공급망 점검(maintainer / source / license / 다운로드수).
- `unsafe` 0. 외부 IO(reqwest / tokio-postgres)만 graceful boundary 로 격리.

## 레이어 (현재)
```
main.rs (interface)  →  retrieve / ingest (use case, 예정)  →  store / ollama (adapter)
                                                              ↘  frontmatter (entity, 예정)
```
SSOT 분리: store=영속·검색, ollama=임베딩·생성, frontmatter=문서 스키마, retrieve=회수 파이프.
