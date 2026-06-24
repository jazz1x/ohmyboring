---
id: eval-lost-update
title: Lost update — two concurrent writes clobbered each other (read-modify-write race)
kind: note
origin: personal
date: "2026-04-16"
tags:
  - concurrency
  - database
  - race-condition
  - optimistic-locking
tools:
  - postgres
concepts:
  - read-modify-write race
  - optimistic concurrency / version column
  - last-write-wins
relates_to: []
summary: Two requests each read a row, modified it, and wrote back; the second overwrote the first's change (last-write-wins). A version column with a compare-and-set UPDATE (optimistic locking) made the stale write fail and retry.
---

# Two requests overwrote each other's update

A counter / balance occasionally ended up wrong under concurrency. The flow was read-modify-write: each request `SELECT`ed the row, computed a new value in app code, then `UPDATE`d it. When two ran at once, both read the same starting value and the second write clobbered the first — a classic lost update. Nothing errored; data was just silently wrong.

The bug is invisible at low traffic and only shows under contention, so it passes manual testing.

Fix: make the write conditional on the value not having changed since the read. Add a `version` (or `updated_at`) column and do an optimistic compare-and-set: `UPDATE ... SET val = $new, version = version + 1 WHERE id = $id AND version = $seen`. If zero rows update, someone else won the race — re-read and retry. For pure increments, push the arithmetic into SQL (`SET n = n + 1`) so the database serializes it. A `SELECT ... FOR UPDATE` row lock is the pessimistic alternative.
