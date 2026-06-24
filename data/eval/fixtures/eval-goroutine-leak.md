---
id: eval-goroutine-leak
title: Goroutine leak — senders block forever on a channel nobody drains
kind: note
origin: personal
date: "2026-03-25"
tags:
  - go
  - goroutine
  - concurrency
  - memory-leak
tools:
  - go
  - pprof
concepts:
  - unbuffered channel blocking
  - context cancellation
  - goroutine lifecycle
relates_to: []
summary: Memory and goroutine count climbed because worker goroutines blocked forever sending on a channel after the consumer returned early; a context-aware select with cancellation let them exit.
---

# Goroutine leak from senders blocking on an undrained channel

Process memory crept up under load and never came back down. A `pprof` goroutine profile showed thousands of goroutines parked in `chan send` — they were stuck, not busy.

The pattern: a handler spawned worker goroutines that sent results on an unbuffered channel, but the handler returned early (timeout / first error) and stopped receiving. Every worker then blocked forever on the send, holding its stack and captured variables — a textbook goroutine leak.

Fix: give every goroutine a way out. Pass a `context.Context` and `select` between the send and `<-ctx.Done()`, so a cancelled/returned consumer unblocks the senders instead of stranding them. Cancel the context in a `defer` on the handler. A buffered channel only hides the leak until the buffer fills; cancellation is the real fix. Add a goroutine-count assertion around the handler in tests.
