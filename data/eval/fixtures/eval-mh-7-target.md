---
id: eval-mh-7-target
title: Offset windows shift when rows are inserted; use a keyset cursor
kind: note
origin: personal
date: "2026-01-11"
tags:
  - sql
  - cursors
tools:
  - postgres
concepts:
  - keyset cursor
claims:
  - subject: feed-api
    predicate: skipped-record-cause
    value: offset windows shift when rows are inserted
summary: Numeric offsets moved under the reader as rows arrived at the top; a keyset cursor is stable.
---

The endpoint used `LIMIT 20 OFFSET n`, and every row inserted at the head pushes later rows
down one slot, so the next window starts one row late. It now returns an opaque cursor that
encodes the last `(created_at, id)` pair, and the next query is
`WHERE (created_at, id) < ($1, $2) ORDER BY created_at DESC, id DESC LIMIT 20`, backed by a
composite index, which is unaffected by how many rows arrived in between.
