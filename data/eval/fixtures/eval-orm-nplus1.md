---
id: eval-orm-nplus1
title: ORM N+1 queries — one SQL per row turned a list endpoint slow
kind: note
origin: personal
date: "2026-03-18"
tags:
  - orm
  - database
  - performance
  - n-plus-one
tools:
  - sqlalchemy
  - django-orm
concepts:
  - eager loading
  - lazy relationship access
  - select-in / join prefetch
relates_to: []
summary: A list endpoint fired one extra query per row by lazily accessing a relationship in a loop; eager-loading the relation (selectinload / prefetch_related) collapsed N+1 into 2 queries.
---

# ORM N+1 queries made a list endpoint slow

A list endpoint that returned fine with ten rows crawled at a thousand. Query logging showed the same `SELECT ... WHERE parent_id = ?` running once per row — the classic N+1: the loop touched a lazy relationship attribute, and each access issued its own round-trip.

The total query count was `1 + N` instead of a constant. None of the individual queries were slow; the latency was pure round-trip overhead multiplied by the row count.

Fix: eager-load the relationship up front so the ORM batches it. In SQLAlchemy that is `selectinload`/`joinedload`; in Django it is `select_related` (FK, via JOIN) or `prefetch_related` (reverse/m2m, via a second `IN` query). That turns `1 + N` into 2 queries regardless of row count. Add a test asserting the query count so the regression can't creep back.
