---
id: eval-mh-2-target
title: Local database schema migration blocked the main thread
kind: note
origin: personal
date: "2026-01-06"
tags:
  - sqlite
  - responsiveness
tools:
  - instruments
concepts:
  - cold start
claims:
  - subject: mobile-client
    predicate: startup-freeze-cause
    value: schema migration runs on the ui thread
summary: A SQLite schema migration executed on the UI thread; moving it to a worker removed the stall.
---

Time Profiler attributed 4.8 s of wall time to `sqlite3_step` under the embedded database's
`ALTER TABLE` plus an index rebuild, all on the main dispatch queue. The migration now runs
on a utility-QoS serial queue inside a transaction. Views read from a read-only snapshot
connection in WAL mode until the migration commits, then swap to the live connection.
