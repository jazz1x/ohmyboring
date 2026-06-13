# Rust Agent Principles

> Rules to follow when writing code. Not maxims — only what can have its violation caught by compiler/review.
> The clearer the criterion, the better it's followed. When ambiguous, lean toward the simpler side.

---

## A. Rust Idioms (the ones often violated)

- [ ] No `unwrap()` / `expect()` / `panic!` (except prototypes and tests). Propagate errors via `Result` + `?`.
- [ ] No `clone()` overuse. First see whether borrowing (`&`, `&mut`) works. Clone only when intentional.
- [ ] Instead of imperative loops + `mut` accumulation, use iterator chains (`map`/`filter`/`fold`/`collect`).
- [ ] `match` exhaustively. Don't paper over cases with the `_ =>` wildcard — adding a new variant should be caught as a compile error.
- [ ] Error types: library → `thiserror`, binary → `anyhow`.
- [ ] Arguments as borrowed slices: `&str` (not `String`), `&[T]` (not `Vec<T>`).
- [ ] Keep traits thin. One trait = one capability.

---

## B. Core Design Philosophy (translated into Rust machinery)

### ADT — Make Impossible States Unrepresentable
- [ ] Don't represent state with multiple bool flags. Use an `enum`.
- [ ] "make illegal states unrepresentable" — invalid combinations can't even be constructed as a type.

### Parse, Don't Validate
- [ ] External input is parsed into a newtype **once at the boundary**. (= fail-fast: invalid input is rejected the moment it enters.)
- [ ] Flow validated types (`Email`, `NonEmptyVec`, `Positive`) inward.
- [ ] Constructor private, only `parse()` public.
- [ ] If you see a `validate()` that checks the same value twice, the design has failed.
- [ ] **The purpose is "the type is the proof"**: a function that receives a validated type does not re-check inside. The type already guarantees it.
- [ ] **The execution method = fail-fast (`?`), the purpose = the type is the proof (newtype).** Both are needed to complete PDV.

### ROP (Railway Oriented)
- [ ] Functions return `Result<T, E>`. Two tracks: success/failure.
- [ ] Connect with `?`. Automatic error conversion via `From`. → `?` is the syntactic implementation of fail-fast.
- [ ] **fail-fast is the default.** Don't drag an invalid state along.
- [ ] **Exception: at UX boundaries (form input, etc.), accumulate errors.** Bouncing on the first failure annoys the user. Since multiple failures must be shown at once, use the `Validated` pattern (this is the only place `?` can't be used).
- [ ] If a failure is not an "error" but a "default/branch," use `.unwrap_or` / `match` instead of `?`.

### SRP — Single Responsibility
- [ ] One function = one responsibility.
- [ ] Separate I/O from pure logic. Pure functions take args and return values (easy to test); side effects live in a thin outer shell.

### Linearity — One-Way Flow
- [ ] Data flow goes one direction, top → bottom. No callback hell / cyclic dependencies.
- [ ] Straight-line it with `let` steps or method chains.
- [ ] An ownership move is itself a kind of linear type — exploit the fact that a value used once can't be reused.

---

## C. Universal Principles (how they land in Rust)

### Dependency Inversion (the heart of Clean Architecture)
- [ ] Keep domain logic from depending on concrete implementations (DB, HTTP). Depend on a trait; inject the implementation from outside.
- [ ] → Smith Hub already has this structure.

### Boundary
- [ ] A parsing boundary between the outside world (file · network · input) and the domain.
- [ ] Inside, only validated types ever circulate.

### First Principles
- [ ] "Is this abstraction really needed? Does a simpler representation guarantee the same invariant?"
- [ ] Beware over-abstraction — don't build with triple-nested generics what a single trait layer suffices for.

### Clean Code — but No Zealotry
- [ ] Short functions, clear names: good.
- [ ] DRY zealotry: warning. Sometimes duplication beats premature abstraction (when lifetimes tangle, the abstraction cost is high).
- [ ] Abstraction starts being considered at the **third duplication** (rule of three).

---

## One-Line Criterion

> Does this code lie? Is this abstraction honest? Does this boundary leak?
> If all three are no, it passes. If in doubt, make it simpler.

---

## Enforcement Mapping — How It Gets Caught
| Principle | How it's caught |
|---|---|
| no unwrap/expect/panic | clippy `unwrap_used`/`expect_used`/`panic` = deny (allow in tests) |
| match wildcard forbidden | clippy `wildcard_enum_match_arm` / new variant → compile error |
| clone overuse | clippy `clippy::clone_on_*`, review |
| iterator chain / `&str`·`&[T]` args | clippy pedantic (`needless_collect`, `ptr_arg`, etc.) |
| ADT · PDV · SRP · linearity · DIP | review (adversarial) — verify the proof via the type signature |

Gate: `cargo clippy --all-targets -- -D warnings` (unsafe forbid + all/pedantic/nursery deny + the restrictions above).
