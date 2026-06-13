# Enforcement Mapping — *What* Upholds the Philosophy

> **Which mechanism enforces** each rule from `PHILOSOPHY.md` (why) and `RUST-STYLE.md` (how).
> Principle: per the user's writing rules, only turn into a rule what can have its "violation **caught by compiler/clippy/review**."
> Meta: the guardrails are themselves 3-layer (block the most lies with the least structure) — clippy is maximum leverage, with only a single gate on top of it.

## Three-Stage Enforcement
```
① Mechanical   compiler · clippy(-D warnings) · rustfmt   ← automatic, 0 cost
② Gate         pre-commit(guard.sh) · pre-deploy(eval-gate.sh)   ← blocks commit/deploy
③ Review       "Does this code lie?" (PR/self-audit)   ← design-level (② can't catch it)
```

## Rule → Enforcement Mechanism
> Sections follow the RUST-STYLE structure: **§0** (Rust official) · **§A** (ADT · error-ADT · PDV) · **§B** (flow · DIP · restraint) · **§C** (my vocabulary: fail-fast · ROP) · **§E** (official sources).

| Rule (source) | Enforcement | Notes |
|---|---|---|
| **§0 Rust official** (fmt · clippy · RFC430 naming · C-CONV `as_/to_/into_` · C-COMMON-TRAITS) | **fmt/clippy (machine)** + some naming/trait review | Shared contract — no need to memorize. *Not my philosophy* |
| ADT (enum > bool) + match exhaustive (§A) | **compiler** (non-exhaustive = error) + review (enum design) | `_` papering-over = review (option `wildcard_enum_match_arm`) |
| Errors are ADTs — `thiserror` (code branches) / `anyhow` (main confluence only) (§A) | **review** | anyhow domain leak = Layer 1 violation |
| Parse-Don't-Validate / boundary (§A) | **compiler (private fields)** + review | the type is the proof |
| no unwrap/expect/panic/todo/unimplemented/unreachable (§B flow) | **clippy restriction (deny)** | Cargo `[lints]` SSOT |
| `unsafe` forbidden | **`unsafe_code="forbid"`** | compiler |
| one-way flow · linearity · DIP · SRP · restraint (rule-of-three · first principles) (§B) | **review** | design-level — includes trait restraint |
| fail-fast · ROP · idiomatic absorption (§C my vocabulary) | no-panic via clippy / the rest **review** | functional renaming (King · Wlaschin) |
| borrow-first / `&str`·`&[T]` · clone restraint | clippy `ptr_arg` in part + **review** | partly mechanical |
| pedantic idioms (semicolon · needless, etc.) | **clippy pedantic (deny)** | nursery excluded (time bomb) |
| formatting | **`cargo fmt --check`** (pre-commit) | — |
| behavioral non-regression (recall/answer quality) | **eval-gate.sh** (`run_eval --check`) | recall@1≥.80 · MRR≥.85 · kw≥.90 |
| `--no-verify` bypass | **forbidden (policy)** | on failure, fix the root cause |

**Honest disclosure**: §0 (official) + §B's no-panic + formatting + behavioral regression are blocked by the *machine*. **The design-level §A·§B·§C (ADT · error-ADT · PDV · DIP · restraint · ROP) are blocked by review** — that the machine can't catch them is not a defect but *design*. The three when-stuck questions (Layer 1 > Layer 2 > Layer 3) are the review checklist.

**§E no guessing**: if naming/APIs are unclear, *no guessing from memory* — check the Rust API Guidelines / std, and if you can't, say so and stay conservative. (On conflict, naming/style is won by the §0 official source, design philosophy by §C this document.)

## Gates
- **pre-commit** = `scripts/guard.sh` (stack-free): `cargo fmt --check` → `cargo clippy --all-targets -D warnings` → `cargo test`. Install: `git config core.hooksPath .githooks`.
- **pre-deploy** = `scripts/eval-gate.sh` (needs the stack): confirm drudge is up → `run_eval --check` floor. On failure, non-zero → deploy halted.
