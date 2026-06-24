---
id: eval-dns-ttl
title: Stale DNS — client kept hitting the old IP after a failover
kind: note
origin: personal
date: "2026-04-23"
tags:
  - dns
  - networking
  - caching
  - jvm
concepts:
  - dns ttl caching
  - connection pool pinning
  - resolver cache
relates_to: []
summary: After a database failover the app kept connecting to the dead IP because the JVM cached the DNS lookup forever and the connection pool pinned the resolved address; bounding the resolver TTL and recycling pooled connections fixed it.
---

# App kept connecting to the old IP after a failover

A managed database failed over to a new node (the hostname's A record was repointed), but the application kept timing out against the *old* IP long after DNS had updated. `dig` from the host returned the new address immediately — so it was the app, not the network.

Two cachers were holding the stale answer. The JVM caches successful DNS lookups, by default effectively forever (`networkaddress.cache.ttl = -1`), so it never re-resolved. And the connection pool had already resolved the hostname to an IP at connection time and pinned it — existing pooled connections never re-looked-up even after the cache expired.

Fix: bound the resolver cache (`networkaddress.cache.ttl=60`) so re-resolution actually happens, and make the pool recycle connections (max-lifetime / validation) so it picks up the new address. The general rule: long-lived clients must honor DNS TTL and not pin a resolved IP for the life of the process.
