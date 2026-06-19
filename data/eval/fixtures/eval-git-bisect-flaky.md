---
id: eval-git-bisect-flaky
title: Using git bisect to find the commit that introduced a flaky test
kind: note
origin: personal
date: "2026-02-11"
tags:
  - git
  - testing
  - debugging
tools:
  - git
  - pytest
concepts:
  - git bisect
  - regression hunting
  - test isolation
relates_to: []
summary: A test started failing one run in five; git bisect run with a repeated test loop pinpointed the commit that introduced shared mutable state.
---

A unit test began failing roughly one run in five, with no obvious culprit commit.
Manually checking out old commits was too slow given the intermittency, so I scripted
`git bisect run`.

The trick for a flaky test is to make the bisect script run the test many times and
fail if it fails even once: `pytest -k the_test --count 20`. That turned a
probabilistic failure into a near-deterministic signal for bisect to follow. Within a
dozen steps bisect named the exact commit.

That commit had introduced a module-level cache shared between tests, so test order
leaked state. The fix was to reset the cache in a fixture. The takeaway: git bisect
works on flaky bugs too, as long as the test script amplifies the flakiness into a
reliable pass/fail.
