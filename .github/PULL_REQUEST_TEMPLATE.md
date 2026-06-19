<!--
Title: use Conventional Commits — type(scope): summary
PRs are squash-merged, so the title becomes the commit message.
-->

## Background

<!-- Why is this change needed? What problem or context motivates it? Link any related issue. -->

## Changes

<!-- What did you change? Bullet the key edits — keep it minimal and surgical. -->

## Result

<!-- What is the observable outcome? Before/after, and how you verified it. -->

## Review points

<!-- Where should reviewers focus? Tradeoffs, risks, follow-ups, anything non-obvious. -->

## Checklist

- [ ] Branched off `main` (no direct push to `main`).
- [ ] `make guard` passes locally (fmt + clippy `-D warnings` + test + py-compile).
- [ ] Title follows Conventional Commits.
- [ ] Change is consistent with `drudge/PHILOSOPHY.md` / `drudge/RUST-STYLE.md`.
