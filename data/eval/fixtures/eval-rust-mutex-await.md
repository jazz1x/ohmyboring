---
id: eval-rust-mutex-await
title: Tokio task deadlocks when a std Mutex guard is held across an await
kind: note
origin: personal
date: "2026-03-04"
tags:
  - rust
  - tokio
  - concurrency
  - deadlock
tools:
  - rust
  - tokio
  - parking_lot
concepts:
  - mutex guard lifetime
  - holding a lock across await
  - send bound on futures
relates_to: []
summary: A tokio task froze because a std::sync::Mutex guard stayed alive across an .await; scope the guard so it drops before awaiting, or use tokio::sync::Mutex.
---

# Tokio task deadlocks when a lock is held across an await

A background task stopped making progress and the whole runtime stalled. The cause: a `std::sync::Mutex` guard was still in scope when the code hit an `.await`, so the lock was held while the task yielded — and another task waiting on the same lock could never acquire it.

Two fixes. The simplest: drop the guard before awaiting by scoping it in its own block, `{ let v = m.lock().unwrap(); v.compute() }`, so the guard is gone before the await. When the lock genuinely must span the await, switch to `tokio::sync::Mutex`, whose guard is held across await points safely (and is `Send`).

Clippy's `await_holding_lock` catches the std-Mutex case; turn it on. The symptom is sneaky because it only deadlocks under contention, so it passes light tests and hangs in production.
