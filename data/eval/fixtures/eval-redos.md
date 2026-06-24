---
id: eval-redos
title: Catastrophic regex backtracking (ReDoS) froze a request thread
kind: note
origin: personal
date: "2026-04-09"
tags:
  - regex
  - redos
  - performance
  - security
tools:
  - regex
  - re2
concepts:
  - catastrophic backtracking
  - nested quantifiers
  - linear-time regex engine
relates_to: []
summary: An input-validation regex with nested quantifiers blew up to exponential backtracking on a crafted near-match, pinning a CPU; rewrote the pattern and moved to a linear-time engine (RE2) with a length cap.
---

# A regex hung the server on certain inputs (ReDoS)

One endpoint occasionally pinned a CPU at 100% and stopped responding; the same request replayed fine with a slightly different string. A thread dump showed it parked inside the regex engine.

The validator used a pattern with nested/overlapping quantifiers like `(a+)+$`. On an input that *almost* matches, a backtracking engine explores exponentially many ways to split the string — microseconds for short inputs, seconds-to-forever as length grows. A user (or attacker) supplying a crafted near-match turns it into a denial of service.

Fix on three fronts: rewrite the pattern to remove the ambiguity (no nested quantifiers; anchor and make subpatterns disjoint); run untrusted input through a **linear-time** engine (RE2 / Rust `regex`, which forbid backtracking by construction); and cap input length before matching. Add the crafted pathological string to the test suite so the regression is caught.
