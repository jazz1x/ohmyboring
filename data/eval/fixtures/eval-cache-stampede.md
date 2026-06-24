---
id: eval-cache-stampede
title: Cache stampede — every request hit the DB at once when a hot key expired
kind: note
origin: personal
date: "2026-04-30"
tags:
  - caching
  - redis
  - thundering-herd
  - performance
concepts:
  - cache stampede / dogpile
  - single-flight recompute
  - early probabilistic expiry
relates_to: []
summary: When a hot cache key expired, every concurrent request missed and recomputed it against the database simultaneously, spiking load; a single-flight lock plus jittered/early recompute collapsed the herd to one rebuild.
---

# All requests hit the DB at once when a hot key expired (thundering herd)

A periodic latency spike lined up exactly with a popular cache entry's TTL. The moment the key expired, every in-flight request missed together, and they all recomputed the same expensive value against the database at once — a cache stampede (dogpile). The DB briefly saturated, which made the recompute slower, which widened the window — a small self-amplifying outage.

Fix: ensure only ONE caller rebuilds a missing key while the others wait or serve stale. A single-flight / mutex-per-key lock lets the first miss recompute and everyone else await its result. Complement it with early/probabilistic expiry (recompute slightly *before* the TTL, jittered) so the rebuild happens off the cliff edge, and add jitter to TTLs so many keys don't expire on the same tick. Serving stale-while-revalidate also keeps latency flat during the rebuild.
