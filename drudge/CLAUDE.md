# drudge — Rust Guardrails

> **Binding Bible (the designer of integrity):**
> - [`PHILOSOPHY.md`](./PHILOSOPHY.md) — *why* (3 layers: epistemology/aesthetics/ethics, priority Layer 1 > 2 > 3)
> - [`RUST-STYLE.md`](./RUST-STYLE.md) — *how* (A idioms / B design implementation / C universal / D tooling / E prohibitions)
> - [`ENFORCEMENT.md`](./ENFORCEMENT.md) — *what enforces it* (clippy/fmt/pre-commit/eval-gate/review mapping)
>
> When-stuck questions: **"Does this code lie?" (Layer 1) · "Is the flow one-directional?" (Layer 2) · "Who would miss this structure if it weren't there?" (Layer 3).**
> ([`PRINCIPLES.md`](./PRINCIPLES.md) is the earlier summary — the three documents above are the SSOT.) Below are supplementary guardrails.

## Enforcement (gated in code — Cargo.toml `[lints]` is the SSOT)
- `unsafe_code = "forbid"` · clippy `all` / `pedantic` / `nursery` = **deny**.
  → **If `cargo clippy -- -D warnings` doesn't pass, no merging.** For new code this gate is the default.
- edition **2024** · toolchain **stable** + clippy + rustfmt pinned (`rust-toolchain.toml`).
- Code/file search via `rg` / `fd`. No `grep -r` / `find -name`.
- Bypassing the pre-commit hook (`git commit --no-verify`) is **absolutely forbidden** — on failure, fix the root cause (lint/format).

## Philosophy (principles)
- **ROP** (Wlaschin): fallible goes on the `Result` rail. Structured error types with `thiserror`.
  No silent fallback · *defensive* timeout (error concealment of the `{timeout:200}` kind) · throw disguised as an early-return.
  But an **I/O-boundary timeout** (network / subprocess) is *justified* as a graceful boundary — its purpose is to prevent an infinite hang; don't remove it.
- **Parse, don't validate** (Alexis King): raw input is parsed into a typed form once at the boundary, then trusted as-is.
  No hardcoding of version-specific field names / schema versions (SSOT).
- **Use the simplest thing that works** (Karpathy): at small scale, simplicity > excessive abstraction.
  Don't escalate before the necessary trigger (corpus size · insufficient accuracy).
- **Clean Architecture**: the dependency arrow always points outer → inner.
  `store` / `ollama` (adapter · framework) → `ingest` / `retrieve` (use case) → `main`/CLI (interface).
  However, `ingest`/`retrieve`/`extract` (use case) currently reference the concrete type `store::Store` directly.
  This is intentional design — backend replacement does not meet the rule-of-three (repeated 3+ times) threshold,
  so the cost of trait abstraction was judged to exceed its benefit (§C first principles / rule-of-three).
- **Composition over duplication** · **avoid half-done state** (a scope that can be finished in one unit of work) ·
  **check existing assets before inventing** (search first with `rg`/`fd`, create new only when absent).
- **Tests = guardrails**: "*Which* guardrail does this test own?" If you can't answer, don't write it. Mindless additions = noise.

## deps Discipline
- Prefer pinned versions. Adding a new dep = a supply-chain check (maintainer / source / license / download count).
- `unsafe` 0. Isolate only external I/O (reqwest / tokio-postgres) behind a graceful boundary.

## Layers (current)
```
main.rs (interface)  →  retrieve / ingest (use case, planned)  →  store / ollama (adapter)
                                                              ↘  frontmatter (entity, planned)
```
SSOT separation: store = persistence · search, ollama = embedding · generation, frontmatter = document schema, retrieve = recall pipeline.
