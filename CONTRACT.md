# Contract: claims anchor to the code they are about — step 1 of D2

Working directory: this worktree. Branch `feat/claims-anchor-to-the-code-they-are-about`, cut
from `origin/main`. Rust only, under `drudge/`. Do not touch `agents/`, `hooks/`, `scripts/`,
`docs/`. **Do not push, do not open a PR, do not run `make build`** — the owner is away until
2026-09-16 and nothing may reach the live engine; this branch is for review on return.

## Why (read `docs/PRD.md` §8 D2 in full first — it is the design, this file is the slice)

8,794 claims, 84% of `(subject, predicate)` pairs singletons: the claim graph is a list. D2's
answer is a code anchor per claim, attached by a deterministic resolver after distillation, not
by the LLM. D2 also says where the raw material is: `path:line` and `dir/file.ext` mentions in
the **note body** (16% / 34% of recent notes), not in claim rows (0.15%). This step builds the
schema, the T1 resolver, the inheritance to claims, the legacy era marker, and the adoption
counter. Stale detection (span hashes, `stale_at`) is step 2 — not here.

## What to build

### 1. Schema (in the `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE … ADD COLUMN IF NOT EXISTS` block)

- `claim.anchor text` — nullable, `project:path[:span]` string (`span` = `L10-L24` or absent).
- `claim.stale_at timestamptz` and `claim.stale_reason text` — nullable, **written by nothing in
  this step**; they exist so step 2 does not need a migration.
- `claim.era text NOT NULL DEFAULT 'pre-anchor'`. New claims written after this lands get
  `era = 'anchored'` when an anchor was attached and `'unanchored'` when the resolver found
  none. Existing rows keep the default — that is the D3 legacy marker, one migration, no value
  rewritten.
- Index on `claim(anchor)` where anchor is not null.

Keep `data/schema/baseline.sql` parity: run `make test-db-upgrade` (read the Makefile target)
and update the baseline the way that target's failure message tells you to.

### 2. T1 resolver — `anchor::from_note_body(project: &str, body: &str) -> Vec<Anchor>`

New module `drudge/src/anchor.rs`. Pure, no IO, unit-tested. Finds, in a note body:

- T1: `path:line` and `path:line-line` mentions — a token containing `/` or a known source
  extension, followed by `:` and digits (`agents/shared/uptake_core.py:283`,
  `drudge/src/store.rs:1212-1240`, backticked or not). Yields `Anchor { project, path, span:
  Some(Lstart-Lend) }`.
- T1b: bare `dir/file.ext` mentions with a source extension (`.rs .py .ts .tsx .js .go .sh .sql
  .toml .yaml .yml .md`) and at least one `/` — yields `span: None`.
- Paths are normalised: strip a leading `./`, strip a trailing punctuation, ignore URLs
  (`http`, `https`), ignore anything under `/tmp` or starting with `~`. Deduplicate, keep order
  of first appearance, cap at 20 per note.

The `project` is the note's project (frontmatter), passed in; the resolver does not guess it.

### 3. Inheritance to claims at ingest

Where claims are upserted from a note (`ingest.rs`, the `upsert_claim` path), attach an anchor:
the note's anchors, filtered to those whose path or file stem appears in the claim's `subject`
or `value`; if none matches, the note's **first** anchor. A note with no anchors leaves
`anchor` NULL and `era = 'unanchored'`. Do not call the LLM. Do not change what `subject`,
`predicate`, `value`, `kind`, `confidence` are.

### 4. `current_claims` filter

`current_claims` and the `claims` MCP tool gain an optional `anchor_path: Option<String>`
filter — rows whose `anchor` starts with `<project>:<anchor_path>` — and every response row
carries `anchor` and `era`. Default behaviour with the filter absent is unchanged. (The
`stale_at IS NULL` default filter is step 2, with the writer.)

### 5. Adoption counter

`corpus_status` (MCP) and `/audit` gain `claims_anchored`, `claims_unanchored`,
`claims_pre_anchor` — counts by `era`. D2: "the first commit carries an adoption counter"
because a supply-side feature nobody can see the uptake of is dead code at coverage ≈ 0.

### 6. Backfill for existing notes is NOT in this step

Legacy rows stay `pre-anchor`. A `drudge anchor-backfill` CLI is step 3, after the owner sees
step 1's counter on new notes.

## Tests

- `anchor.rs` unit tests: each T1/T1b shape above, URL and `/tmp` exclusion, dedup, cap, a
  Korean sentence with a backticked `path:line` inside it.
- Inheritance: a pure function `anchor_for_claim(note_anchors, subject, value) -> Option<Anchor>`
  with tests for the three cases (matching path, no match → first, no anchors → None).
- Schema: the existing store integration suite (`drudge/tests/store_integration.rs`, guard
  `BORING_TEST_DATABASE_URL`) gets a case that ingests a note whose body cites `a/b.rs:10` and
  asserts the claim row has `anchor = '<project>:a/b.rs:L10'` and `era = 'anchored'`, and that
  a pre-existing row keeps `era = 'pre-anchor'`.
- Contract gates in `drudge/src/serve/mcp.rs` (`quality_gate_*`) must still pass; if the MCP
  tool schema for `claims` changes, update the inventory the gate compares.

## Gate

From `drudge/`: `cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test`.
From the repo root: `sh scripts/guard.sh` green, `make test-db-upgrade` green (needs docker;
a disposable container, never the live one — read the target before running it).

## Deliverable

One commit on this branch, English subject under 72 chars in the repo's style
(`feat(claims): a claim anchors to the code the note cites`), plain-prose body that states the
counts you saw and what you verified. **Do not push.** Comments only where a name cannot
carry the reason. Print `WORKER_DONE <sha>` as the last line.
