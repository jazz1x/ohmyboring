---
id: eval-pg-pool
title: Tuning the Postgres connection pool to stop "too many clients" errors
kind: note
origin: personal
date: "2026-02-01"
tags:
  - postgres
  - connection-pool
  - performance
tools:
  - postgres
  - pgbouncer
  - deadpool
concepts:
  - connection pooling
  - max_connections
  - transaction pooling
relates_to: []
summary: A burst of requests exhausted Postgres connections; a bounded deadpool plus pgbouncer in transaction mode fixed the FATAL too-many-clients errors.
---

The service started throwing `FATAL: sorry, too many clients already` under load.
Each worker opened its own unbounded Postgres connection, so a traffic spike blew
past the server `max_connections` of 100.

The fix was two layered changes. First, a bounded `deadpool` of 16 connections per
worker so the application can never open more than it is allowed. Second, putting
`pgbouncer` in front in **transaction pooling** mode, which multiplexes many short
client transactions onto a small set of real backend connections.

After the change the open-connection count stayed flat under the same load and the
too-many-clients FATAL errors disappeared. The lesson: never let a worker open an
unbounded number of database connections — always pool with an explicit ceiling.
