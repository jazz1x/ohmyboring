# 강제 매핑 — 철학을 *무엇이* 지키는가

> `PHILOSOPHY.md`(왜) · `RUST-STYLE.md`(어떻게)의 각 규칙을 **어떤 메커니즘이 강제**하는지.
> 원칙: 사용자 작성법대로 "위반을 **컴파일러/clippy/리뷰로** 잡을 수 있는 것만" 규칙화.
> 메타: 가드레일도 3겹(최소 구조로 최대 거짓말 차단) — clippy가 최대 레버리지, 그 위 단일 게이트만.

## 3단 강제
```
① 기계적   컴파일러 · clippy(-D warnings) · rustfmt   ← 자동, 0 비용
② 게이트   pre-commit(guard.sh) · pre-deploy(eval-gate.sh)   ← 커밋/배포 차단
③ 리뷰     "이 코드가 거짓말하는가?" (PR/self-audit)   ← 설계급(②로 못 잡음)
```

## 규칙 → 강제수단
> 섹션은 RUST-STYLE 구조: **§0**(러스트 공식) · **§A**(ADT·에러ADT·PDV) · **§B**(흐름·DIP·절제) · **§C**(내 어휘: fail-fast·ROP) · **§E**(공식출처).

| 규칙(출처) | 강제 | 비고 |
|---|---|---|
| **§0 러스트 공식**(fmt·clippy·RFC430 명명·C-CONV `as_/to_/into_`·C-COMMON-TRAITS) | **fmt/clippy(기계)** + 명명·트레잇 일부 리뷰 | 공유계약 — 외울 필요 X. *내 철학 아님* |
| ADT(enum>bool) + match exhaustive (§A) | **컴파일러**(non-exhaustive=에러) + 리뷰(enum 설계) | `_` 뭉개기 = 리뷰(옵션 `wildcard_enum_match_arm`) |
| 에러는 ADT — `thiserror`(코드가 분기) / `anyhow`(main 합류만) (§A) | **리뷰** | anyhow 도메인 누수 = 1겹 위반 |
| Parse-Don't-Validate / 경계 (§A) | **컴파일러(private 필드)** + 리뷰 | 타입이 증거 |
| no unwrap/expect/panic/todo/unimplemented/unreachable (§B 흐름) | **clippy restriction(deny)** | Cargo `[lints]` SSOT |
| `unsafe` 금지 | **`unsafe_code="forbid"`** | 컴파일러 |
| 흐름 한방향·선형성·DIP·SRP·절제(rule-of-three·제1원칙) (§B) | **리뷰** | 설계급 — trait 절제 포함 |
| fail-fast·ROP·관용적흡수 (§C 내 어휘) | no-panic은 clippy / 나머지 **리뷰** | 함수형 재명명(King·Wlaschin) |
| 빌림 우선/&str·&[T]·clone 절제 | clippy `ptr_arg` 일부 + **리뷰** | 부분 기계 |
| pedantic 관용구(semicolon·needless 등) | **clippy pedantic(deny)** | nursery 제외(시한폭탄) |
| 형식 | **`cargo fmt --check`**(pre-commit) | — |
| 동작 무회귀(회수/답변 품질) | **eval-gate.sh**(`run_eval --check`) | recall@1≥.80·MRR≥.85·kw≥.90 |
| `--no-verify` 우회 | **금지(정책)** | 실패 시 근본원인 픽스 |

**정직 고지**: §0(공식)+§B의 no-panic+형식+동작회귀는 *기계*가 막는다. **§A·§B·§C 설계급(ADT·에러ADT·PDV·DIP·절제·ROP)은 리뷰**가 막는다 — 기계로 못 잡는 게 결함이 아니라 *설계*다. 막힘질문 3개(1겹>2겹>3겹)가 리뷰 체크리스트.

**§E 추측 금지**: 명명·API가 헷갈리면 *기억 추측 금지* — Rust API Guidelines / std 확인, 못 하면 밝히고 보수적. (충돌 시 명명·스타일은 §0 공식이, 설계철학은 §C 이 문서가 이김.)

## 게이트
- **pre-commit** = `scripts/guard.sh` (스택-프리): `cargo fmt --check` → `cargo clippy --all-targets -D warnings` → `cargo test`. 설치: `git config core.hooksPath .githooks`.
- **pre-deploy** = `scripts/eval-gate.sh` (스택 필요): drudge 가동 확인 → `run_eval --check` 바닥선. 미달 시 비0 → 배포 중단.
