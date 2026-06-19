# Contributing to ohmyboring

Thanks for considering a contribution. ohmyboring is a self-hosted personal
memory RAG: your Claude Code sessions are distilled into a local, human-readable
wiki and recalled on demand. This guide covers how to build, test, and submit
changes.

## Prerequisites

- **Rust** (edition 2024) with `clippy` and `rustfmt` — the `drudge` engine.
- **Python 3** — the hook and agent adapters (`hooks/`, `agents/`).
- **Docker** + **Docker Compose** — only to run the full stack locally; not
  required to build or pass the gate.

## Build & test

The engine lives in `drudge/`. The mechanical gate is one command:

```bash
make guard
```

`make guard` runs `scripts/guard.sh` — the single source of truth shared by the
pre-commit hook and CI:

1. `cargo fmt --check` — formatting.
2. `cargo clippy --all-targets -- -D warnings` — lint, with the philosophy
   rules enforced (no `unwrap`/`expect`/`panic`, exhaustive matches, pedantic).
3. `cargo test` — the guardrail tests (stack-free).
4. `python3 -m py_compile` over `agents/` and `hooks/` — adapter syntax.

Run it before every commit; the same checks run in CI, so green locally means
green on the PR.

Install the pre-commit hook to get the fast subset on every commit:

```bash
pip install pre-commit && pre-commit install
```

Two heavier gates run in CI (and are available locally):

- `make deny` — supply-chain gate (`cargo-deny`: vulnerabilities, licenses,
  duplicate versions).
- `make eval` — behavioral regression gate (needs the stack running).

Do **not** bypass the gate with `git commit --no-verify`. On failure, fix the
root cause rather than papering over the symptom.

## Branch & PR workflow

- **Never push directly to `main`.** Create a feature branch and open a pull
  request. `main` is protected; CI is a required check.
- Keep changes **minimal and surgical** — consistent with the surrounding code.
- Write **[Conventional Commits](https://www.conventionalcommits.org/)**:
  `type(scope): summary` (e.g. `fix(distill): …`, `feat(ingest): …`,
  `chore: …`). PRs are squash-merged, so the PR title becomes the commit.
- Fill in the PR template (background / changes / result / review points).
- A PR must be green on CI before it can merge: the structural gate, secret
  scan (gitleaks), supply-chain gate (cargo-deny), and security scan (trivy).

## Design philosophy

Before non-trivial Rust changes, read the two documents the gate enforces:

- [`drudge/PHILOSOPHY.md`](drudge/PHILOSOPHY.md) — the "why": integrity, ADTs,
  parse-don't-validate, fail-fast, Railway-Oriented Programming, SRP.
- [`drudge/RUST-STYLE.md`](drudge/RUST-STYLE.md) — the "how": the concrete
  rules clippy and review apply.

## Reporting bugs & requesting features

Open an issue using the [bug report](.github/ISSUE_TEMPLATE/bug_report.md) or
[feature request](.github/ISSUE_TEMPLATE/feature_request.md) template. For
security issues, follow [`SECURITY.md`](SECURITY.md) instead of opening a public
issue.

By contributing, you agree that your work is licensed under the project's
[MIT License](LICENSE).
