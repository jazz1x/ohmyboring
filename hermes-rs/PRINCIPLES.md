# Rust Agent Principles

> 코드를 쓸 때 따를 규칙. 격언이 아니라 위반을 컴파일러/리뷰로 잡을 수 있는 것만.
> 판단 기준이 명확할수록 잘 따른다. 애매하면 더 단순한 쪽으로.

---

## A. 러스트 관용구 (자주 어기는 것)

- [ ] `unwrap()` / `expect()` / `panic!` 금지 (프로토타입·테스트 제외). 에러는 `Result` + `?`로 전파.
- [ ] `clone()` 남발 금지. 빌림(`&`, `&mut`)으로 되는지 먼저. clone은 의도적일 때만.
- [ ] 명령형 루프 + `mut` 누적 대신 이터레이터 체인(`map`/`filter`/`fold`/`collect`).
- [ ] `match`는 exhaustive하게. `_ =>` 와일드카드로 케이스 뭉개지 말 것 — 새 variant 추가 시 컴파일 에러로 잡혀야 한다.
- [ ] 에러 타입: 라이브러리 → `thiserror`, 바이너리 → `anyhow`.
- [ ] 인자는 빌린 슬라이스로: `&str`(not `String`), `&[T]`(not `Vec<T>`).
- [ ] trait은 얇게. 한 trait = 한 능력.

---

## B. 핵심 설계 철학 (러스트 기계장치로 번역)

### ADT — 불가능한 상태를 표현 불가능하게
- [ ] 상태를 bool 플래그 여러 개로 표현하지 말 것. `enum`으로.
- [ ] "make illegal states unrepresentable" — 잘못된 조합이 아예 타입으로 안 만들어지게.

### Parse, Don't Validate
- [ ] 외부 입력은 **경계에서 1회** newtype으로 파싱. (= fail-fast: 잘못된 입력은 들어오는 순간 거부)
- [ ] 검증된 타입(`Email`, `NonEmptyVec`, `Positive`)을 내부로 흘린다.
- [ ] 생성자는 private, `parse()`만 public.
- [ ] 같은 값을 두 번 검사하는 `validate()`가 보이면 설계 실패.
- [ ] **목적은 "타입이 증거"**: 검증된 타입을 받은 함수는 안에서 재검사하지 않는다. 타입이 이미 보장하므로.
- [ ] **실행 방식 = fail-fast(`?`), 목적 = 타입이 증거(newtype).** 둘 다 있어야 PDV 완성.

### ROP (Railway Oriented)
- [ ] 함수는 `Result<T, E>` 반환. 성공/실패 두 트랙.
- [ ] `?`로 연결. `From`으로 에러 변환 자동. → `?`가 fail-fast의 문법적 구현체.
- [ ] **fail-fast가 기본값.** 잘못된 상태를 끌고 가지 않는다.
- [ ] **예외: UX 경계(폼 입력 등)에서는 에러 누적.** 첫 실패에 튕기면 사용자가 짜증. 여러 실패를 한 번에 모아 보여줘야 하므로 `Validated` 패턴 (여기만 `?` 못 씀).
- [ ] 실패가 "에러"가 아니라 "기본값/분기"면 `?`가 아니라 `.unwrap_or` / `match`.

### SRP — 단일 책임
- [ ] 한 함수 = 한 책임.
- [ ] I/O와 순수 로직 분리. 순수 함수는 인자 받아 값 반환(테스트 쉬움), 부수효과는 바깥 얇은 껍질로.

### 선형성 — 한 방향 흐름
- [ ] 데이터 흐름은 위→아래 한 방향. 콜백 지옥/순환 의존 금지.
- [ ] `let` 단계 또는 메서드 체인으로 직선.
- [ ] 소유권 move 자체가 선형 타입의 일종 — 한 번 쓴 값 재사용 막힘을 활용.

---

## C. 보편 원칙 (러스트에 어떻게 떨어지는가)

### 의존성 역전 (클린아키텍처 핵심)
- [ ] 도메인 로직이 구체 구현(DB, HTTP)에 의존하지 않게. trait에 의존, 구현은 바깥에서 주입.
- [ ] → Smith Hub가 이미 이 구조.

### 경계 (Boundary)
- [ ] 외부 세계(파일·네트워크·입력)와 도메인 사이에 파싱 경계.
- [ ] 안쪽은 항상 검증된 타입만 돈다.

### 제1원칙
- [ ] "이 추상화가 정말 필요한가? 더 단순한 표현이 같은 불변식을 보장하나?"
- [ ] 과잉 추상화 경계 — trait 한 겹으로 충분한 걸 제네릭 3중첩으로 만들지 말 것.

### 클린코드 — 단, 광신 금지
- [ ] 함수 짧게·이름 명확히: 좋다.
- [ ] DRY 광신: 경고. 성급한 추상화보다 중복이 나을 때가 있다 (라이프타임 얽히면 추상화 비용이 큼).
- [ ] 추상화는 **세 번째 중복**에서 고민 시작 (rule of three).

---

## 한 줄 기준

> 이 코드가 거짓말을 하는가? 이 추상화가 정직한가? 이 경계가 새는가?
> 셋 다 아니오면 통과. 의심되면 더 단순하게.

---

## 강제(enforcement) 매핑 — 어떻게 잡히나
| 원칙 | 잡는 법 |
|---|---|
| no unwrap/expect/panic | clippy `unwrap_used`/`expect_used`/`panic` = deny (test는 allow) |
| match 와일드카드 금지 | clippy `wildcard_enum_match_arm` / 신규 variant → 컴파일 에러 |
| clone 남발 | clippy `clippy::clone_on_*`, 리뷰 |
| iterator 체인 / `&str`·`&[T]` 인자 | clippy pedantic (`needless_collect`, `ptr_arg` 등) |
| ADT·PDV·SRP·선형성·DIP | 리뷰(adversarial) — 타입 시그니처로 증거 확인 |

게이트: `cargo clippy --all-targets -- -D warnings` (unsafe forbid + all/pedantic/nursery deny + 위 restriction).
