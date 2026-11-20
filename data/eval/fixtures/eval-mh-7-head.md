---
id: eval-mh-7-head
title: Pagination skips some records when new items are added while scrolling
kind: note
origin: personal
date: "2026-03-16"
tags:
  - incident
  - feed
tools:
  - rest-api
concepts:
  - consistent listing
claims:
  - subject: feed-api
    predicate: skipped-record-cause
    value: offset windows shift when rows are inserted
summary: Pagination skips records while new items arrive during scrolling; the cause is in an older note.
---

Pagination skips some records when new items are added while a user is scrolling through
the feed. Nothing is lost in the database, and reloading from the top shows every record.
The cause is documented in the older note that shares the claim below.
