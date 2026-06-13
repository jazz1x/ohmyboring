# How to Write Rust

> The "why" → `PHILOSOPHY.md`. This document is the **"how."**
> Examples are deliberately omitted — an example makes you copy-paste one shape and stops your thinking.
> Rules force thinking. When stuck → look at the official source in E.
> For what the *machine* catches versus what *review* catches → `ENFORCEMENT.md`.

It splits into three areas. **What doesn't overlap is short; what overlaps is the meat.**

- **Rust only** (enforced by tools) → Section 0. No need to memorize.
- **Me only** (the functional vocabulary Rust doesn't push) → C. My identity.
- **The intersection** (what both Rust and I push) → A·B. **The core of this document.**

---

## 0. The Rust Official Stuff (enforced by tools — the less the better)

Unrelated to my philosophy. `cargo fmt` + `clippy` catch it mechanically, so I don't memorize it.

- [ ] `cargo fmt` passing is a precondition. No formatting debates.
- [ ] `cargo clippy` warnings are to be **read and judged**. When ignoring one, state the reason explicitly via `#[allow(...)]` (no blind zero-warning obsession).
- [ ] Naming, conversion methods (`as_`/`to_`/`into_`), and trait implementations follow the official guidelines → E.

---

## A. The Intersection — What Both Rust and I Push [the core of this document]

This is where "my philosophy = Rust mainstream" meets. Highest confidence, look here first.

### ADT — Make Impossible States Unrepresentable [Layer 1]
- [ ] Don't represent state as a combination of bool flags. Close impossible combinations off in the type with an `enum`.
- [ ] Official Rust pattern names: **type-driven design / make illegal states unrepresentable.**
- [ ] `match` must be exhaustive. Don't paper over with `_ =>` — a new variant should be caught as a compile error.

### Errors Are ADTs [Layer 1]
- [ ] Choosing the error type comes down to **"who branches on this error"** (the common wisdom "library = thiserror / binary = anyhow" is imprecise):
  - *Code* splits failure modes with `match` → `thiserror` enum (the type is the proof)
  - *A human* reads it and that's the end (the outermost `main` confluence point, no further branching) → `anyhow` allowed (a region where Layer 1 doesn't apply)
  - If `anyhow` leaks into the domain/internals → **Layer 1 violation.** When in doubt, thiserror.
- [ ] Error conversion is a `From` impl → `?` converts automatically. (Without this, `?` won't compile — the most common sticking point.)

### Parse, Don't Validate [Layer 1]
- [ ] External input is parsed into a newtype **once at the boundary**. Constructor private, only `parse()` public.
- [ ] Flow validated types (`Email`, `NonEmpty`) inward. No re-checking inside — the type is already the proof.
- [ ] If you see a `validate()` that checks the same value twice, the design has failed.

---

## B. The Intersection — Flow and Restraint [riding Rust's grain]

### Flow Goes One Way [Layer 2]
- [ ] Data goes one direction, top → bottom. No callback hell, no cyclic dependencies.
- [ ] An ownership move is itself linear — exploit the fact that a value used once can't be reused.
- [ ] No `unwrap`/`expect`/`panic!` (except prototypes and tests). Failure flows via `Result`+`?`.
  - You have an `Option` but the function returns `Result` → `.ok_or(err)?`. (The reverse is `.ok()`)
  - If a failure is not an "error" but a "default/branch," use `.unwrap_or`/`match` instead of `?`.

### Dependency Inversion — but Restraint Prevents Overuse [Layer 2 × Layer 3]
- [ ] Keep the domain from depending on concrete implementations (DB·HTTP). Depend on a trait; inject the implementation.
- [ ] **A trait is not free.** Criterion: "Does another implementation actually exist (including a test mock)?"
  - Yes (I/O boundary: DB·HTTP·clock·file — needs test isolation) → trait
  - No (pure logic, a single implementation) → enum / direct call. Don't make a trait object.

### Restraint [Layer 3]
- [ ] Before abstracting, ask: "Does a simpler representation guarantee the same invariant?"
- [ ] Don't build with triple-nested generics what a single trait layer would do.
- [ ] Beware DRY zealotry. Abstraction starts being considered at the **third duplication** (rule of three).
- [ ] SRP: separate I/O from pure logic. Pure functions go args→value (easy to test); side effects live in a thin outer shell.

---

## C. My Own Vocabulary — What Rust Doesn't Push [my identity]

The concepts are the same as Rust mainstream, but the *names* I brought over from the functional camp (King·Wlaschin).
Don't get confused about "is this official?" — this is my renaming.

- **fail-fast** = the way PDV executes. `?` is its syntactic implementation. The default.
  - Exception: UX boundaries (form input) accumulate errors (`Validated`). Bouncing on the first failure annoys the user → this is the only place `?` can't be used.
- **ROP** = the two-track `Result` mindset. The success/failure railway.
- **Idiomatic absorption** = a shared `Monad` trait · HKT mimicry (`higher`) · transformer transplant — **none of these.**
  Without HKT they all stay PoC. Use the individual monad APIs (`map`/`and_then`/`?`/`flat_map`).
  → **Functional enough at the value level; give up generalization at the type/module level.**

---

## D. Common Sticking Points — Quick Answers

- No `|>` pipe → method chaining is the pipe. For free functions, `let` steps (not a hack, the default idiom). For frequently used conversions, methodize via an extension trait.
- Several `Result`s at once → `iter.collect::<Result<Vec<_>,E>>()`. Stops at the first error (= `traverse`).
- bind = `.and_then` (Option/Result) / `.flat_map` (Iterator). map = `.map`. failure conversion = `.map_err` (Result).
- If you genuinely need an immutable persistent data structure, `im`. (Mostly unnecessary.)
- **async** (I/O boundary): `?` propagation is the same. On a multithreaded runtime (tokio by default), an error crossing an await needs `Send` — if all fields are `Send`, the `thiserror` derive follows along (not an automatic guarantee; it hangs on the fields). For async methods in a trait, `async-trait` or RPITIT. Check the official docs for details → E.

---

## E. When Unsure, Look at the Official Source (no guessing)

Rule on conflict: **for naming/style/API design, the official source wins. For design philosophy (C), it's my choice, so this document wins.**

| When unsure | Source |
|---|---|
| naming · casing · conversion methods · trait implementations | https://rust-lang.github.io/api-guidelines/ (checklist: /checklist.html) |
| idiomatic patterns · anti-patterns | `cargo clippy` + https://rust-lang.github.io/rust-clippy/ |
| errors · Result · Option · match · iterators | The Book — https://doc.rust-lang.org/book/ |
| design patterns (functional · idioms) | https://rust-unofficial.github.io/patterns/ |
| exact standard-library signatures | https://doc.rust-lang.org/std/ |

**Don't guess naming/APIs from memory.** Verify, or if you can't, say so and stay conservative.

---

## When Stuck

> Layer 1 **"Does this code lie?"** > Layer 2 **"Is the flow one-directional?"** > Layer 3 **"Who would miss this structure if it weren't there?"**

Priority 1 > 2 > 3. The higher beats the lower.
