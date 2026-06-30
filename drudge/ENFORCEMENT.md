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
> The **Where** column points to the primary code location(s) implementing each rule.

| Rule (source) | Enforcement | Where | Notes |
|---|---|---|---|
| **§0 Rust official** (fmt · clippy · RFC430 naming · C-CONV `as_/to_/into_` · C-COMMON-TRAITS) | **fmt/clippy (machine)** + some naming/trait review | `drudge/Cargo.toml:41-63` · `scripts/guard.sh` | Shared contract — no need to memorize. *Not my philosophy* |
| ADT (enum > bool) + match exhaustive (§A) | **compiler** (non-exhaustive = error) + review (enum design) | `drudge/src/config.rs` · `drudge/src/frontmatter.rs` · `drudge/src/vault.rs` | `_` papering-over = review (option `wildcard_enum_match_arm`) |
| Errors are ADTs — `thiserror` (code branches) / `anyhow` (main confluence only) (§A) | **review** | `drudge/src/store.rs` · `drudge/src/llm.rs` · `drudge/src/ingest.rs` | anyhow domain leak = Layer 1 violation |
| Parse-Don't-Validate / boundary (§A) | **compiler (private fields)** + review | `drudge/src/frontmatter.rs` · `drudge/src/config.rs` · `drudge/src/redact.rs` | the type is the proof |
| no unwrap/expect/panic/todo/unimplemented/unreachable (§B flow) | **clippy restriction (deny)** | `drudge/Cargo.toml:58-63` | Cargo `[lints]` SSOT |
| `unsafe` forbidden | **`unsafe_code="forbid"`** | `drudge/Cargo.toml:42` | compiler |
| one-way flow · linearity · DIP · SRP · restraint (rule-of-three · first principles) (§B) | **review** | `drudge/src/ingest.rs` · `drudge/src/retrieve.rs` · `drudge/src/ask.rs` · `drudge/src/serve.rs` | design-level — includes trait restraint |
| fail-fast · ROP · idiomatic absorption (§C my vocabulary) | no-panic via clippy / the rest **review** | all `drudge/src/*.rs` | functional renaming (King · Wlaschin) |
| borrow-first / `&str`·`&[T]` · clone restraint | clippy `ptr_arg` in part + **review** | `drudge/src/retrieve.rs` · `drudge/src/ingest.rs` | partly mechanical |
| pedantic idioms (semicolon · needless, etc.) | **clippy pedantic (deny)** | `drudge/Cargo.toml:45-46` | nursery excluded (time bomb) |
| formatting | **`cargo fmt --check`** (pre-commit) | `scripts/guard.sh` · `.pre-commit-config.yaml` | — |
| behavioral non-regression (recall/answer quality) | **eval-gate.sh** (`data/eval/run_eval.py`) — **CI-enforced** (`eval-gate` job, offline recorded-embedding replay) + local `make eval` on a live stack | `data/eval/run_eval.py` · `scripts/eval-gate.sh` · `.github/workflows/ci.yml` | Recall@3 target = 1.00 on `data/eval/golden.json` |
| release acceptance drift (MCP contract ↔ docs ↔ removed danger surface) | **quality-gate** (`make quality`) — stack-free, CI-enforced | `drudge/src/serve/mcp.rs` tests · `Makefile` · `.github/workflows/ci.yml` | Fails if tool inventory/vector-mode docs drift or the removed `renumber` surface returns |
| `--no-verify` bypass | **forbidden (policy)** | `.pre-commit-config.yaml` | on failure, fix the root cause |

### Cross-reference to design decisions

| Decision | Primary modules |
|---|---|
| **D1** Wiki-first, pgvector optional | `drudge/src/wiki_recall.rs` · `drudge/src/main.rs` |
| **D2** Deterministic graph, no LLM extraction in kernel | `drudge/src/ingest.rs` · `drudge/src/graph.rs` · `drudge/src/frontmatter.rs` |
| **D3** Write door gated / read door open | `drudge/src/serve.rs` · `agents/claude-code/distill-session.py` |
| **D4** Secret scrub = single leak boundary | `drudge/src/redact.rs` · `drudge/src/store.rs:787-823` |
| **D5** Claim temporal authority (supersede) | `drudge/src/store.rs:500-617` · `drudge/src/ask.rs:126-153` |
| **D6** No-panic / ROP / Layer 1>2>3 | `drudge/PHILOSOPHY.md` · `drudge/Cargo.toml:41-63` |
| **D7** Vault/wiki SSOT, stable wiki IDs, DB rebuildable | `drudge/src/vault.rs` · `drudge/src/vault/remember.rs` · `drudge/src/ingest.rs:285-287` |

**Honest disclosure**: §0 (official) + §B's no-panic + formatting + behavioral regression are blocked by the *machine*. **The design-level §A·§B·§C (ADT · error-ADT · PDV · DIP · restraint · ROP) are blocked by review** — that the machine can't catch them is not a defect but *design*. The three when-stuck questions (Layer 1 > Layer 2 > Layer 3) are the review checklist.

**§E no guessing**: if naming/APIs are unclear, *no guessing from memory* — check the Rust API Guidelines / std, and if you can't, say so and stay conservative. (On conflict, naming/style is won by the §0 official source, design philosophy by §C this document.)

## Gates
- **pre-commit** = `scripts/guard.sh` (stack-free): `cargo fmt --check` → `cargo clippy --all-targets -D warnings` → `cargo test`. Install: `git config core.hooksPath .githooks`.
- **release acceptance / CI** = `make quality`: MCP tool inventory, vector-mode support docs, and removed dangerous CLI surfaces stay synchronized.
- **pre-deploy / CI** = `scripts/eval-gate.sh`: confirm drudge is up → copy `data/eval/fixtures/` into the vault → `/sync` → `run_eval.py` recall floor. The harness is committed (`data/eval/run_eval.py` + `golden.json` + 7 fixtures). CI runs it without a GPU via `data/eval/stub_embedder.py` (replays real bge-m3 vectors recorded by `record_embeddings.py` into `recorded_embeddings.json`) — so CI recall == real recall. Recall@3 < 1.00 → non-zero → merge/deploy halted. Locally, `make eval` runs the same gate against a live LLM.
